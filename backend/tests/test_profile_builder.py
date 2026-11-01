from __future__ import annotations

import io

import pytest
from PIL import Image

from app.intake.id_extract import ExtractedValue, IdExtraction
from app.intake.profile_builder import (
    IntakeService,
    VoiceExtraction,
    _VoiceOut,
    build_profile,
    postprocess_voice,
    profile_card,
    profile_digest,
)
from app.intake.transcribe import Transcript
from app.models import Category, FieldSource, Gender, IdType
from app.security import JobDataStore
from tests.fakes import FakeLLM

JOB = "job_intake01"


def ev(value, conf=0.95):
    return ExtractedValue(value=value, confidence=conf)


def id_ex(**fields):
    base = {
        "name": ev("Priya Sharma", 0.97), "dob": ev("2004-08-15", 0.93), "gender": ev("female", 0.98),
        "id_type": ev("aadhaar", 0.99), "id_number": ev("999941057058", 0.92),
        "address": ev({"line1": "House 12, Gandhi Nagar", "district": "Varanasi", "state": "Uttar Pradesh",
                       "pincode": "221001"}, 0.9),
    }
    base.update(fields)
    return IdExtraction(document_type=IdType.AADHAAR, fields={k: v for k, v in base.items() if v is not None},
                        extras={"father_or_spouse_name": ev("Rakesh Sharma", 0.9)})


def voice_ex(**fields):
    base = {
        "name": ev("Priya Sharmaa", 0.8), "dob": ev("2004-08-15", 0.9), "phone": ev("9876500001", 0.95),
        "email": ev("priya.demo@example.com", 0.9), "income": ev(250000, 0.9), "category": ev("general_ews", 0.9),
        "address": ev({"village_or_city": "Rampur", "block": "Sadar", "district": "Varanasi"}, 0.9),
        "bank_details": ev({"account_number": "501000123456", "ifsc": "DEMO0001234", "bank_name": "Demo State Bank"}, 0.9),
    }
    base.update(fields)
    return VoiceExtraction(fields={k: v for k, v in base.items() if v is not None},
                           extras={"course": ev("B.Sc."), "institution_name": ev("Govt Degree College")})


def test_merge_rules_and_conflicts():
    r = build_profile([id_ex()], [voice_ex()])
    p = r.profile
    # Identity from the ID photo, even though the voice note disagrees.
    assert p.name.value == "Priya Sharma" and p.name.source is FieldSource.ID_PHOTO
    assert p.id_number.value == "999941057058"
    # Contact/preference from what she said.
    assert p.phone.value == "9876500001" and p.phone.source is FieldSource.VOICE
    assert p.category.value is Category.GENERAL_EWS and p.income.value == 250000
    # Address merged part by part: ID lines + spoken block/village; voice wins on shared parts.
    a = p.address.value
    assert a.line1 == "House 12, Gandhi Nagar" and a.block == "Sadar" and a.village_or_city == "Rampur"
    assert a.pincode == "221001" and p.address.source is FieldSource.VOICE
    # Conflicts are surfaced and flag the field for confirmation.
    names = [c for c in r.conflicts if c.field == "name"]
    assert names and "Priya Sharmaa" in names[0].other and p.name.conflict_note
    assert "name" in r.needs_confirmation
    assert not any(c.field == "dob" for c in r.conflicts)  # same value, no conflict
    assert r.extras["father_name"] == "Rakesh Sharma" and r.extras["course"] == "B.Sc."


def test_case_and_order_insensitive_names_are_not_conflicts():
    r = build_profile([id_ex(name=ev("PRIYA SHARMA"))], [voice_ex(name=ev("sharma priya"))])
    assert not any(c.field == "name" for c in r.conflicts)


def test_conflict_messages_mask_id_numbers():
    r = build_profile([id_ex()], [voice_ex(id_number=ev("999941057099", 0.6))])
    c = next(c for c in r.conflicts if c.field == "id_number")
    assert "999941057099" not in c.message and "XXXXXXXX7058" in c.message and "XXXXXXXX7099" in c.message
    assert r.profile.id_number.value == "999941057058"


def test_voice_fallback_when_no_id_and_two_ids():
    r = build_profile([], [voice_ex()])
    assert r.profile.name.value == "Priya Sharmaa" and r.profile.name.source is FieldSource.VOICE
    pan = IdExtraction(document_type=IdType.PAN, fields={"name": ev("Priya Sharma K", 0.99), "id_type": ev("pan")})
    r = build_profile([id_ex(), pan], [])
    assert r.profile.name.value == "Priya Sharma K"  # most confident ID read wins
    assert any(c.field == "name" and "Aadhaar" in c.other_from for c in r.conflicts)


def test_user_edit_wins_and_is_kept():
    r = build_profile([id_ex()], [voice_ex()], user_edits={"name": "Priya Sharma", "address": {"line1": "Flat 3"}})
    assert r.profile.name.source is FieldSource.USER_EDIT and r.profile.name.confidence == 1.0
    assert r.profile.name.conflict_note is None and "name" not in r.needs_confirmation
    assert r.profile.address.value.line1 == "Flat 3" and r.profile.address.source is FieldSource.USER_EDIT


def test_postprocess_voice_validation():
    def vv(value, conf=0.9):
        return {"value": value, "confidence": conf}

    empty = {"value": None, "confidence": 0}
    out = _VoiceOut.model_validate({
        "language": "hi", "name": vv("Ramesh Yadav"), "dob": vv("1979-11-05"), "gender": vv("male"),
        "phone": vv("98765"), "email": empty, "id_type": empty, "id_number": empty,
        "address_line1": empty, "address_line2": empty, "village_or_city": vv("Kurmawan"), "block": vv("Manpur"),
        "district": vv("Gaya"), "state": vv("Bihar"), "pincode": vv("8230"),
        "income": vv("1.2 lakh"), "category": vv("OBC"), "account_holder": empty, "account_number": vv("302000987654"),
        "ifsc": vv("BAD"), "bank_name": vv("Demo Gramin Bank"), "branch": empty,
        "other_facts": [{"key": "Land Area (acres)", "value": "2.5", "confidence": 0.8}],
        "service_request": "PM-Kisan claim",
    })
    ex = postprocess_voice(out, transcript_confidence=0.6)
    assert "phone" not in ex.fields and any("mobile" in i for i in ex.issues)
    assert ex.fields["income"].value == 120000
    assert ex.fields["name"].confidence == pytest.approx(0.9 * 0.6)
    assert "pincode" not in ex.fields["address"].value and ex.fields["address"].value["block"] == "Manpur"
    assert "ifsc" not in ex.fields["bank_details"].value
    assert ex.extras["land_area_acres"].value == "2.5"
    assert ex.service_request == "PM-Kisan claim"


def test_card_is_masked(profile):
    rows = {r.field: r for r in profile_card(profile, 0.85)}
    assert rows["id_number"].display_value == "XXXXXXXX0123"
    assert "12345678901234" not in rows["bank_details"].display_value
    assert rows["category"].needs_confirmation  # 0.7 confidence
    assert rows["name"].label_hi == "नाम"


# ---------------------------------------------------------------------------
# IntakeService end to end (fakes for the models, real encryption)
# ---------------------------------------------------------------------------


class FakeTranscriber:
    name = "fake"

    def __init__(self, text, language="hi", confidence=0.9):
        self.t = Transcript(text=text, language=language, confidence=confidence, backend="fake")

    async def transcribe(self, audio, **_):
        return self.t


def _vv(v, c=0.9):
    return {"value": v, "confidence": c}


_E = {"value": None, "confidence": 0}

VOICE_OUT = {
    "language": "hi", "name": _vv("Priya Sharmaa", 0.8), "dob": _E, "gender": _E, "phone": _vv("9876500001"),
    "email": _E, "id_type": _E, "id_number": _E, "address_line1": _E, "address_line2": _E,
    "village_or_city": _E, "block": _vv("Sadar"), "district": _vv("Varanasi"), "state": _E, "pincode": _E,
    "income": _vv("250000"), "category": _vv("general_ews"), "account_holder": _E, "account_number": _E,
    "ifsc": _E, "bank_name": _E, "branch": _E, "other_facts": [], "service_request": "apply for scholarship",
}

ID_OUT = {
    "is_identity_document": True, "document_type": "aadhaar", "legible": True, "issues": [],
    "name": _vv("Priya Sharma", 0.97), "dob": _vv("2004-08-15", 0.95), "year_of_birth": _E,
    "gender": _vv("female", 0.98), "id_number": _vv("999941057058", 0.95), "father_or_spouse_name": _E,
    "address_line1": _vv("House 12", 0.9), "address_line2": _E, "village_or_city": _E, "district": _vv("Varanasi"),
    "state": _vv("Uttar Pradesh"), "pincode": _vv("221001"),
}


def _photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1200, 760), "white").save(buf, "JPEG")
    return buf.getvalue()


async def test_intake_service_end_to_end(settings):
    store = JobDataStore(settings)
    llm = FakeLLM({"voice_extract": VOICE_OUT, "id_extract": ID_OUT})
    svc = IntakeService(settings, llm=llm, transcriber=FakeTranscriber("मेरा नाम प्रिया..."), store=store)

    u1 = await svc.add_voice(JOB, b"\x1a\x45\xdf\xa3" + b"\0" * 64)
    assert u1.service_request_hint == "apply for scholarship" and u1.transcript_language == "hi"
    assert not u1.confirmed

    u2 = await svc.add_id_photos(JOB, [(_photo(), "image/jpeg")])
    profile = store.load_profile(JOB)
    assert profile.name.value == "Priya Sharma" and profile.gender.value is Gender.FEMALE
    assert profile.address.value.block == "Sadar" and profile.address.value.pincode == "221001"
    assert any(d.doc_key == "id_proof" for d in profile.documents)
    assert any(c.field == "name" for c in u2.conflicts)

    # Encrypted at rest: neither the transcript nor the number is readable on disk.
    raw = (settings.profiles_dir / f"{JOB}.intake.rec").read_bytes()
    assert "प्रिया".encode() not in raw and b"999941057058" not in raw

    update, errors = svc.edit(JOB, {"name": "Priya Sharma", "phone": "12345", "address": {"line1": "House 12A"}})
    assert errors == {"phone": "phone must be a 10-digit Indian mobile number"}
    assert store.load_profile(JOB).address.value.district == "Varanasi"  # partial edit kept other parts

    confirmed = svc.confirm(JOB)
    assert confirmed.confirmed and svc.is_confirmed(JOB)
    # Any later change voids the confirmation.
    svc.edit(JOB, {"email": "priya.demo@example.com"})
    assert not svc.is_confirmed(JOB)
    assert svc.confirm(JOB).confirmed

    # A fresh service (backend restart) sees the same state.
    again = IntakeService(settings, llm=llm, transcriber=FakeTranscriber(""), store=JobDataStore(settings))
    assert again.is_confirmed(JOB)

    removed = store.delete_job_data(JOB)
    assert f"{JOB}.intake.rec" in removed
    assert again.load_state(JOB).voice_extractions == []


async def test_empty_recording_and_not_an_id(settings):
    store = JobDataStore(settings)
    llm = FakeLLM({"id_extract": {**ID_OUT, "is_identity_document": False}})
    svc = IntakeService(settings, llm=llm, transcriber=FakeTranscriber("  "), store=store)
    u = await svc.add_voice(JOB, b"\x1a\x45\xdf\xa3" + b"\0" * 64)
    assert u.issues[0].startswith("I could not hear")
    u = await svc.add_id_photos(JOB, [(_photo(), "image/jpeg")])
    assert u.issues[0].startswith("This does not look")
    assert svc.load_state(JOB).documents == []
    with pytest.raises(ValueError):
        svc.confirm(JOB)


def test_digest_ignores_documents(profile):
    d1 = profile_digest(profile)
    profile.documents = []
    assert profile_digest(profile) == d1
    profile.phone.value = "9876500009"
    assert profile_digest(profile) != d1
