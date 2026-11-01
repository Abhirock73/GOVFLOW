from __future__ import annotations

from datetime import date

import pytest
from cryptography.fernet import Fernet

from app import security
from app.config import Settings, get_settings
from app.models import (
    Address,
    BankDetails,
    CitizenProfile,
    FieldSource,
    ProfileField,
)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("GOVFLOW_ENV", "test")
    monkeypatch.setenv("GOVFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOVFLOW_MASTER_KEYS", Fernet.generate_key().decode())
    get_settings.cache_clear()
    s = get_settings()
    yield s
    get_settings.cache_clear()
    security.sensitive_registry._values.clear()


@pytest.fixture
def profile() -> CitizenProfile:
    return CitizenProfile(
        name=ProfileField[str](value="Priya Sharma", confidence=0.97, source=FieldSource.ID_PHOTO),
        dob=ProfileField(value="15/08/2004", confidence=0.93, source=FieldSource.ID_PHOTO),
        gender=ProfileField(value="F", confidence=0.99, source=FieldSource.ID_PHOTO),
        id_type=ProfileField(value="Aadhar", confidence=0.99, source=FieldSource.ID_PHOTO),
        id_number=ProfileField[str](value="2345 6789 0123", confidence=0.91, source=FieldSource.ID_PHOTO),
        address=ProfileField(
            value=Address(
                line1="12 Gandhi Nagar",
                village_or_city="Rampur",
                block="Sadar",
                district="Varanasi",
                state="Uttar Pradesh",
                pincode="221 001",
            ),
            confidence=0.8,
            source=FieldSource.ID_PHOTO,
        ),
        phone=ProfileField[str](value="+91 98765 43210", confidence=0.9, source=FieldSource.VOICE),
        email=ProfileField[str](value="Priya.S@Example.com", confidence=0.88, source=FieldSource.VOICE),
        income=ProfileField(value="2.5 lakh", confidence=0.86, source=FieldSource.VOICE),
        category=ProfileField(value="EWS", confidence=0.7, source=FieldSource.VOICE),
        bank_details=ProfileField(
            value=BankDetails(
                account_holder="Priya Sharma",
                account_number="1234-5678-9012-34",
                ifsc="sbin0001234",
                bank_name="State Bank of India",
            ),
            confidence=0.95,
            source=FieldSource.USER_EDIT,
        ),
    )


@pytest.fixture
def expected_dob() -> date:
    return date(2004, 8, 15)
