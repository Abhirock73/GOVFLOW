from __future__ import annotations

import io
from datetime import date

import pytest
from PIL import Image

from app.models import (
    CitizenProfile,
    DocumentKind,
    DocumentProblem,
    DocumentRef,
    DocumentRequirement,
    EligibilityKind,
    EligibilityRule,
    FieldSource,
    FieldType,
    FormPlan,
    FormStep,
    InputMode,
    PlanField,
    QuestionKind,
    ServiceRequest,
    StepKind,
    WarningSeverity,
)
from app.planner.gap_analysis import (
    ConversionError,
    analyze_gaps,
    apply_answers,
    convert_for_requirement,
    parse_batch_reply,
)
from tests.fakes import FakeLLM

TODAY = date(2026, 10, 30)
SHA = "0" * 64


def doc(key, kind, filename, mime, size_kb):
    return DocumentRef(doc_key=key, kind=kind, filename=filename, mime_type=mime,
                       size_bytes=int(size_kb * 1024), sha256=SHA)


def make_plan(rules=None) -> FormPlan:
    return FormPlan(
        portal_url="https://portal.gov.in/",
        form_version_hash="abc",
        steps=[FormStep(index=0, title="Login", kind=StepKind.LOGIN), FormStep(index=1, title="Form")],
        required_fields=[
            PlanField(key="s0.f0", label="User ID", step_index=0, required=True, profile_ref="profile.phone"),
            PlanField(key="s1.f0", label="Name", step_index=1, required=True, profile_ref="profile.name"),
            PlanField(key="s1.f1", label="Date of birth", step_index=1, required=True,
                      profile_ref="profile.dob|DD/MM/YYYY", format_hint="DD/MM/YYYY"),
            PlanField(key="s1.f2", label="Father's name", step_index=1, required=True, answer_key="father_name"),
            PlanField(key="s1.f3", label="Course", step_index=1, required=True, field_type=FieldType.SELECT,
                      options=["B.A.", "B.Sc."], answer_key="course"),
            PlanField(key="s1.f4", label="District", step_index=1, required=True, profile_ref="profile.address.district"),
            PlanField(key="s1.f5", label="Captcha", step_index=1, required=True, field_type=FieldType.CAPTCHA),
            PlanField(key="s1.f6", label="I hereby declare", step_index=1, required=True, field_type=FieldType.CHECKBOX),
            PlanField(key="s1.f7", label="Mobile", step_index=1, required=True, profile_ref="profile.phone"),
        ],
        required_documents=[
            DocumentRequirement(key="photo", name="Photograph", kind=DocumentKind.PHOTO, accepted_formats=["jpg"],
                                max_size_kb=100),
            DocumentRequirement(key="income", name="Income certificate", kind=DocumentKind.INCOME_CERTIFICATE,
                                accepted_formats=["pdf"], max_size_kb=500),
            DocumentRequirement(key="caste", name="Caste certificate", kind=DocumentKind.CASTE_CERTIFICATE,
                                accepted_formats=["pdf"]),
            DocumentRequirement(key="optional_doc", name="Optional", required=False),
        ],
        eligibility_rules=rules or [],
    )


def test_gap_report_batches_everything(profile):
    profile.address.value.district = None
    profile.documents = [
        doc("photo", DocumentKind.PHOTO, "me.png", "image/png", 300),
        doc("income", DocumentKind.INCOME_CERTIFICATE, "inc.jpg", "image/jpeg", 50),
    ]
    req = ServiceRequest(input_mode=InputMode.NAME, extra_params={"course": "B.Sc."})
    report = analyze_gaps(make_plan(), profile, req, today=TODAY)

    targets = {q.target for q in report.questions_for_user}
    assert "profile.address.district" in targets
    assert "extra.father_name" in targets
    assert "extra.course" not in targets  # already answered
    assert not any("captcha" in t or "declare" in t.lower() for t in targets)
    assert "profile.phone" not in targets  # login step skipped; phone present anyway

    issues = {d.requirement_key: d for d in report.missing_documents}
    assert issues["photo"].problem is DocumentProblem.WRONG_FORMAT and issues["photo"].can_auto_convert
    assert issues["income"].problem is DocumentProblem.WRONG_FORMAT and issues["income"].can_auto_convert
    assert issues["caste"].problem is DocumentProblem.MISSING
    assert "optional_doc" not in issues
    assert "doc.caste" in targets and "doc.photo" not in targets  # auto-fixable ones are not asked

    assert report.summary_en.startswith("I need 2 more details and 1 document")
    assert "दस्तावेज़" in report.summary_hi
    assert not report.ready


def test_eligibility_honesty(profile):
    rules = [
        EligibilityRule(kind=EligibilityKind.AGE, description="Age 25-35", min_value=25, max_value=35),
        EligibilityRule(kind=EligibilityKind.INCOME, description="Income below 2 lakh", max_value=200000),
        EligibilityRule(kind=EligibilityKind.CATEGORY, description="General/EWS", allowed_values=["general"]),
        EligibilityRule(kind=EligibilityKind.DOMICILE, description="Bihar residents", allowed_values=["Bihar"]),
        EligibilityRule(kind=EligibilityKind.EDUCATION, description="Passed class 12"),
    ]
    req = ServiceRequest(input_mode=InputMode.NAME)
    report = analyze_gaps(make_plan(rules), profile, req, today=TODAY)
    by_kind = {w.kind: w for w in report.eligibility_warnings}
    assert by_kind[EligibilityKind.AGE].severity is WarningSeverity.BLOCKER  # 22 years old
    assert by_kind[EligibilityKind.INCOME].severity is WarningSeverity.BLOCKER  # 2.5 lakh
    assert EligibilityKind.CATEGORY not in by_kind  # EWS counts as general
    assert by_kind[EligibilityKind.DOMICILE].severity is WarningSeverity.BLOCKER  # UP resident
    assert by_kind[EligibilityKind.EDUCATION].severity is WarningSeverity.UNVERIFIED
    assert report.appears_ineligible
    assert "may not be eligible" in report.summary_en and "decide" in report.summary_en
    assert any(q.kind is QuestionKind.ELIGIBILITY and q.options == ["Yes", "No"] for q in report.questions_for_user)
    # The profile is never altered to fit the rules.
    assert profile.income.value == 250000


def test_apply_answers_and_reanalyze(profile):
    profile.address.value.district = None
    profile.documents = [doc("photo", DocumentKind.PHOTO, "p.jpg", "image/jpeg", 50),
                         doc("income", DocumentKind.INCOME_CERTIFICATE, "i.pdf", "application/pdf", 100),
                         doc("caste", DocumentKind.CASTE_CERTIFICATE, "c.pdf", "application/pdf", 100)]
    rules = [EligibilityRule(kind=EligibilityKind.EDUCATION, description="Passed class 12")]
    req = ServiceRequest(input_mode=InputMode.NAME)
    report = analyze_gaps(make_plan(rules), profile, req, today=TODAY)
    ids = {q.target: q.id for q in report.questions_for_user}
    answers = {ids["profile.address.district"]: "Varanasi", ids["extra.father_name"]: "Rakesh Sharma",
               ids["extra.course"]: "b.sc.", ids["eligibility.0"]: "हाँ"}
    res = apply_answers(report, answers, profile, req)
    assert not res.errors, res.errors
    assert res.profile.address.value.district == "Varanasi"
    assert res.profile.address.source is FieldSource.USER_EDIT and res.profile.address.confidence == 1.0
    assert res.request.extra_params["course"] == "B.Sc."  # normalised to the exact option
    assert res.request.extra_params["eligibility_confirmations"] == {"0": "yes"}
    again = analyze_gaps(make_plan(rules), res.profile, res.request, today=TODAY)
    assert again.questions_for_user == [] and again.ready


def test_apply_answers_validation_errors(profile):
    profile.phone.value = None
    plan = make_plan()
    req = ServiceRequest(input_mode=InputMode.NAME, extra_params={"course": "B.A.", "father_name": "X Y"})
    report = analyze_gaps(plan, profile, req, today=TODAY)
    field_qs = [q for q in report.questions_for_user if q.kind is QuestionKind.FIELD]
    assert [q.target for q in field_qs] == ["profile.phone"]
    qid = field_qs[0].id
    res = apply_answers(report, {qid: "12345"}, profile, req)
    assert qid in res.errors and "10-digit" in res.errors[qid]
    assert res.profile.phone.value is None and res.applied == []
    ok = apply_answers(report, {qid: "+91 98765 43210"}, profile, req)
    assert ok.profile.phone.value == "9876543210" and not ok.errors


async def test_parse_batch_reply(profile):
    plan = make_plan()
    report = analyze_gaps(plan, profile, ServiceRequest(input_mode=InputMode.NAME), today=TODAY)
    q_ids = [q.id for q in report.questions_for_user if q.kind is QuestionKind.FIELD]
    llm = FakeLLM({"parse_batch_reply": {"answers": [
        {"question_id": q_ids[0], "answer": "Rakesh Sharma"},
        {"question_id": "q999", "answer": "ignored"},
        {"question_id": q_ids[1], "answer": None},
    ]}})
    out = await parse_batch_reply("pita ka naam Rakesh Sharma hai", report.questions_for_user, llm)
    assert out == {q_ids[0]: "Rakesh Sharma"}
    assert "<reply>" in llm.user_content_for("parse_batch_reply")


def _png_bytes(size=(1600, 1200)):
    img = Image.effect_noise(size, 80).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_convert_png_to_small_jpeg():
    data = _png_bytes()
    req = DocumentRequirement(key="photo", name="Photo", accepted_formats=["jpg"], max_size_kb=100)
    out = convert_for_requirement(data, "image/png", "me.png", req)
    assert out.mime_type == "image/jpeg" and out.filename == "me.jpg"
    assert len(out.data) <= 100 * 1024 and out.data[:3] == b"\xff\xd8\xff"


def test_convert_image_to_pdf_and_pdf_rules():
    data = _png_bytes((800, 600))
    req = DocumentRequirement(key="income", name="Income", accepted_formats=["pdf"], max_size_kb=400)
    out = convert_for_requirement(data, "image/png", "scan.png", req)
    assert out.data.startswith(b"%PDF") and len(out.data) <= 400 * 1024
    small = convert_for_requirement(out.data, "application/pdf", "scan.pdf", req)
    assert small.note == "unchanged"
    with pytest.raises(ConversionError):
        convert_for_requirement(out.data, "application/pdf", "scan.pdf",
                                DocumentRequirement(key="p", name="Photo", accepted_formats=["jpg"]))
    with pytest.raises(ConversionError):
        convert_for_requirement(b"not an image", "image/png", "x.png", req)
