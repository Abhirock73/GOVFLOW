from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import pytest

from app.intake.transcribe import (
    AudioError,
    FasterWhisperTranscriber,
    OpenAIWhisperTranscriber,
    TranscriberUnavailable,
    get_transcriber,
    normalize_language,
    sniff_audio,
)

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64


class FakeTranscriptions:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.resp


def fake_openai(resp):
    t = FakeTranscriptions(resp)
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=t)), t


def test_sniff_audio():
    assert sniff_audio(WEBM) == ("webm", "audio/webm")
    assert sniff_audio(WAV) == ("wav", "audio/wav")
    assert sniff_audio(M4A) == ("m4a", "audio/mp4")
    with pytest.raises(AudioError):
        sniff_audio(b"%PDF-1.4 not audio at all.......")
    with pytest.raises(AudioError):
        sniff_audio(b"")


def test_normalize_language():
    assert normalize_language("hindi") == "hi"
    assert normalize_language("English") == "en"
    assert normalize_language("hi") == "hi"
    assert normalize_language(None) == "und"


async def test_openai_transcriber_hindi(settings, caplog):
    resp = SimpleNamespace(
        text="मेरा नाम प्रिया शर्मा है, मेरा नंबर 9876500001 है",
        language="hindi",
        duration=7.5,
        segments=[SimpleNamespace(start=0.0, end=3.0, text="मेरा नाम प्रिया शर्मा है", avg_logprob=-0.1),
                  SimpleNamespace(start=3.0, end=7.5, text="मेरा नंबर 9876500001 है", avg_logprob=-0.3)],
    )
    client, calls = fake_openai(resp)
    tr = OpenAIWhisperTranscriber(settings, client=client)
    with caplog.at_level(logging.INFO):
        t = await tr.transcribe(WEBM, language_hint="hi")
    assert t.language == "hi" and t.duration_seconds == 7.5
    assert t.confidence == pytest.approx(math.exp(-0.2))
    assert len(t.segments) == 2 and t.backend == "openai"
    call = calls.calls[0]
    assert call["model"] == settings.whisper_model and call["response_format"] == "verbose_json"
    assert call["language"] == "hi" and "prompt" in call
    assert call["file"][0] == "voice.webm"
    assert "प्रिया" not in caplog.text and "9876500001" not in caplog.text  # transcripts never logged


async def test_openai_auto_language_and_limits(settings):
    client, calls = fake_openai(SimpleNamespace(text="My name is Arjun", language="english", duration=2, segments=[]))
    t = await OpenAIWhisperTranscriber(settings, client=client).transcribe(WAV)
    assert t.language == "en" and t.confidence == 1.0
    assert "language" not in calls.calls[0]
    with pytest.raises(AudioError):
        await OpenAIWhisperTranscriber(settings, client=client).transcribe(WEBM + b"\x00" * (26 * 1024 * 1024))


def test_openai_requires_key(settings):
    settings.openai_api_key = None
    with pytest.raises(TranscriberUnavailable):
        OpenAIWhisperTranscriber(settings)


class FakeWhisperModel:
    def __init__(self):
        self.paths = []

    def transcribe(self, path, **kw):
        self.paths.append(path)
        segs = iter([SimpleNamespace(start=0.0, end=2.0, text=" मेरा नाम रमेश है ", avg_logprob=-0.05)])
        return segs, SimpleNamespace(language="hi", duration=2.0)


async def test_faster_whisper_deletes_temp_file(settings):
    model = FakeWhisperModel()
    tr = FasterWhisperTranscriber(settings, model=model)
    t = await tr.transcribe(WEBM)
    assert t.text == "मेरा नाम रमेश है" and t.language == "hi" and t.backend == "faster_whisper"
    from pathlib import Path

    assert model.paths and not Path(model.paths[0]).exists()
    FasterWhisperTranscriber._model = None


def test_factory(settings):
    settings.transcriber = "faster_whisper"
    assert isinstance(get_transcriber(settings), FasterWhisperTranscriber)
