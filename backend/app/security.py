"""PII masking, log redaction and the encrypted per-job data store.

Encryption model (envelope encryption):

* Each job gets its own random Fernet data key (DEK) — the "per-session key".
* The DEK is stored on disk only after being wrapped (encrypted) with the
  master key(s) from ``GOVFLOW_MASTER_KEYS``. This lets a job survive a
  backend restart while keeping nothing readable at rest.
* Profiles and uploaded documents are encrypted with the job's DEK.
* ``delete_job_data`` removes the wrapped DEK first (crypto-shredding: even
  a leftover ciphertext copy is unreadable), then every file for the job.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Iterable
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import BaseModel

from .config import Settings, get_settings
from .models import (
    Address,
    BankDetails,
    CitizenProfile,
    DocumentKind,
    DocumentRef,
    FieldSource,
    enum_label,
)

logger = logging.getLogger(__name__)

MASK_CHAR = "X"

# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def mask_last4(value: str | None, keep: int = 4) -> str:
    """Mask all but the last ``keep`` alphanumerics: ``XXXXXXXX9012``.

    Separators are dropped so the length of grouped IDs is not leaked by
    their formatting. Values of ``keep`` characters or fewer are fully masked.
    """
    if not value:
        return ""
    chars = re.sub(r"[\s\-/]", "", str(value))
    if len(chars) <= keep:
        return MASK_CHAR * len(chars)
    return MASK_CHAR * (len(chars) - keep) + chars[-keep:]


def mask_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return mask_last4(digits)


def mask_email(value: str | None) -> str:
    """``rahul.kumar@gmail.com`` → ``r**********@gmail.com``."""
    if not value or "@" not in value:
        return mask_last4(value)
    local, _, domain = value.partition("@")
    if not local:
        return f"@{domain}"
    return f"{local[0]}{'*' * max(len(local) - 1, 1)}@{domain}"


def mask_name(value: str | None) -> str:
    """Keep initials only, e.g. ``Rahul Kumar`` → ``R*** K***``. For logs."""
    if not value:
        return ""
    return " ".join(f"{p[0]}***" for p in value.split() if p)


def mask_bank(details: BankDetails | None) -> dict[str, str]:
    if details is None:
        return {}
    out: dict[str, str] = {}
    if details.account_holder:
        out["account_holder"] = details.account_holder
    if details.account_number:
        out["account_number"] = mask_last4(details.account_number)
    if details.ifsc:
        out["ifsc"] = details.ifsc
    if details.bank_name:
        out["bank_name"] = details.bank_name
    if details.branch:
        out["branch"] = details.branch
    return out


def mask_address(addr: Address | None) -> str:
    """Hide the street lines; district/state/PIN are not identifying alone."""
    if addr is None:
        return ""
    parts = []
    if addr.line1 or addr.line2:
        parts.append("***")
    parts.extend(p for p in (addr.village_or_city, addr.block, addr.district, addr.state) if p)
    s = ", ".join(parts)
    return f"{s} - {addr.pincode}" if addr.pincode else s


def mask_field_value(field_name: str, value: Any) -> str:
    """Mask a single profile value for display to the citizen."""
    if value is None or value == "":
        return ""
    if field_name == "id_number":
        return mask_last4(str(value))
    if field_name == "phone":
        return mask_phone(str(value))
    if field_name == "email":
        return mask_email(str(value))
    if field_name == "bank_details" and isinstance(value, BankDetails):
        return ", ".join(f"{k}: {v}" for k, v in mask_bank(value).items())
    if field_name == "address" and isinstance(value, Address):
        return value.one_line()
    if isinstance(value, Enum):
        return enum_label(value)
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if field_name == "income" and isinstance(value, int):
        return f"₹{value:,}"
    return str(value)


def mask_profile(profile: CitizenProfile) -> dict[str, str]:
    """Flat, display-safe view of a profile (for UI and confirm modal)."""
    out: dict[str, str] = {}
    for name in CitizenProfile.FIELD_NAMES:
        f = profile.get_field(name)
        if f.is_set:
            out[name] = mask_field_value(name, f.value)
    if profile.documents:
        out["documents"] = ", ".join(f"{d.kind.value}: {d.filename}" for d in profile.documents)
    return out


def mask_resolved_reference(reference: str, resolved: str) -> str:
    """Mask a value the agent typed via a ``profile.*`` reference.

    Used when recording ``AgentStep.args`` and building
    ``Receipt.submitted_fields_masked``.
    """
    ref, _, fmt = reference.partition("|")
    if fmt == "last4":
        return resolved  # already partial by construction
    if ref.startswith("profile.id_number") or ref == "profile.bank_details.account_number":
        return mask_last4(resolved)
    if ref.startswith("profile.phone"):
        return mask_phone(resolved)
    if ref.startswith("profile.email"):
        return mask_email(resolved)
    if ref.startswith("profile.address.line"):
        return "***"
    return resolved


# ---------------------------------------------------------------------------
# Free-text redaction (logs, page text echoed to logs, exceptions)
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Email
    (re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b"), lambda m: mask_email(m.group(0))),
    # Aadhaar: 12 digits, optionally grouped 4-4-4
    (re.compile(r"(?<!\d)\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)"), lambda m: mask_last4(m.group(0))),
    # PAN: AAAAA9999A
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), lambda m: mask_last4(m.group(0))),
    # Indian mobile, optionally +91 / 0 prefixed
    (re.compile(r"(?<!\d)(?:\+?91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)"), lambda m: mask_phone(m.group(0))),
    # Bank account numbers / other long digit runs (9-18 digits)
    (re.compile(r"(?<!\d)\d{9,18}(?!\d)"), lambda m: mask_last4(m.group(0))),
]


class _SensitiveRegistry:
    """Exact values known to be sensitive (e.g. a loaded profile's ID number).

    Regexes miss oddly formatted IDs (voter IDs, passports, DL numbers), so
    every sensitive profile value is registered here and redacted verbatim.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, set[str]] = {}

    def register(self, job_id: str, values: Iterable[str | None]) -> None:
        clean = {v for v in values if v and len(v) >= 4}
        if not clean:
            return
        with self._lock:
            self._values.setdefault(job_id, set()).update(clean)

    def forget(self, job_id: str) -> None:
        with self._lock:
            self._values.pop(job_id, None)

    def snapshot(self) -> list[str]:
        with self._lock:
            vals = {v for s in self._values.values() for v in s}
        # Longest first so substrings don't break longer matches.
        return sorted(vals, key=len, reverse=True)


sensitive_registry = _SensitiveRegistry()


def register_profile_secrets(job_id: str, profile: CitizenProfile) -> None:
    bank = profile.bank_details.value
    sensitive_registry.register(
        job_id,
        [
            profile.id_number.value,
            profile.phone.value,
            profile.email.value,
            bank.account_number if bank else None,
        ],
    )


def redact_text(text: str, extra_values: Iterable[str] = ()) -> str:
    """Redact PII from arbitrary text using known values and regexes."""
    if not text:
        return text
    for v in [*extra_values, *sensitive_registry.snapshot()]:
        if v and v in text:
            text = text.replace(v, mask_last4(v))
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists (e.g. structured logs)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    if isinstance(obj, BaseModel):
        return redact_obj(obj.model_dump(mode="json"))
    return obj


class PIIRedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message and exc text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # malformed %-args; let logging report it
            return True
        record.msg = redact_text(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


def configure_logging(settings: Settings | None = None) -> None:
    """Install root logging with PII redaction on every handler."""
    settings = settings or get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    redactor = PIIRedactingFilter()
    for h in root.handlers:
        if not any(isinstance(f, PIIRedactingFilter) for f in h.filters):
            h.addFilter(redactor)
    # Third-party loggers that may echo request bodies.
    for name in ("httpx", "anthropic", "openai", "uvicorn.access"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


class CryptoError(RuntimeError):
    pass


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` readable only by the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_master_cipher(settings: Settings) -> MultiFernet:
    raw: list[str] = []
    if settings.master_keys is not None:
        raw = [k.strip() for k in settings.master_keys.get_secret_value().split(",") if k.strip()]
    if not raw:
        if settings.env == "prod":
            raise CryptoError("GOVFLOW_MASTER_KEYS is required in prod")
        path = settings.dev_master_key_path
        if not path.exists():
            _write_private(path, Fernet.generate_key())
            logger.warning("Generated a dev master key at %s; set GOVFLOW_MASTER_KEYS for real use", path)
        raw = [path.read_text().strip()] if path.exists() else []
    if not raw or not raw[0]:
        key = Fernet.generate_key().decode()
        raw = [key]
        try:
            _write_private(settings.dev_master_key_path, key.encode())
        except Exception:
            pass
    try:
        return MultiFernet([Fernet(k.encode()) for k in raw])
    except (ValueError, TypeError) as e:
        raise CryptoError("GOVFLOW_MASTER_KEYS contains an invalid Fernet key") from e


# ---------------------------------------------------------------------------
# Encrypted per-job store
# ---------------------------------------------------------------------------

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-]+")
_RECORD_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


def validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        raise ValueError("invalid job id")
    return job_id


class JobDataStore:
    """Encrypted storage for a job's profile and documents.

    Layout under ``data_dir``::

        profiles/<job_id>.key   wrapped DEK
        profiles/<job_id>.bin   Fernet(DEK) of the profile JSON
        uploads/<job_id>/<doc_key>.bin   Fernet(DEK) of each document
        screenshots/<job_id>/   (written by the browser layer)
        browser_state/<job_id>.json
        receipts/<job_id>/
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self._master = load_master_cipher(self.settings)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._dek_cache: dict[str, Fernet] = {}

    # -- paths --------------------------------------------------------------

    def _key_path(self, job_id: str) -> Path:
        return self.settings.profiles_dir / f"{validate_job_id(job_id)}.key"

    def _profile_path(self, job_id: str) -> Path:
        return self.settings.profiles_dir / f"{validate_job_id(job_id)}.bin"

    def _uploads_dir(self, job_id: str) -> Path:
        return self.settings.uploads_dir / validate_job_id(job_id)

    def _record_path(self, job_id: str, name: str) -> Path:
        if not _RECORD_NAME_RE.fullmatch(name):
            raise ValueError("invalid record name")
        return self.settings.profiles_dir / f"{validate_job_id(job_id)}.{name}.rec"

    def job_paths(self, job_id: str) -> list[Path]:
        """Every on-disk location that may hold data for the job."""
        validate_job_id(job_id)
        s = self.settings
        return [
            self._key_path(job_id),
            self._profile_path(job_id),
            *sorted(s.profiles_dir.glob(f"{job_id}.*.rec")),
            self._uploads_dir(job_id),
            s.screenshots_dir / job_id,
            s.browser_state_dir / f"{job_id}.json",
            s.receipts_dir / job_id,
        ]

    def _lock(self, job_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(job_id, threading.RLock())

    # -- keys ---------------------------------------------------------------

    def _dek(self, job_id: str, create: bool) -> Fernet:
        with self._lock(job_id):
            cached = self._dek_cache.get(job_id)
            if cached is not None:
                return cached
            path = self._key_path(job_id)
            if path.exists():
                try:
                    dek = self._master.decrypt(path.read_bytes())
                except InvalidToken as e:
                    raise CryptoError(f"cannot unwrap data key for job {job_id}") from e
            elif create:
                dek = Fernet.generate_key()
                _write_private(path, self._master.encrypt(dek))
            else:
                raise FileNotFoundError(f"no data for job {job_id}")
            fernet = Fernet(dek)
            self._dek_cache[job_id] = fernet
            return fernet

    def rotate_master(self, job_id: str) -> None:
        """Re-wrap a job's DEK with the current primary master key."""
        with self._lock(job_id):
            path = self._key_path(job_id)
            _write_private(path, self._master.rotate(path.read_bytes()))

    # -- profile ------------------------------------------------------------

    def save_profile(self, job_id: str, profile: CitizenProfile) -> None:
        with self._lock(job_id):
            token = self._dek(job_id, create=True).encrypt(profile.model_dump_json().encode())
            _write_private(self._profile_path(job_id), token)
        register_profile_secrets(job_id, profile)

    def load_profile(self, job_id: str) -> CitizenProfile | None:
        with self._lock(job_id):
            path = self._profile_path(job_id)
            if not path.exists():
                return None
            try:
                raw = self._dek(job_id, create=False).decrypt(path.read_bytes())
            except InvalidToken as e:
                raise CryptoError(f"profile for job {job_id} is corrupt or tampered") from e
        profile = CitizenProfile.model_validate_json(raw)
        register_profile_secrets(job_id, profile)
        return profile

    def has_profile(self, job_id: str) -> bool:
        return self._profile_path(job_id).exists()

    # -- other encrypted records (intake state, transcripts) ------------------

    def save_record(self, job_id: str, name: str, data: bytes) -> None:
        with self._lock(job_id):
            token = self._dek(job_id, create=True).encrypt(data)
            _write_private(self._record_path(job_id, name), token)

    def load_record(self, job_id: str, name: str) -> bytes | None:
        path = self._record_path(job_id, name)
        with self._lock(job_id):
            if not path.exists():
                return None
            try:
                return self._dek(job_id, create=False).decrypt(path.read_bytes())
            except InvalidToken as e:
                raise CryptoError(f"record {name} for job {job_id} is corrupt or tampered") from e

    def save_screenshot(self, job_id: str, png_bytes: bytes) -> str:
        validate_job_id(job_id)
        out_dir = self.settings.screenshots_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"receipt_{int(time.time())}.png"
        path.write_bytes(png_bytes)
        return str(path)

    # -- documents ----------------------------------------------------------

    def save_document(
        self,
        job_id: str,
        doc_key: str,
        kind: DocumentKind,
        filename: str,
        mime_type: str,
        data: bytes,
        source: FieldSource = FieldSource.USER_EDIT,
    ) -> DocumentRef:
        if len(data) > self.settings.max_upload_bytes:
            raise ValueError("document exceeds the maximum upload size")
        ref = DocumentRef(
            doc_key=doc_key,
            kind=kind,
            filename=_SAFE_FILENAME_RE.sub("_", Path(filename).name)[:120] or "document",
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            source=source,
        )
        with self._lock(job_id):
            token = self._dek(job_id, create=True).encrypt(data)
            _write_private(self._uploads_dir(job_id) / f"{ref.doc_key}.bin", token)
        return ref

    def load_document(self, job_id: str, doc_key: str) -> bytes:
        if not re.fullmatch(r"[a-z0-9_]{1,40}", doc_key):
            raise ValueError("invalid doc key")
        path = self._uploads_dir(job_id) / f"{doc_key}.bin"
        with self._lock(job_id):
            if not path.exists():
                raise FileNotFoundError(f"document {doc_key} not found")
            try:
                return self._dek(job_id, create=False).decrypt(path.read_bytes())
            except InvalidToken as e:
                raise CryptoError(f"document {doc_key} is corrupt or tampered") from e

    def materialize_document(self, job_id: str, ref: DocumentRef, dest_dir: Path) -> Path:
        """Write a decrypted copy for a browser file chooser.

        The caller must delete it right after the upload; see
        :func:`secure_delete_file`.
        """
        data = self.load_document(job_id, ref.doc_key)
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise CryptoError(f"document {ref.doc_key} failed its integrity check")
        out = dest_dir / ref.filename
        _write_private(out, data)
        return out

    # -- deletion -----------------------------------------------------------

    def delete_job_data(self, job_id: str) -> list[str]:
        """Delete everything stored for a job. Returns removed path names."""
        removed: list[str] = []
        with self._lock(job_id):
            self._dek_cache.pop(job_id, None)
            # Key first: once it's gone, any remaining ciphertext is useless.
            for path in self.job_paths(job_id):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(path.name)
                elif path.exists():
                    secure_delete_file(path)
                    removed.append(path.name)
        sensitive_registry.forget(job_id)
        with self._locks_guard:
            self._locks.pop(job_id, None)
        logger.info("Deleted data for job %s (%d items)", job_id, len(removed))
        return removed


def secure_delete_file(path: Path) -> None:
    """Best-effort overwrite then unlink.

    Overwriting is not reliable on SSDs or copy-on-write filesystems; the
    real guarantee comes from deleting the wrapped key (crypto-shredding).
    """
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(os.urandom(size))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


_store: JobDataStore | None = None
_store_guard = threading.Lock()


def get_store() -> JobDataStore:
    global _store
    with _store_guard:
        if _store is None:
            _store = JobDataStore()
        return _store
