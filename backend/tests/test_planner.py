from __future__ import annotations

from datetime import date

import pytest

from app.agent.memory import PlanCache, normalize_portal_url
from app.models import DocumentKind, FieldType, ObstacleKind, StepKind
from app.planner.form_crawler import CrawlResult, InfoPage, PageSnapshot, classify
from app.planner.requirements import (
    FormPlanner,
    baseline_documents,
    baseline_fields,
    build_form_plan,
    valid_profile_ref,
)
from tests.fakes import FakeLLM


def f(label, type_="text", **kw):
    base = {"tag": "input", "type": type_, "name": label.lower().replace(" ", "_"), "id": "", "label": label,
            "hint": "", "required": False, "maxlength": None, "pattern": "", "accept": "", "multiple": False,
            "autocomplete": "", "disabled": False, "readonly": False, "options": [], "options_total": 0,
            "option_label": ""}
    base.update(kw)
    return base


def make_crawl() -> CrawlResult:
    login = PageSnapshot(
        url="https://portal.gov.in/login", title="Login", lang="en", text="Login. New user? Register here",
        fields=[f("Mobile No."), f("Password", "password"), f("Captcha", hint="enter text")],
        buttons=[], flags={"password": True, "captcha": True},
    )
    login.kind = classify(login)
    form = PageSnapshot(
        url="https://portal.gov.in/apply", title="Application Form", lang="en", text="I hereby declare ...",
        fields=[
            f("Applicant Name *", required=True),
            f("Date of Birth", hint="(DD/MM/YYYY)", required=True),
            f("Day", "select", options=[str(d) for d in range(1, 32)], options_total=31),
            f("State", "select", options=["Bihar", "Uttar Pradesh"], options_total=2, required=True),
            f("District", "select", required=True),
            f("Father's Name", required=True),
            f("Upload Photo", "file", accept=".jpg,.jpeg", hint="(JPG only, max 200 KB, min 20 KB)", required=True),
            f("I agree to the declaration", "checkbox", required=True),
            f("Gender", "radio", options=["Male", "Female"], options_total=2, required=True, in_iframe=True),
        ],
        buttons=[], flags={"declaration": True, "otp": False},
    )
    form.kind = classify(form)
    return CrawlResult(
        start_url="https://portal.gov.in/", final_url="https://portal.gov.in/", entry_title="Demo Scheme",
        form_pages=[login, form], login_wall=True, registration_wall=True,
        info_pages=[InfoPage("https://portal.gov.in/guidelines", "Guidelines",
                             "Eligibility: age 18 to 35 as on 01/07/2026. Residents of Bihar only. Income below 3 lakh.")],
    )


def plan_out(_content):
    return {
        "portal_title": "Demo Scheme Portal",
        "language": "en",
        "steps": [
            {"title": "Login", "kind": "login", "page_index": 0},
            {"title": "Application form", "kind": "form", "page_index": 1},
            {"title": "Upload documents", "kind": "form", "page_index": None},
        ],
        "fields": [
            {"key": "s1.f0", "plain_label": "Applicant name", "required": True, "format_hint": None,
             "profile_ref": "profile.name", "answer_key": None, "notes": None},
            {"key": "s1.f1", "plain_label": "Date of birth", "required": True, "format_hint": "DD/MM/YYYY",
             "profile_ref": "profile.dob|DD/MM/YYYY", "answer_key": None, "notes": None},
            {"key": "s1.f2", "plain_label": "Birth day", "required": True, "format_hint": None,
             "profile_ref": "profile.dob|day", "answer_key": None, "notes": None},
            {"key": "s1.f3", "plain_label": "State", "required": True, "format_hint": None,
             "profile_ref": "profile.address.state", "answer_key": None, "notes": None},
            {"key": "s1.f4", "plain_label": "District", "required": True, "format_hint": None,
             "profile_ref": "profile.address.district", "answer_key": None, "notes": None},
            # The model invents a profile field; it must be dropped in favour of answer_key.
            {"key": "s1.f5", "plain_label": "Father's name", "required": True, "format_hint": None,
             "profile_ref": "profile.father_name", "answer_key": "father_name", "notes": None},
            {"key": "s1.f6", "plain_label": "Photo", "required": True, "format_hint": None,
             "profile_ref": "profile.name", "answer_key": None, "notes": None},
            {"key": "s1.f8", "plain_label": "Gender", "required": True, "format_hint": None,
             "profile_ref": "profile.gender|label", "answer_key": None, "notes": None},
            {"key": "zz.bogus", "plain_label": "x", "required": True, "format_hint": None,
             "profile_ref": None, "answer_key": None, "notes": None},
        ],
        "inferred_fields": [
            {"label": "Bank account number", "step_title": "Upload documents", "field_type": "text", "required": True,
             "profile_ref": "profile.bank_details.account_number", "answer_key": None, "options": []},
        ],
        "documents": [
            {"name": "Recent passport-size photograph", "kind": "photo", "accepted_formats": ["jpg"],
             "max_size_kb": None, "min_size_kb": None, "count": 1, "required": True, "notes": None, "field_key": "s1.f6"},
            {"name": "Income certificate", "kind": "income_certificate", "accepted_formats": ["PDF"],
             "max_size_kb": 500, "min_size_kb": None, "count": 1, "required": True, "notes": None, "field_key": None},
        ],
        "eligibility_rules": [
            {"kind": "age", "description": "Age 18 to 35 as on 01/07/2026", "min_value": 18, "max_value": 35,
             "allowed_values": [], "reference_date": "2026-07-01", "source_text": "age 18 to 35"},
            {"kind": "domicile", "description": "Residents of Bihar", "min_value": None, "max_value": None,
             "allowed_values": ["Bihar"], "reference_date": None, "source_text": None},
            {"kind": "income", "description": "Family income below 3 lakh", "min_value": None, "max_value": 300000,
             "allowed_values": [], "reference_date": None, "source_text": None},
        ],
        "obstacles": [{"kind": "session_timeout", "description": "20 minute session", "step_index": None}],
        "login_required": True,
        "registration_required": False,
        "scheme_status": "open",
        "deadline": "2026-12-31",
        "instructions_summary": "Register, log in, fill the form and upload documents.",
    }


def test_baseline_extraction():
    crawl = make_crawl()
    fields, steps = baseline_fields(crawl)
    by_label = {x.label: x for x in fields}
    assert by_label["Date of Birth"].format_hint == "DD/MM/YYYY"
    assert by_label["Captcha"].field_type is FieldType.CAPTCHA
    assert by_label["District"].depends_on == by_label["State"].key
    assert by_label["Gender"].in_iframe
    assert steps[0].kind is StepKind.LOGIN and steps[1].kind is StepKind.FORM
    docs = baseline_documents(crawl, fields)
    photo = next(iter(docs.values()))
    assert photo.kind is DocumentKind.PHOTO
    assert photo.accepted_formats == ["jpg"]
    assert photo.max_size_kb == 200 and photo.min_size_kb == 20


def test_valid_profile_ref():
    assert valid_profile_ref("profile.dob|day") == "profile.dob|day"
    assert valid_profile_ref("profile.address.district") == "profile.address.district"
    assert valid_profile_ref("profile.father_name") is None
    assert valid_profile_ref("profile.dob|weird") is None
    assert valid_profile_ref("os.environ") is None


async def test_build_form_plan_merges_llm_and_dom():
    llm = FakeLLM({"plan_form": plan_out})
    plan = await build_form_plan(make_crawl(), llm=llm, service_name="Demo", today=date(2026, 10, 30))
    fields = {x.key: x for x in plan.all_fields}
    assert fields["s1.f0"].profile_ref == "profile.name"
    assert fields["s1.f2"].profile_ref == "profile.dob|day" and fields["s1.f2"].required
    assert fields["s1.f5"].profile_ref is None and fields["s1.f5"].answer_key == "father_name"
    assert fields["s1.f6"].profile_ref is None  # file inputs are never mapped to values
    assert "zz.bogus" not in fields
    assert plan.steps[0].kind is StepKind.LOGIN
    assert [s.observed for s in plan.steps] == [True, True, False]
    assert any(x.label == "Bank account number" and not x.observed for x in plan.all_fields)
    docs = {d.kind: d for d in plan.required_documents}
    assert docs[DocumentKind.PHOTO].max_size_kb == 200  # DOM limit kept
    assert docs[DocumentKind.PHOTO].name == "Recent passport-size photograph"
    assert docs[DocumentKind.INCOME_CERTIFICATE].accepted_formats == ["pdf"]
    kinds = {o.kind for o in plan.known_obstacles}
    assert {ObstacleKind.CAPTCHA, ObstacleKind.LOGIN, ObstacleKind.CASCADING_DROPDOWN, ObstacleKind.IFRAME,
            ObstacleKind.FILE_UPLOAD, ObstacleKind.DECLARATION, ObstacleKind.REGISTRATION,
            ObstacleKind.SESSION_TIMEOUT} <= kinds
    assert plan.login_required and plan.registration_required
    assert plan.deadline == date(2026, 12, 31)
    assert plan.eligibility_rules[0].reference_date == date(2026, 7, 1)
    assert plan.form_version_hash and plan.estimated_steps == 3
    # Untrusted page text goes to the model inside a delimited block.
    assert "<instruction_pages>" in llm.user_content_for("plan_form")


def test_plan_has_no_values_only_structure():
    crawl = make_crawl()
    blob = str(crawl.to_dict())
    assert "value" not in {k for p in crawl.form_pages for fld in p.fields for k in fld}
    assert "Priya" not in blob


class FakeCrawler:
    def __init__(self, crawl):
        self.crawl_result = crawl
        self.calls = []

    async def crawl(self, url, include_info=True):
        self.calls.append(include_info)
        return self.crawl_result


async def test_planner_uses_cache_and_detects_change(settings):
    cache = PlanCache(settings.plan_cache_path)
    crawl = make_crawl()
    llm = FakeLLM({"plan_form": [plan_out(None), plan_out(None)]})
    crawler = FakeCrawler(crawl)
    planner = FormPlanner(settings, llm=llm, cache=cache, crawler=crawler)

    first = await planner.plan("https://portal.gov.in/?utm_source=x", service_name="Demo")
    assert not first.from_cache and crawler.calls == [True]

    second = await planner.plan("https://portal.gov.in/", service_name="Demo")
    assert second.from_cache and len(llm.calls) == 1
    assert crawler.calls[-1] is False  # quick structural crawl only

    crawl.form_pages[1].fields.append(f("New mandatory field", required=True))
    third = await planner.plan("https://portal.gov.in/")
    assert not third.from_cache and third.changed_since_last_plan
    assert len(cache.history("https://portal.gov.in/")) == 2


def test_normalize_portal_url():
    assert normalize_portal_url("HTTPS://Portal.GOV.in/apply/;jsessionid=ABC?b=2&a=1&utm_source=x#top") == \
        "https://portal.gov.in/apply?a=1&b=2"
    assert normalize_portal_url("http://localhost:5001/") == "http://localhost:5001/"


@pytest.mark.parametrize("n", [0])
def test_cache_rejects_plan_without_hash(settings, n):
    from app.models import FormPlan

    with pytest.raises(ValueError):
        PlanCache(settings.plan_cache_path).put(FormPlan(portal_url="https://x.gov.in/"))
