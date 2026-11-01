from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.models import (
    ActionType,
    AgentStep,
    Category,
    CitizenProfile,
    Gender,
    IdType,
    InvalidProfileReference,
    MissingProfileData,
    ProfileField,
    Receipt,
    InputMode,
    ServiceRequest,
    parse_date_flexible,
    parse_income,
)


def test_normalisation(profile, expected_dob):
    assert profile.dob.value == expected_dob
    assert profile.gender.value is Gender.FEMALE
    assert profile.id_type.value is IdType.AADHAAR
    assert profile.id_number.value == "234567890123"
    assert profile.phone.value == "9876543210"
    assert profile.email.value == "priya.s@example.com"
    assert profile.income.value == 250_000
    assert profile.category.value is Category.GENERAL_EWS
    assert profile.address.value.pincode == "221001"
    assert profile.bank_details.value.account_number == "12345678901234"
    assert profile.bank_details.value.ifsc == "SBIN0001234"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2004-08-15", date(2004, 8, 15)),
        ("15-08-2004", date(2004, 8, 15)),
        ("15.08.2004", date(2004, 8, 15)),
        ("15th Aug 2004", date(2004, 8, 15)),
        ("August 15, 2004", date(2004, 8, 15)),
    ],
)
def test_parse_date_flexible(raw, expected):
    assert parse_date_flexible(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("2,50,000", 250_000), ("₹ 1,20,000", 120_000), ("80k", 80_000), ("1.2 lakh", 120_000), (99000, 99000)],
)
def test_parse_income(raw, expected):
    assert parse_income(raw) == expected


def test_invalid_values_rejected():
    with pytest.raises(ValidationError):
        CitizenProfile(phone=ProfileField[str](value="12345"))
    with pytest.raises(ValidationError):
        CitizenProfile(email=ProfileField[str](value="not-an-email"))
    with pytest.raises(ValidationError):
        CitizenProfile(name=ProfileField[str](value="x", confidence=1.5))


def test_confirmation_flags(profile):
    flagged = profile.fields_needing_confirmation(0.85)
    assert "category" in flagged and "address" in flagged
    assert "name" not in flagged
    profile.name.conflict_note = "Voice said 'Priya Sharma', ID says 'Priya Sharmaa'"
    assert "name" in profile.fields_needing_confirmation(0.85)


def test_missing_fields():
    p = CitizenProfile(name=ProfileField[str](value="A B"))
    assert "name" not in p.missing_fields()
    assert p.missing_fields(["name", "dob"]) == ["dob"]


def test_resolve_reference(profile):
    r = profile.resolve_reference
    assert r("profile.dob") == "15/08/2004"
    assert r("profile.dob|YYYY-MM-DD") == "2004-08-15"
    assert r("profile.dob|day") == "15"
    assert r("profile.dob|month_name") == "August"
    assert r("profile.dob|year") == "2004"
    assert r("profile.name|first") == "Priya"
    assert r("profile.name|last") == "Sharma"
    assert r("profile.name|upper") == "PRIYA SHARMA"
    assert r("profile.category|label") == "General (EWS)"
    assert r("profile.category") == "general_ews"
    assert r("profile.address.district") == "Varanasi"
    assert r("profile.address").endswith("- 221001")
    assert r("profile.bank_details.ifsc") == "SBIN0001234"
    assert r("profile.income") == "250000"


def test_resolve_reference_errors(profile):
    with pytest.raises(InvalidProfileReference):
        profile.resolve_reference("profile.password")
    with pytest.raises(InvalidProfileReference):
        profile.resolve_reference("os.environ")
    with pytest.raises(InvalidProfileReference):
        profile.resolve_reference("profile.bank_details")
    with pytest.raises(InvalidProfileReference):
        profile.resolve_reference("profile.dob|weird")
    with pytest.raises(MissingProfileData):
        profile.resolve_reference("profile.address.line2")
    with pytest.raises(MissingProfileData):
        CitizenProfile().resolve_reference("profile.phone")


def test_available_references_hide_values(profile):
    refs = profile.available_references()
    assert "profile.id_number" in refs
    assert "profile.address.district" in refs
    assert "profile.address.line2" not in refs
    assert not any("2345" in r for r in refs)


def test_profile_roundtrip(profile):
    again = CitizenProfile.model_validate_json(profile.model_dump_json())
    assert again == profile


def test_service_request_and_receipt():
    sr = ServiceRequest(raw_user_text="x", input_mode=InputMode.LINK, portal_url="http://Localhost:5001/apply")
    assert sr.portal_host == "localhost"
    assert ServiceRequest(input_mode=InputMode.NAME).portal_host is None
    rc = Receipt(job_id="abc12345", reference_no="SCH-2026-0001", portal="localhost", service_name="Demo scheme")
    assert rc.timestamp.tzinfo is not None
    step = AgentStep(job_id="abc12345", step_no=1, action=ActionType.CLICK, args={"element_id": 4})
    assert step.status.value == "ok"


@pytest.mark.parametrize(
    "text,mode,url",
    [
        ("https://scholarships.gov.in/fresh", InputMode.LINK, "https://scholarships.gov.in/fresh"),
        ("pmkisan.gov.in", InputMode.LINK, "https://pmkisan.gov.in/"),
        ("http://localhost:5001/register", InputMode.LINK, "http://localhost:5001/register"),
        ("apply for Kushal Yuva 2026", InputMode.NAME, None),
        ("renew my driving licence", InputMode.NAME, None),
        ("PM-Kisan", InputMode.NAME, None),
    ],
)
def test_from_user_input(text, mode, url):
    sr = ServiceRequest.from_user_input(text)
    assert sr.input_mode is mode
    assert sr.raw_user_text == text
    assert (str(sr.portal_url) if sr.portal_url else None) == url
