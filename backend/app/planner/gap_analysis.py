"""Compare a FormPlan with the citizen's profile and documents, and ask for
everything that is missing in ONE batch before the agent starts.

Honesty rule: eligibility problems are reported plainly and the citizen
decides. Nothing here ever changes a fact to make someone fit a rule.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..llm import LLM
from ..models import (
    Category,
    CitizenProfile,
    DocumentIssue,
    DocumentProblem,
    DocumentRef,
    DocumentRequirement,
    EligibilityKind,
    EligibilityRule,
    EligibilityWarning,
    FieldSource,
    FieldType,
    FormPlan,
    GapQuestion,
    GapReport,
    InvalidProfileReference,
    MissingField,
    MissingProfileData,
    PlanField,
    QuestionKind,
    ServiceRequest,
    StepKind,
    WarningSeverity,
    enum_label,
)

logger = logging.getLogger(__name__)

SKIP_TYPES = {FieldType.CAPTCHA, FieldType.OTP, FieldType.PASSWORD, FieldType.FILE}
DECLARATION_RE = re.compile(r"declar|undertak|i agree|hereby|terms|consent|घोषणा|सहमत", re.I)
IMAGE_FORMATS = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp"}
_MIME_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "application/pdf": "pdf", "image/gif": "gif",
    "image/bmp": "bmp", "image/tiff": "tiff", "image/webp": "webp",
}
_YES = {"yes", "y", "haan", "han", "ha", "हाँ", "हां", "true", "1", "ji", "जी"}
_NO = {"no", "n", "nahi", "nahin", "नहीं", "false", "0"}
CONFIRMATIONS_KEY = "eligibility_confirmations"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
    return s if s and s[0].isalpha() else f"field_{s}" if s else "field"


def _ref_target(ref: str) -> str:
    return ref.split("|", 1)[0].strip()


def _field_prompt(f: PlanField) -> str:
    prompt = f"What is your {f.label.rstrip(':* ').strip()}?"
    if f.format_hint:
        prompt += f" (format {f.format_hint})"
    return prompt


def _has_extra(request: ServiceRequest, key: str) -> bool:
    v = request.extra_params.get(key)
    return v not in (None, "", [])


def doc_format(doc: DocumentRef) -> str:
    ext = Path(doc.filename).suffix.lower().lstrip(".")
    ext = {"jpeg": "jpg", "tif": "tiff"}.get(ext, ext)
    return ext or _MIME_EXT.get(doc.mime_type.lower(), "")


def conversion_possible(src: str, accepted: list[str]) -> str | None:
    """Describe the automatic fix, or None if it cannot be done automatically."""
    acc = {a.replace("jpeg", "jpg") for a in accepted}
    if src in IMAGE_FORMATS:
        for target in ("jpg", "png", "pdf"):
            if target in acc:
                return f"convert the image to {target.upper()}"
    return None


def _match_document(docs: list[DocumentRef], req: DocumentRequirement) -> DocumentRef | None:
    return next((d for d in docs if d.doc_key == req.key), None) or next(
        (d for d in docs if d.kind == req.kind and req.kind.value != "other"), None
    )


def _check_document(req: DocumentRequirement, doc: DocumentRef | None) -> DocumentIssue | None:
    limits = []
    if req.accepted_formats:
        limits.append("/".join(f.upper() for f in req.accepted_formats))
    if req.max_size_kb:
        limits.append(f"max {req.max_size_kb} KB")
    hint = f" ({', '.join(limits)})" if limits else ""
    if doc is None:
        return DocumentIssue(requirement_key=req.key, name=req.name, problem=DocumentProblem.MISSING,
                             detail=f"Please upload: {req.name}{hint}.")
    fmt = doc_format(doc)
    accepted = [a.replace("jpeg", "jpg") for a in req.accepted_formats]
    if accepted and fmt not in accepted:
        fix = conversion_possible(fmt, accepted)
        return DocumentIssue(
            requirement_key=req.key, name=req.name, problem=DocumentProblem.WRONG_FORMAT,
            detail=f"{doc.filename} is {fmt.upper() or 'an unknown type'}; the portal accepts {', '.join(accepted).upper()}.",
            can_auto_convert=fix is not None, conversion=fix,
        )
    size_kb = doc.size_bytes / 1024
    if req.max_size_kb and size_kb > req.max_size_kb:
        can = fmt in IMAGE_FORMATS or fmt == "pdf"
        return DocumentIssue(
            requirement_key=req.key, name=req.name, problem=DocumentProblem.TOO_LARGE,
            detail=f"{doc.filename} is {size_kb:.0f} KB; the limit is {req.max_size_kb} KB.",
            can_auto_convert=can, conversion=f"compress to under {req.max_size_kb} KB" if can else None,
        )
    if req.min_size_kb and size_kb < req.min_size_kb:
        return DocumentIssue(
            requirement_key=req.key, name=req.name, problem=DocumentProblem.TOO_SMALL,
            detail=f"{doc.filename} is {size_kb:.0f} KB; the portal needs at least {req.min_size_kb} KB "
                   "(usually a clearer, higher-resolution scan).",
        )
    return None


def _age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _check_rule(rule: EligibilityRule, profile: CitizenProfile, today: date) -> EligibilityWarning | None:
    """Return a warning, or None when the profile clearly satisfies the rule."""
    W = lambda sev, msg: EligibilityWarning(kind=rule.kind, severity=sev, message=msg)  # noqa: E731
    k = rule.kind
    if k is EligibilityKind.AGE:
        dob = profile.dob.value
        if dob is None:
            return W(WarningSeverity.UNVERIFIED, f"I could not check the age rule ({rule.description}) without your date of birth.")
        on = rule.reference_date or today
        age = _age_on(dob, on)
        if rule.min_value is not None and age < rule.min_value:
            return W(WarningSeverity.BLOCKER, f"You are {age} on {on:%d/%m/%Y}; the minimum age is {rule.min_value:g}.")
        if rule.max_value is not None and age > rule.max_value:
            return W(WarningSeverity.BLOCKER, f"You are {age} on {on:%d/%m/%Y}; the maximum age is {rule.max_value:g}.")
        return None
    if k is EligibilityKind.INCOME:
        inc = profile.income.value
        if inc is None:
            return W(WarningSeverity.UNVERIFIED, f"I could not check the income limit ({rule.description}) without your annual family income.")
        if rule.max_value is not None and inc > rule.max_value:
            return W(WarningSeverity.BLOCKER, f"Your annual income Rs {inc:,} is above the limit of Rs {rule.max_value:,.0f}.")
        if rule.min_value is not None and inc < rule.min_value:
            return W(WarningSeverity.BLOCKER, f"Your annual income Rs {inc:,} is below the minimum of Rs {rule.min_value:,.0f}.")
        return None
    if k is EligibilityKind.CATEGORY and rule.allowed_values:
        cat = profile.category.value
        if cat is None:
            return W(WarningSeverity.UNVERIFIED, f"I could not check the category rule ({rule.description}).")
        allowed = {_norm(v) for v in rule.allowed_values}
        ok = _norm(cat.value) in allowed or _norm(enum_label(cat)) in allowed or (
            cat is Category.GENERAL_EWS and "general" in allowed)
        if not ok:
            return W(WarningSeverity.BLOCKER, f"This scheme is for {', '.join(rule.allowed_values)}; your category is {enum_label(cat)}.")
        return None
    if k is EligibilityKind.DOMICILE and rule.allowed_values:
        state = profile.address.value.state if profile.address.value else None
        if not state:
            return W(WarningSeverity.UNVERIFIED, f"I could not check the residence rule ({rule.description}) without your state.")
        if _norm(state) not in {_norm(v) for v in rule.allowed_values}:
            return W(WarningSeverity.BLOCKER, f"This scheme is for residents of {', '.join(rule.allowed_values)}; your address is in {state}.")
        return None
    if k is EligibilityKind.GENDER and rule.allowed_values:
        g = profile.gender.value
        if g is None:
            return W(WarningSeverity.UNVERIFIED, f"I could not check the gender rule ({rule.description}).")
        if _norm(g.value) not in {_norm(v) for v in rule.allowed_values}:
            return W(WarningSeverity.BLOCKER, f"This scheme is only for {', '.join(rule.allowed_values)} applicants.")
        return None
    return W(WarningSeverity.UNVERIFIED, f"Please confirm you meet this condition: {rule.description}")


def analyze_gaps(plan: FormPlan, profile: CitizenProfile, request: ServiceRequest, *,
                 today: date | None = None) -> GapReport:
    today = today or date.today()
    login_steps = {s.index for s in plan.steps if s.kind is StepKind.LOGIN}
    report = GapReport()
    seen_targets: set[str] = set()
    qn = 0

    def ask(kind: QuestionKind, prompt: str, target: str, options: list[str] | None = None,
            fmt: str | None = None) -> None:
        nonlocal qn
        if target in seen_targets:
            return
        seen_targets.add(target)
        qn += 1
        report.questions_for_user.append(
            GapQuestion(id=f"q{qn}", kind=kind, prompt=prompt, target=target, options=options or [], format_hint=fmt)
        )

    for f in plan.required_fields:
        if f.field_type in SKIP_TYPES or f.step_index in login_steps:
            continue
        if f.field_type is FieldType.CHECKBOX and DECLARATION_RE.search(f.label):
            continue  # confirmed by the citizen at submit time
        target: str
        reason: str
        if f.profile_ref:
            try:
                profile.resolve_reference(f.profile_ref)
                continue
            except MissingProfileData:
                target, reason = _ref_target(f.profile_ref), "not in your profile yet"
            except InvalidProfileReference:
                target, reason = f"extra.{f.answer_key or _slug(f.label)}", "needed by the form"
        else:
            key = f.answer_key or _slug(f.label)
            if _has_extra(request, key):
                continue
            target, reason = f"extra.{key}", "needed by the form"
        if target in seen_targets:
            continue
        report.missing_fields.append(MissingField(key=f.key, label=f.label, reason=reason))
        opts = f.options if f.field_type in (FieldType.SELECT, FieldType.RADIO) and 0 < len(f.options) <= 30 else []
        ask(QuestionKind.FIELD, _field_prompt(f), target, opts, f.format_hint)

    for req in plan.required_documents:
        if not req.required:
            continue
        issue = _check_document(req, _match_document(profile.documents, req))
        if issue is None:
            continue
        report.missing_documents.append(issue)
        if not issue.can_auto_convert:
            ask(QuestionKind.DOCUMENT, issue.detail, f"doc.{req.key}")

    confirmations: dict[str, Any] = request.extra_params.get(CONFIRMATIONS_KEY, {}) or {}
    for i, rule in enumerate(plan.eligibility_rules):
        w = _check_rule(rule, profile, today)
        if w is None:
            continue
        answered = str(confirmations.get(str(i), "")).lower()
        if w.severity is WarningSeverity.UNVERIFIED and answered == "yes":
            continue
        if w.severity is WarningSeverity.UNVERIFIED and answered == "no":
            w = EligibilityWarning(kind=w.kind, severity=WarningSeverity.BLOCKER,
                                   message=f"You said you do not meet: {rule.description}")
        report.eligibility_warnings.append(w)
        if w.severity is WarningSeverity.UNVERIFIED:
            ask(QuestionKind.ELIGIBILITY, f"Do you meet this condition: {rule.description}?",
                f"eligibility.{i}", ["Yes", "No"])

    report.summary_en, report.summary_hi = summarize(report)
    return report


def summarize(report: GapReport) -> tuple[str, str]:
    n_fields = sum(q.kind is QuestionKind.FIELD for q in report.questions_for_user)
    n_docs = sum(q.kind is QuestionKind.DOCUMENT for q in report.questions_for_user)
    n_conf = sum(q.kind is QuestionKind.ELIGIBILITY for q in report.questions_for_user)
    blockers = [w for w in report.eligibility_warnings if w.severity is WarningSeverity.BLOCKER]
    auto = [d for d in report.missing_documents if d.can_auto_convert]

    en: list[str] = []
    hi: list[str] = []
    if blockers:
        en.append("Based on the scheme rules you may not be eligible: " + " ".join(w.message for w in blockers)
                  + " You can still decide whether to continue.")
        hi.append("योजना के नियमों के अनुसार आप शायद पात्र नहीं हैं: " + " ".join(w.message for w in blockers)
                  + " आगे बढ़ना है या नहीं, यह आप तय करें।")
    if n_fields or n_docs or n_conf:
        parts = []
        if n_fields:
            parts.append(f"{n_fields} more detail{'s' if n_fields != 1 else ''}")
        if n_docs:
            parts.append(f"{n_docs} document{'s' if n_docs != 1 else ''}")
        if n_conf:
            parts.append(f"{n_conf} confirmation{'s' if n_conf != 1 else ''}")
        en.append(f"I need {' and '.join(parts)} before I can start.")
        hi_parts = []
        if n_fields:
            hi_parts.append(f"{n_fields} और जानकारी")
        if n_docs:
            hi_parts.append(f"{n_docs} दस्तावेज़")
        if n_conf:
            hi_parts.append(f"{n_conf} पुष्टि")
        hi.append(f"शुरू करने से पहले मुझे {' और '.join(hi_parts)} चाहिए।")
    else:
        en.append("I have everything I need to start.")
        hi.append("शुरू करने के लिए मेरे पास सब कुछ है।")
    if auto:
        en.append(f"I will automatically fix {len(auto)} document(s): " + "; ".join(f"{d.name}: {d.conversion}" for d in auto) + ".")
        hi.append(f"मैं {len(auto)} दस्तावेज़ अपने-आप ठीक कर दूँगा।")
    return " ".join(en), " ".join(hi)


# ---------------------------------------------------------------------------
# Applying the citizen's answers
# ---------------------------------------------------------------------------


@dataclass
class AnswerResult:
    profile: CitizenProfile
    request: ServiceRequest
    errors: dict[str, str] = field(default_factory=dict)  # question id -> problem
    applied: list[str] = field(default_factory=list)


def _yes_no(text: str) -> str | None:
    t = text.strip().lower().rstrip(".!")
    if t in _YES:
        return "yes"
    if t in _NO:
        return "no"
    return None


def apply_answers(report: GapReport, answers: dict[str, str], profile: CitizenProfile,
                  request: ServiceRequest) -> AnswerResult:
    """Write answers into the profile (as user edits) or extra_params.

    Each answer is validated on its own, so one bad value does not discard
    the others; its error is returned for a follow-up question.
    """
    result = AnswerResult(profile=profile, request=request.model_copy(deep=True))
    for q in report.questions_for_user:
        raw = answers.get(q.id)
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        if q.options and q.kind is not QuestionKind.ELIGIBILITY:
            match = next((o for o in q.options if o.lower() == text.lower()), None)
            if match is None:
                result.errors[q.id] = f"Please choose one of: {', '.join(q.options)}"
                continue
            text = match
        if q.target.startswith("profile."):
            try:
                result.profile = _set_profile_value(result.profile, q.target, text)
                result.applied.append(q.id)
            except (ValidationError, ValueError) as e:
                msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
                result.errors[q.id] = f"That doesn't look right: {msg}"
        elif q.target.startswith("extra."):
            result.request.extra_params[q.target.split(".", 1)[1]] = text
            result.applied.append(q.id)
        elif q.target.startswith("eligibility."):
            yn = _yes_no(text)
            if yn is None:
                result.errors[q.id] = "Please answer yes or no."
                continue
            result.request.extra_params.setdefault(CONFIRMATIONS_KEY, {})[q.target.split(".", 1)[1]] = yn
            result.applied.append(q.id)
        # doc.* questions are answered by uploading a file.
    return result


def _set_profile_value(profile: CitizenProfile, target: str, value: str) -> CitizenProfile:
    parts = target.split(".")
    if len(parts) not in (2, 3) or parts[1] not in CitizenProfile.FIELD_NAMES:
        raise ValueError(f"cannot set {target}")
    data = profile.model_dump(mode="json")
    name = parts[1]
    entry = {"confidence": 1.0, "source": FieldSource.USER_EDIT.value, "conflict_note": None}
    if len(parts) == 2:
        data[name] = {**entry, "value": value}
    else:
        current = (data.get(name) or {}).get("value") or {}
        data[name] = {**entry, "value": {**current, parts[2]: value}}
    return CitizenProfile.model_validate(data)


class _Answer(BaseModel):
    question_id: str
    answer: str | None


class _Answers(BaseModel):
    answers: list[_Answer]


PARSE_SYSTEM = """A citizen replied in chat or by voice (any language, often Hindi, English or a mix) to a batch
of questions. Extract the answer to each question id. Copy values exactly as the citizen gave them; for
questions with options return the exact option text that matches; for yes/no questions return "Yes" or "No".
Use null when a question was not answered. Never invent or guess values."""


async def parse_batch_reply(text: str, questions: list[GapQuestion], llm: LLM,
                            job_id: str | None = None) -> dict[str, str]:
    if not questions or not text.strip():
        return {}
    listing = "\n".join(
        f"- {q.id}: {q.prompt}" + (f" Options: {' | '.join(q.options)}" if q.options else "")
        for q in questions if q.kind is not QuestionKind.DOCUMENT
    )
    out = await llm.structured(
        _Answers, purpose="parse_batch_reply", system=PARSE_SYSTEM,
        user_content=f"Questions:\n{listing}\n\nCitizen's reply:\n<reply>{text}</reply>",
        max_tokens=2048, effort="low", job_id=job_id,
    )
    valid = {q.id for q in questions}
    return {a.question_id: a.answer for a in out.answers if a.question_id in valid and a.answer}


# ---------------------------------------------------------------------------
# Document conversion / compression
# ---------------------------------------------------------------------------


class ConversionError(ValueError):
    pass


@dataclass
class ConvertedDocument:
    data: bytes
    mime_type: str
    filename: str
    note: str


def convert_for_requirement(data: bytes, mime_type: str, filename: str,
                            req: DocumentRequirement) -> ConvertedDocument:
    """Re-encode, resize or compress a document to meet the portal's rules."""
    from PIL import Image

    src = Path(filename).suffix.lower().lstrip(".").replace("jpeg", "jpg") or _MIME_EXT.get(mime_type, "")
    accepted = [a.replace("jpeg", "jpg") for a in req.accepted_formats] or [src]
    max_bytes = req.max_size_kb * 1024 if req.max_size_kb else None
    stem = Path(filename).stem or req.key

    if src == "pdf":
        if "pdf" not in accepted:
            raise ConversionError("A PDF cannot be turned into an image automatically; please upload a photo or scan.")
        if max_bytes is None or len(data) <= max_bytes:
            return ConvertedDocument(data, "application/pdf", f"{stem}.pdf", "unchanged")
        out = _compress_pdf(data, max_bytes)
        return ConvertedDocument(out, "application/pdf", f"{stem}.pdf", f"compressed to {len(out) // 1024} KB")

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ConversionError("The file is not a readable image or PDF.") from e

    if src in accepted and (max_bytes is None or len(data) <= max_bytes):
        return ConvertedDocument(data, mime_type, filename, "unchanged")
    if "jpg" in accepted:
        out = _jpeg_under(img, max_bytes)
        return ConvertedDocument(out, "image/jpeg", f"{stem}.jpg", f"saved as JPEG, {len(out) // 1024} KB")
    if "png" in accepted:
        out = _png_under(img, max_bytes)
        return ConvertedDocument(out, "image/png", f"{stem}.png", f"saved as PNG, {len(out) // 1024} KB")
    if "pdf" in accepted:
        out = _image_pdf_under(img, max_bytes)
        return ConvertedDocument(out, "application/pdf", f"{stem}.pdf", f"converted to PDF, {len(out) // 1024} KB")
    raise ConversionError(f"Cannot convert {src.upper()} to {', '.join(accepted).upper()}.")


def _rgb(img):
    from PIL import Image

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img


def _shrink(img, factor: float = 0.85):
    w, h = img.size
    return img.resize((max(1, int(w * factor)), max(1, int(h * factor))))


def _jpeg_under(img, max_bytes: int | None) -> bytes:
    img = _rgb(img)
    while True:
        for q in (90, 80, 70, 60, 50, 40, 35):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, optimize=True)
            if max_bytes is None or buf.tell() <= max_bytes:
                return buf.getvalue()
        if min(img.size) < 120:
            raise ConversionError("Could not compress the image enough without making it unreadable.")
        img = _shrink(img)


def _png_under(img, max_bytes: int | None) -> bytes:
    while True:
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        if max_bytes is None or buf.tell() <= max_bytes:
            return buf.getvalue()
        if min(img.size) < 120:
            raise ConversionError("Could not compress the image enough without making it unreadable.")
        img = _shrink(img)


def _image_pdf_under(img, max_bytes: int | None) -> bytes:
    img = _rgb(img)
    while True:
        buf = io.BytesIO()
        img.save(buf, "PDF", resolution=150.0, quality=75)
        if max_bytes is None or buf.tell() <= max_bytes:
            return buf.getvalue()
        if min(img.size) < 200:
            raise ConversionError("Could not make the PDF small enough.")
        img = _shrink(img)


def _compress_pdf(data: bytes, max_bytes: int) -> bytes:
    from pypdf import PdfReader, PdfWriter

    best = data
    for quality in (70, 50, 30):
        try:
            writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
            for page in writer.pages:
                for image in page.images:
                    try:
                        image.replace(image.image, quality=quality)
                    except Exception:
                        continue
                page.compress_content_streams()
            writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
            buf = io.BytesIO()
            writer.write(buf)
        except Exception as e:
            raise ConversionError("This PDF could not be compressed automatically.") from e
        if buf.tell() < len(best):
            best = buf.getvalue()
        if len(best) <= max_bytes:
            return best
    raise ConversionError(
        f"The PDF is still {len(best) // 1024} KB after compression; please upload a smaller scan "
        f"(under {max_bytes // 1024} KB)."
    )
