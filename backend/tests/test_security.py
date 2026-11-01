from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from app import security
from app.config import Settings
from app.models import DocumentKind
from app.security import (
    CryptoError,
    JobDataStore,
    PIIRedactingFilter,
    mask_email,
    mask_last4,
    mask_profile,
    mask_resolved_reference,
    redact_text,
    validate_job_id,
)

JOB = "job_0001abcd"


def test_mask_last4():
    assert mask_last4("2345 6789 0123") == "XXXXXXXX0123"
    assert mask_last4("ABCDE1234F") == "XXXXXX234F"
    assert mask_last4("123") == "XXX"
    assert mask_last4(None) == ""


def test_mask_email():
    assert mask_email("priya.s@example.com") == "p******@example.com"


def test_mask_profile_never_contains_raw_pii(profile):
    masked = mask_profile(profile)
    blob = " ".join(masked.values())
    assert masked["id_number"] == "XXXXXXXX0123"
    assert masked["phone"] == "XXXXXX3210"
    assert "12345678901234" not in blob
    assert "XXXXXXXXXX1234" in masked["bank_details"]
    assert "234567890123" not in blob
    assert masked["category"] == "General (EWS)"
    assert masked["dob"] == "15/08/2004"
    assert masked["income"] == "₹250,000"


def test_mask_resolved_reference():
    assert mask_resolved_reference("profile.id_number", "234567890123") == "XXXXXXXX0123"
    assert mask_resolved_reference("profile.phone|last4", "3210") == "3210"
    assert mask_resolved_reference("profile.address.line1", "12 Gandhi Nagar") == "***"
    assert mask_resolved_reference("profile.name", "Priya Sharma") == "Priya Sharma"


@pytest.mark.parametrize(
    "text,leak",
    [
        ("aadhaar 2345 6789 0123 entered", "6789"),
        ("pan ABCDE1234F", "ABCDE"),
        ("call +91 98765 43210 now", "98765"),
        ("mobile 9876543210", "98765"),
        ("acct 12345678901234", "1234567890"),
        ("mail priya.s@example.com", "priya.s"),
    ],
)
def test_redact_text_patterns(text, leak):
    assert leak not in redact_text(text)


def test_redact_registered_values(settings, profile):
    security.sensitive_registry.register(JOB, ["UP1420110012345"])
    assert "UP1420110012345" not in redact_text("DL number UP1420110012345 ok")


def test_log_filter_redacts_args_and_exceptions(caplog):
    logger = logging.getLogger("govflow.test")
    caplog.handler.addFilter(PIIRedactingFilter())
    with caplog.at_level(logging.INFO, logger="govflow.test"):
        logger.info("typing id %s", "2345 6789 0123")
        try:
            raise ValueError("bad phone 9876543210")
        except ValueError:
            logger.exception("failed")
    out = caplog.text
    assert "6789" not in out and "98765" not in out
    assert "XXXXXXXX0123" in out


def test_validate_job_id():
    assert validate_job_id(JOB) == JOB
    for bad in ("../../etc", "short", "a/b/c/d/e/f", ""):
        with pytest.raises(ValueError):
            validate_job_id(bad)


def test_profile_store_roundtrip_and_encryption(settings, profile):
    store = JobDataStore(settings)
    store.save_profile(JOB, profile)
    raw = (settings.profiles_dir / f"{JOB}.bin").read_bytes()
    assert b"Priya" not in raw and b"234567890123" not in raw
    # A fresh store (simulating a backend restart) can still read it.
    assert JobDataStore(settings).load_profile(JOB) == profile
    assert store.load_profile("job_missing1") is None


def test_documents_and_materialize(settings, tmp_path):
    store = JobDataStore(settings)
    ref = store.save_document(JOB, "income_cert", DocumentKind.INCOME_CERTIFICATE, "../../evil name.pdf", "application/pdf", b"%PDF-1.4 data")
    assert ref.filename == "evil_name.pdf"
    assert store.load_document(JOB, "income_cert") == b"%PDF-1.4 data"
    assert b"PDF" not in (settings.uploads_dir / JOB / "income_cert.bin").read_bytes()
    out = store.materialize_document(JOB, ref, tmp_path)
    assert out.read_bytes() == b"%PDF-1.4 data"


def test_wrong_master_key_cannot_read(settings, profile, monkeypatch):
    JobDataStore(settings).save_profile(JOB, profile)
    monkeypatch.setenv("GOVFLOW_MASTER_KEYS", Fernet.generate_key().decode())
    other = Settings()
    with pytest.raises(CryptoError):
        JobDataStore(other).load_profile(JOB)


def test_master_key_rotation(settings, profile, monkeypatch):
    old_key = settings.master_keys.get_secret_value()
    JobDataStore(settings).save_profile(JOB, profile)
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("GOVFLOW_MASTER_KEYS", f"{new_key},{old_key}")
    rotated = JobDataStore(Settings())
    rotated.rotate_master(JOB)
    monkeypatch.setenv("GOVFLOW_MASTER_KEYS", new_key)
    assert JobDataStore(Settings()).load_profile(JOB) == profile


def test_delete_job_data(settings, profile):
    store = JobDataStore(settings)
    store.save_profile(JOB, profile)
    store.save_document(JOB, "photo", DocumentKind.PHOTO, "me.jpg", "image/jpeg", b"\xff\xd8jpeg")
    (settings.screenshots_dir / JOB).mkdir(parents=True)
    (settings.screenshots_dir / JOB / "1.jpg").write_bytes(b"x")
    removed = store.delete_job_data(JOB)
    assert f"{JOB}.key" in removed and f"{JOB}.bin" in removed
    for p in store.job_paths(JOB):
        assert not p.exists()
    assert store.load_profile(JOB) is None
    assert JOB not in security.sensitive_registry._values


def test_dev_master_key_autogenerated(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVFLOW_ENV", "dev")
    monkeypatch.delenv("GOVFLOW_MASTER_KEYS", raising=False)
    s = Settings(GOVFLOW_DATA_DIR=tmp_path / "d", GOVFLOW_MASTER_KEYS=None)
    store = JobDataStore(s)
    assert s.dev_master_key_path.exists()
    assert store is not None


def test_prod_requires_master_key(monkeypatch):
    monkeypatch.delenv("GOVFLOW_MASTER_KEYS", raising=False)
    with pytest.raises(ValueError):
        Settings(GOVFLOW_ENV="prod", GOVFLOW_MASTER_KEYS=None)


def test_csv_env_lists(monkeypatch):
    monkeypatch.setenv("EXTRA_ALLOWED_DOMAINS", "a.gov.in, b.gov.in")
    monkeypatch.setenv("CORS_ORIGINS", '["http://x:1"]')
    s = Settings()
    assert s.extra_allowed_domains == ["a.gov.in", "b.gov.in"]
    assert s.cors_origins == ["http://x:1"]
