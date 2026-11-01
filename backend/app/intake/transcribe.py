"""Speech to text behind a pluggable ``Transcriber`` interface.

Backends:
  * ``openai`` (default): OpenAI Whisper API (``OPENAI_API_KEY``)
  * ``faster_whisper``: local model via the optional ``faster-whisper`` package

Both handle Hindi, English and code-mixed speech. Transcripts contain PII:
they are returned to the caller and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..security import secure_delete_file

logger = logging.getLogger(__name__)

OPENAI_MAX_BYTES = 25 * 1024 * 1024

# (magic prefix, offset, extension, mime)
_AUDIO_SIGNATURES: list[tuple[bytes, int, str, str]] = [
    (b"\x1a\x45\xdf\xa3", 0, "webm", "audio/webm"),  # EBML: webm / mkv (MediaRecorder default)
    (b"OggS", 0, "ogg", "audio/ogg"),
    (b"RIFF", 0, "wav", "audio/wav"),
    (b"fLaC", 0, "flac", "audio/flac"),
    (b"ID3", 0, "mp3", "audio/mpeg"),
    (b"\xff\xfb", 0, "mp3", "audio/mpeg"),
    (b"\xff\xf3", 0, "mp3", "audio/mpeg"),
    (b"\xff\xf2", 0, "mp3", "audio/mpeg"),
    (b"ftyp", 4, "m4a", "audio/mp4"),  # mp4 / m4a (iOS recorders)
]

_LANG_NAMES = {
    "hindi": "hi", "english": "en", "urdu": "ur", "bengali": "bn", "marathi": "mr", "tamil": "ta", "telugu": "te",
    "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa", "odia": "or", "nepali": "ne",
}

# Bias Whisper toward Devanagari Hindi / Latin English rather than Urdu script.
_HINDI_PROMPT = "नमस्ते, मेरा नाम ... है। My mobile number is ... मैं आवेदन करना चाहता हूँ।"


class AudioError(ValueError):
    pass


class TranscriberUnavailable(RuntimeError):
    pass


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    text: str
    language: str = "und"  # ISO 639-1 where known
    duration_seconds: float | None = None
    # Mean per-segment probability in [0, 1]; 1.0 when the backend gives none.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    backend: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class Transcriber(Protocol):
    name: str

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None,
                         language_hint: str | None = None) -> Transcript: ...


def sniff_audio(data: bytes) -> tuple[str, str]:
    """Return ``(extension, mime)`` from magic bytes, or raise AudioError."""
    if len(data) < 16:
        raise AudioError("The recording is empty or too short.")
    for magic, offset, ext, mime in _AUDIO_SIGNATURES:
        if data[offset: offset + len(magic)] == magic:
            if ext == "wav" and data[8:12] != b"WAVE":
                continue
            return ext, mime
    raise AudioError("Unsupported audio format. Please record again (webm, ogg, mp3, m4a, wav or flac).")


def normalize_language(lang: str | None) -> str:
    if not lang:
        return "und"
    lang = lang.strip().lower()
    return _LANG_NAMES.get(lang, lang[:2] if len(lang) >= 2 else "und")


def _confidence_from_logprobs(logprobs: list[float]) -> float:
    if not logprobs:
        return 1.0
    return max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


class OpenAIWhisperTranscriber:
    name = "openai"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        if client is None:
            if settings.openai_api_key is None:
                raise TranscriberUnavailable("TRANSCRIBER=openai but OPENAI_API_KEY is not set")
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        self._client = client

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None,
                         language_hint: str | None = None) -> Transcript:
        ext, mime = sniff_audio(audio)
        if len(audio) > OPENAI_MAX_BYTES:
            raise AudioError("The recording is too long. Please keep voice notes under about 10 minutes.")
        kwargs: dict[str, Any] = {
            "model": self.settings.whisper_model,
            "file": (f"voice.{ext}", audio, mime_type or mime),
            "response_format": "verbose_json",
            "temperature": 0,
        }
        hint = normalize_language(language_hint) if language_hint else None
        if hint in ("hi", "en"):
            kwargs["language"] = hint
        if hint in (None, "hi", "und"):
            kwargs["prompt"] = _HINDI_PROMPT
        resp = await self._client.audio.transcriptions.create(**kwargs)
        segments = _get(resp, "segments") or []
        transcript = Transcript(
            text=(_get(resp, "text") or "").strip(),
            language=normalize_language(_get(resp, "language") or hint),
            duration_seconds=_get(resp, "duration"),
            confidence=_confidence_from_logprobs([_get(s, "avg_logprob") for s in segments
                                                  if _get(s, "avg_logprob") is not None]),
            segments=[TranscriptSegment(start=_get(s, "start", 0.0), end=_get(s, "end", 0.0),
                                        text=(_get(s, "text") or "").strip()) for s in segments],
            backend=self.name,
        )
        logger.info("transcribed audio backend=%s lang=%s seconds=%s chars=%d conf=%.2f", self.name,
                    transcript.language, transcript.duration_seconds, len(transcript.text), transcript.confidence)
        return transcript


class FasterWhisperTranscriber:
    """Local transcription; the model loads once per process and runs in a thread."""

    name = "faster_whisper"
    _model: Any = None
    _model_lock = threading.Lock()

    def __init__(self, settings: Settings, model: Any | None = None) -> None:
        self.settings = settings
        if model is not None:
            type(self)._model = model

    def _load(self) -> Any:
        with self._model_lock:
            if type(self)._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as e:
                    raise TranscriberUnavailable(
                        "TRANSCRIBER=faster_whisper needs the optional package: pip install faster-whisper"
                    ) from e
                type(self)._model = WhisperModel(self.settings.faster_whisper_model, device="auto", compute_type="int8")
            return type(self)._model

    async def transcribe(self, audio: bytes, *, mime_type: str | None = None,
                         language_hint: str | None = None) -> Transcript:
        ext, _ = sniff_audio(audio)
        hint = normalize_language(language_hint) if language_hint else None
        return await asyncio.to_thread(self._run, audio, ext, hint)

    def _run(self, audio: bytes, ext: str, hint: str | None) -> Transcript:
        model = self._load()
        tmp_dir = self.settings.uploads_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=tmp_dir, prefix=".voice-", suffix=f".{ext}")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(audio)
            os.chmod(path, 0o600)
            segments, info = model.transcribe(
                path, language=hint if hint in ("hi", "en") else None, vad_filter=True, beam_size=5,
                initial_prompt=_HINDI_PROMPT if hint in (None, "hi") else None,
            )
            segs = list(segments)  # the generator does the actual decoding
        finally:
            secure_delete_file(Path(path))
        return Transcript(
            text=" ".join(s.text.strip() for s in segs).strip(),
            language=normalize_language(getattr(info, "language", None)),
            duration_seconds=getattr(info, "duration", None),
            confidence=_confidence_from_logprobs([s.avg_logprob for s in segs if s.avg_logprob is not None]),
            segments=[TranscriptSegment(start=s.start, end=s.end, text=s.text.strip()) for s in segs],
            backend=self.name,
        )


def get_transcriber(settings: Settings | None = None) -> Transcriber:
    settings = settings or get_settings()
    if settings.transcriber == "faster_whisper":
        return FasterWhisperTranscriber(settings)
    return OpenAIWhisperTranscriber(settings)
