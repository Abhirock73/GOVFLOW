"""Core domain models shared by intake, agent, graph and API layers.

Every citizen-profile attribute is wrapped in a :class:`ProfileField`, which
records the value together with how confident we are in it and where it came
from (voice note, ID photo, or a direct user edit).

The agent never sees raw profile values. It refers to them symbolically as
``profile.<field>[.<subfield>][|<format>]`` and the server resolves them via
:meth:`CitizenProfile.resolve_reference` at action time.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Generic, TypeVar
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
)

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FieldSource(str, Enum):
    VOICE = "voice"
    ID_PHOTO = "id_photo"
    USER_EDIT = "user_edit"


class IdType(str, Enum):
    AADHAAR = "aadhaar"
    PAN = "pan"
    VOTER_ID = "voter_id"
    DRIVING_LICENSE = "driving_license"
    PASSPORT = "passport"
    RATION_CARD = "ration_card"
    OTHER = "other"


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    TRANSGENDER = "transgender"
    OTHER = "other"


class Category(str, Enum):
    GENERAL = "general"
    GENERAL_EWS = "general_ews"
    OBC = "obc"
    SC = "sc"
    ST = "st"
    OTHER = "other"


class InputMode(str, Enum):
    LINK = "link"  # the citizen pasted a URL
    NAME = "name"  # the citizen named a service in plain language


class DocumentKind(str, Enum):
    PHOTO = "photo"
    ID_PROOF = "id_proof"
    INCOME_CERTIFICATE = "income_certificate"
    CASTE_CERTIFICATE = "caste_certificate"
    ADDRESS_PROOF = "address_proof"
    MARKSHEET = "marksheet"
    BANK_PASSBOOK = "bank_passbook"
    SIGNATURE = "signature"
    OTHER = "other"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT_OPTION = "select_option"
    CHECK = "check"
    UPLOAD_FILE = "upload_file"
    SCROLL = "scroll"
    PRESS_KEY = "press_key"
    WAIT = "wait"
    GO_BACK = "go_back"
    CLICK_XY = "click_xy"
    ASK_HUMAN = "ask_human"
    SAVE_PROGRESS = "save_progress"
    FINISH = "finish"


class StepStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"  # rejected by a guard before execution
    WAITING_HUMAN = "waiting_human"


class HumanRequestKind(str, Enum):
    OTP = "otp"
    CAPTCHA = "captcha"
    AMBIGUOUS_CHOICE = "ambiguous_choice"
    MISSING_DATA = "missing_data"
    CONFIRM_SUBMIT = "confirm_submit"
    CONFIRM_PROFILE = "confirm_profile"
    LOGIN = "login"
    REGISTRATION = "registration"
    CONFIRM_PORTAL = "confirm_portal"
    COLLECT_MISSING = "collect_missing"


class JobStatus(str, Enum):
    CREATED = "created"
    RESOLVING = "resolving"
    AWAITING_PORTAL_CONFIRMATION = "awaiting_portal_confirmation"
    PLANNING = "planning"
    AWAITING_MISSING_DATA = "awaiting_missing_data"
    INTAKE = "intake"
    AWAITING_PROFILE_CONFIRMATION = "awaiting_profile_confirmation"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    PAUSED = "paused"  # human wait timed out; resumable
    AWAITING_SUBMIT_CONFIRMATION = "awaiting_submit_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Lenient parsers (voice and OCR output is messy)
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}


def _safe_date(y: int, mo: int, d: int, original: Any) -> Any:
    try:
        return date(y, mo, d)
    except ValueError:  # e.g. 31/31/2004: let pydantic report a normal error
        return original


def parse_date_flexible(v: Any) -> Any:
    """Parse common Indian date spellings; assumes day-first when ambiguous.

    Accepts ``date``/``datetime``, ISO ``YYYY-MM-DD``, ``DD/MM/YYYY``,
    ``DD-MM-YYYY``, ``DD.MM.YYYY`` and ``15 Aug 2004`` / ``Aug 15, 2004``.
    Anything else is passed through so pydantic raises a normal error.
    """
    if v is None or isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        return _safe_date(y, mo, d, v)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        d, mo, y = map(int, m.groups())
        return _safe_date(y, mo, d, v)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]+)\.?,?[\s\-]+(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), v)
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), v)
    return v


def parse_income(v: Any) -> Any:
    """Parse ``"2,50,000"``, ``"₹ 250000"``, ``"2.5 lakh"`` into an int (INR)."""
    if v is None or isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, float):
        return int(round(v))
    if not isinstance(v, str):
        return v
    s = v.strip().lower().replace("₹", "").replace("rs.", "").replace("rs", "").replace("inr", "")
    s = s.replace(",", "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(lakh|lakhs|lac|lacs|l|k|thousand|crore|cr)?", s)
    if not m:
        return v
    n = float(m.group(1))
    mult = {
        None: 1,
        "k": 1_000,
        "thousand": 1_000,
        "l": 100_000,
        "lac": 100_000,
        "lacs": 100_000,
        "lakh": 100_000,
        "lakhs": 100_000,
        "cr": 10_000_000,
        "crore": 10_000_000,
    }[m.group(2)]
    return int(round(n * mult))


def _normalise_enum_text(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"[\s\-()]+", "_", v.strip().lower()).strip("_")
    return v


_CATEGORY_ALIASES = {
    "gen": "general",
    "open": "general",
    "ur": "general",
    "unreserved": "general",
    "ews": "general_ews",
    "general_ews": "general_ews",
    "obc_ncl": "obc",
    "bc": "obc",
    "sc_st": "sc",
}

_GENDER_ALIASES = {"m": "male", "f": "female", "t": "transgender", "tg": "transgender"}

_ID_TYPE_ALIASES = {
    "aadhar": "aadhaar",
    "uid": "aadhaar",
    "pan_card": "pan",
    "voter": "voter_id",
    "epic": "voter_id",
    "dl": "driving_license",
    "driving_licence": "driving_license",
}


def _alias(aliases: dict[str, str]):
    def inner(v: Any) -> Any:
        v = _normalise_enum_text(v)
        return aliases.get(v, v) if isinstance(v, str) else v

    return inner


FlexDate = Annotated[date, BeforeValidator(parse_date_flexible)]
FlexIncome = Annotated[int, BeforeValidator(parse_income)]
FlexCategory = Annotated[Category, BeforeValidator(_alias(_CATEGORY_ALIASES))]
FlexGender = Annotated[Gender, BeforeValidator(_alias(_GENDER_ALIASES))]
FlexIdType = Annotated[IdType, BeforeValidator(_alias(_ID_TYPE_ALIASES))]


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class ProfileField(BaseModel, Generic[T]):
    """A single profile value plus its provenance."""

    model_config = ConfigDict(frozen=False)

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: FieldSource = FieldSource.USER_EDIT
    # Set by the profile builder when voice and ID disagree; shown to the user.
    conflict_note: str | None = None

    @property
    def is_set(self) -> bool:
        return self.value is not None and self.value != ""

    def needs_confirmation(self, threshold: float) -> bool:
        return self.is_set and (self.confidence < threshold or self.conflict_note is not None)


class Address(BaseModel):
    line1: str | None = None
    line2: str | None = None
    village_or_city: str | None = None
    block: str | None = None  # tehsil / mandal / taluka
    district: str | None = None
    state: str | None = None
    pincode: str | None = None

    @field_validator("pincode", mode="before")
    @classmethod
    def _pincode(cls, v: Any) -> Any:
        if v is None:
            return v
        digits = re.sub(r"\D", "", str(v))
        if digits and len(digits) != 6:
            raise ValueError("pincode must have 6 digits")
        return digits or None

    def one_line(self) -> str:
        parts = [self.line1, self.line2, self.village_or_city, self.block, self.district, self.state]
        s = ", ".join(p for p in parts if p)
        return f"{s} - {self.pincode}" if self.pincode else s


class BankDetails(BaseModel):
    account_holder: str | None = None
    account_number: str | None = None
    ifsc: str | None = None
    bank_name: str | None = None
    branch: str | None = None

    @field_validator("account_number", mode="before")
    @classmethod
    def _account_number(cls, v: Any) -> Any:
        if v is None:
            return v
        digits = re.sub(r"[\s\-]", "", str(v))
        if digits and not re.fullmatch(r"\d{6,20}", digits):
            raise ValueError("account number must be 6-20 digits")
        return digits or None

    @field_validator("ifsc", mode="before")
    @classmethod
    def _ifsc(cls, v: Any) -> Any:
        if v is None:
            return v
        s = re.sub(r"\s", "", str(v)).upper()
        if s and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", s):
            raise ValueError("invalid IFSC code")
        return s or None


class DocumentRef(BaseModel):
    """An uploaded supporting document, stored encrypted under the job.

    ``doc_key`` is what the agent passes to ``upload_file``; it never sees
    the file path.
    """

    doc_key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    kind: DocumentKind
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: FieldSource = FieldSource.USER_EDIT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    uploaded_at: datetime = Field(default_factory=utcnow)


class MissingProfileData(LookupError):
    """Raised when the agent references a profile value that is not set."""

    def __init__(self, reference: str):
        super().__init__(f"profile value not available: {reference}")
        self.reference = reference


class InvalidProfileReference(ValueError):
    """Raised for malformed or unknown ``profile.*`` references."""


_REF_RE = re.compile(r"^profile\.([a-z_]+)(?:\.([a-z0-9_]+))?(?:\|([A-Za-z0-9_/\-.]+))?$")

_DATE_FORMATS = {
    "DD/MM/YYYY": "%d/%m/%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD.MM.YYYY": "%d.%m.%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "iso": "%Y-%m-%d",
}


class CitizenProfile(BaseModel):
    name: ProfileField[str] = Field(default_factory=ProfileField[str])
    dob: ProfileField[FlexDate] = Field(default_factory=ProfileField[FlexDate])
    gender: ProfileField[FlexGender] = Field(default_factory=ProfileField[FlexGender])
    id_type: ProfileField[FlexIdType] = Field(default_factory=ProfileField[FlexIdType])
    id_number: ProfileField[str] = Field(default_factory=ProfileField[str])
    address: ProfileField[Address] = Field(default_factory=ProfileField[Address])
    phone: ProfileField[str] = Field(default_factory=ProfileField[str])
    email: ProfileField[str] = Field(default_factory=ProfileField[str])
    income: ProfileField[FlexIncome] = Field(default_factory=ProfileField[FlexIncome])
    category: ProfileField[FlexCategory] = Field(default_factory=ProfileField[FlexCategory])
    bank_details: ProfileField[BankDetails] = Field(default_factory=ProfileField[BankDetails])
    documents: list[DocumentRef] = Field(default_factory=list)

    # Names of the ProfileField attributes, in display order.
    FIELD_NAMES: ClassVar[tuple[str, ...]] = (
        "name",
        "dob",
        "gender",
        "id_type",
        "id_number",
        "address",
        "phone",
        "email",
        "income",
        "category",
        "bank_details",
    )

    @field_validator("id_number", mode="after")
    @classmethod
    def _norm_id(cls, f: ProfileField[str]) -> ProfileField[str]:
        if isinstance(f.value, str):
            f.value = re.sub(r"[\s\-]", "", f.value).upper() or None
        return f

    @field_validator("phone", mode="after")
    @classmethod
    def _norm_phone(cls, f: ProfileField[str]) -> ProfileField[str]:
        if isinstance(f.value, str):
            digits = re.sub(r"\D", "", f.value)
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if digits and not re.fullmatch(r"[6-9]\d{9}", digits):
                raise ValueError("phone must be a 10-digit Indian mobile number")
            f.value = digits or None
        return f

    @field_validator("email", mode="after")
    @classmethod
    def _norm_email(cls, f: ProfileField[str]) -> ProfileField[str]:
        if isinstance(f.value, str):
            s = f.value.strip().lower()
            if s and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
                raise ValueError("invalid email address")
            f.value = s or None
        return f

    # -- helpers ------------------------------------------------------------

    def get_field(self, name: str) -> ProfileField[Any]:
        if name not in self.FIELD_NAMES:
            raise InvalidProfileReference(f"unknown profile field: {name}")
        return getattr(self, name)

    def fields_needing_confirmation(self, threshold: float) -> list[str]:
        return [n for n in self.FIELD_NAMES if self.get_field(n).needs_confirmation(threshold)]

    def missing_fields(self, required: list[str] | None = None) -> list[str]:
        names = required if required is not None else list(self.FIELD_NAMES)
        return [n for n in names if not self.get_field(n).is_set]

    def document(self, doc_key: str) -> DocumentRef | None:
        return next((d for d in self.documents if d.doc_key == doc_key), None)

    def add_document(self, ref: DocumentRef) -> None:
        self.documents = [d for d in self.documents if d.doc_key != ref.doc_key]
        self.documents.append(ref)

    def masked_summary(self) -> dict[str, str]:
        from .security import mask_profile
        return mask_profile(self)

    def available_references(self) -> list[str]:
        """Symbolic references the agent may use, for the system prompt.

        Only references whose value is set are listed, so the model can tell
        what is missing without seeing any value.
        """
        refs: list[str] = []
        for n in self.FIELD_NAMES:
            f = self.get_field(n)
            if not f.is_set:
                continue
            if isinstance(f.value, BaseModel):
                refs.extend(
                    f"profile.{n}.{k}" for k, v in f.value.model_dump().items() if v not in (None, "")
                )
            else:
                refs.append(f"profile.{n}")
        return refs

    def resolve_reference(self, reference: str) -> str:
        """Resolve ``profile.<field>[.<sub>][|<format>]`` to a plain string.

        Formats:
          * dates: ``DD/MM/YYYY`` (default), ``DD-MM-YYYY``, ``YYYY-MM-DD``,
            ``MM/DD/YYYY``, ``day``, ``month``, ``month_name``, ``month_short``,
            ``year`` (for three-dropdown date pickers)
          * strings: ``upper``, ``lower``, ``title``, ``first``, ``last``,
            ``middle`` (name parts), ``last4``
          * enums: ``label`` (human text, e.g. "General (EWS)")
          * address: ``profile.address|one_line``
        """
        m = _REF_RE.match(reference.strip())
        if not m:
            raise InvalidProfileReference(f"malformed profile reference: {reference!r}")
        field_name, sub, fmt = m.groups()
        field = self.get_field(field_name)
        value: Any = field.value
        if value is None:
            raise MissingProfileData(reference)

        if sub is not None:
            if not isinstance(value, BaseModel) or sub not in type(value).model_fields:
                raise InvalidProfileReference(f"unknown sub-field: {field_name}.{sub}")
            value = getattr(value, sub)
            if value in (None, ""):
                raise MissingProfileData(reference)
        elif isinstance(value, Address):
            if fmt in (None, "one_line"):
                return value.one_line()
            raise InvalidProfileReference(f"unsupported format {fmt!r} for address")
        elif isinstance(value, BaseModel):
            raise InvalidProfileReference(f"{field_name} needs a sub-field, e.g. profile.{field_name}.<name>")

        return _format_value(value, fmt, reference)


_ENUM_LABELS: dict[type[Enum], dict[str, str]] = {
    Category: {
        "general": "General",
        "general_ews": "General (EWS)",
        "obc": "OBC",
        "sc": "SC",
        "st": "ST",
    },
    IdType: {
        "aadhaar": "Aadhaar",
        "pan": "PAN",
        "voter_id": "Voter ID",
    },
}


def enum_label(v: Enum) -> str:
    return _ENUM_LABELS.get(type(v), {}).get(v.value) or str(v.value).replace("_", " ").title()


def _format_value(value: Any, fmt: str | None, reference: str) -> str:
    if isinstance(value, date):
        if fmt is None:
            return value.strftime("%d/%m/%Y")
        if fmt in _DATE_FORMATS:
            return value.strftime(_DATE_FORMATS[fmt])
        if fmt == "day":
            return str(value.day)
        if fmt == "month":
            return str(value.month)
        if fmt == "month_name":
            return value.strftime("%B")
        if fmt == "month_short":
            return value.strftime("%b")
        if fmt == "year":
            return str(value.year)
        raise InvalidProfileReference(f"unsupported date format {fmt!r} in {reference}")

    if isinstance(value, Enum):
        if fmt in (None, "value"):
            return str(value.value)
        if fmt == "label":
            return enum_label(value)
        raise InvalidProfileReference(f"unsupported format {fmt!r} in {reference}")

    s = str(value)
    if fmt is None:
        return s
    if fmt == "upper":
        return s.upper()
    if fmt == "lower":
        return s.lower()
    if fmt == "title":
        return s.title()
    if fmt in ("first", "last", "middle"):
        parts = s.split()
        if fmt == "first":
            return parts[0] if parts else ""
        if fmt == "last":
            return parts[-1] if len(parts) > 1 else ""
        return " ".join(parts[1:-1])
    if fmt == "last4":
        return s[-4:]
    raise InvalidProfileReference(f"unsupported format {fmt!r} in {reference}")


# ---------------------------------------------------------------------------
# Service request, steps, human-in-the-loop, receipt
# ---------------------------------------------------------------------------


_URLISH_RE = re.compile(r"^(?:https?://)?(?:[a-z0-9\-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.I)
_LOCAL_URL_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:[/?#]\S*)?$", re.I)


class ServiceRequest(BaseModel):
    """What the citizen asked for: a link, or a service named in plain words.

    ``portal_url`` stays empty for a named service until the resolver finds a
    candidate and the citizen confirms it.
    """

    raw_user_text: str = Field(default="", max_length=2000)
    input_mode: InputMode
    portal_url: HttpUrl | None = None
    service_name: str | None = None
    resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Non-identity answers collected for this service (e.g. institution,
    # course, father_name) plus eligibility confirmations.
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_user_input(cls, text: str) -> "ServiceRequest":
        """Classify free input as a link or a named service."""
        t = (text or "").strip()
        if _LOCAL_URL_RE.match(t) or (" " not in t and _URLISH_RE.match(t)):
            url = t if re.match(r"^https?://", t, re.I) else f"https://{t}"
            return cls(raw_user_text=t, input_mode=InputMode.LINK, portal_url=url)
        return cls(raw_user_text=t, input_mode=InputMode.NAME)

    @property
    def portal_host(self) -> str | None:
        if self.portal_url is None:
            return None
        return (urlparse(str(self.portal_url)).hostname or "").lower() or None


class AgentStep(BaseModel):
    """One observe→act iteration, as shown on the timeline and stored in state.

    ``args`` must already be masked; raw values never belong here.
    """

    job_id: str
    step_no: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=utcnow)
    action: ActionType
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    status: StepStatus = StepStatus.OK
    error: str | None = None
    url: str | None = None
    page_title: str | None = None
    # Short human label for the timeline, e.g. "Filled personal details".
    summary: str | None = None
    screenshot_path: str | None = None
    thumbnail_b64: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class HumanRequest(BaseModel):
    request_id: str = Field(default_factory=new_id)
    kind: HumanRequestKind
    question: str
    options: list[str] | None = None
    # Masked summary of entered fields, for confirm_submit.
    summary: dict[str, str] | None = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None


class HumanReply(BaseModel):
    request_id: str
    # OTP text, chosen option, free-text data, or "yes"/"no".
    text: str | None = None
    approved: bool | None = None
    captcha_solved: bool = False

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class Receipt(BaseModel):
    job_id: str
    reference_no: str = Field(min_length=1, max_length=64)
    timestamp: datetime = Field(default_factory=utcnow)
    portal: str
    service_name: str | None = None
    screenshot_path: Path | None = None
    submitted_fields_masked: dict[str, str] = Field(default_factory=dict)
    summary: str | None = None


class Job(BaseModel):
    """Lightweight job record exposed by the API (no PII)."""

    job_id: str = Field(default_factory=new_id)
    status: JobStatus = JobStatus.CREATED
    service: ServiceRequest | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    step_count: int = 0
    cost_usd: float = 0.0
    pending_request: HumanRequest | None = None
    receipt: Receipt | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Service resolution (Phase 2a)
# ---------------------------------------------------------------------------


class DomainTrust(str, Enum):
    OFFICIAL = "official"  # .gov.in, .nic.in, .gov, gov.<cc>
    LIKELY_OFFICIAL = "likely_official"  # configured official domains, .ac.in, .edu.in, ...
    LOCAL_TEST = "local_test"  # localhost testbed
    UNKNOWN = "unknown"
    SUSPICIOUS = "suspicious"  # lookalike, homograph, gov-imitation, IP host


class DomainAssessment(BaseModel):
    url: str
    host: str
    registered_domain: str
    trust: DomainTrust
    https: bool
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def needs_loud_warning(self) -> bool:
        return self.trust not in (DomainTrust.OFFICIAL, DomainTrust.LOCAL_TEST)


class PageType(str, Enum):
    APPLICATION_FORM = "application_form"
    PORTAL_HOME = "portal_home"  # official portal that leads to the form
    SCHEME_INFO = "scheme_info"  # official scheme page without the form
    LOGIN = "login"
    NEWS = "news"
    AGGREGATOR = "aggregator"  # coaching / "sarkari result" style sites
    LOOKALIKE = "lookalike"
    OTHER = "other"


class SchemeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    NOT_YET_OPEN = "not_yet_open"
    UNKNOWN = "unknown"


class PortalCandidate(BaseModel):
    url: str
    title: str = ""
    domain: DomainAssessment
    page_type: PageType = PageType.OTHER
    scheme_status: SchemeStatus = SchemeStatus.UNKNOWN
    deadline: date | None = None
    asks_payment: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    rejected: bool = False


class SchemeIntent(BaseModel):
    """What the LLM understood from a plain-language request."""

    scheme_name: str
    year: str | None = None
    state: str | None = None
    department: str | None = None
    language: str = "en"
    is_clear: bool = True
    clarifying_question: str | None = None
    search_queries: list[str] = Field(default_factory=list)


class ResolutionResult(BaseModel):
    request: ServiceRequest
    intent: SchemeIntent | None = None
    candidates: list[PortalCandidate] = Field(default_factory=list)
    rejected: list[PortalCandidate] = Field(default_factory=list)
    recommended_index: int | None = None
    needs_user_choice: bool = True
    # Shown before anything else, e.g. "applications closed on 31/08/2026".
    blocking_notice: str | None = None
    message_for_user: str = ""

    @property
    def recommended(self) -> PortalCandidate | None:
        if self.recommended_index is None:
            return None
        return self.candidates[self.recommended_index]


# ---------------------------------------------------------------------------
# Form planning and gap analysis (Phase 2b/2c)
# ---------------------------------------------------------------------------


class FieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    EMAIL = "email"
    TEL = "tel"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    TEXTAREA = "textarea"
    FILE = "file"
    PASSWORD = "password"
    CAPTCHA = "captcha"
    OTP = "otp"
    OTHER = "other"


class PlanField(BaseModel):
    key: str  # stable within the plan, e.g. "s0.f3"
    label: str
    field_type: FieldType = FieldType.TEXT
    required: bool = False
    step_index: int = 0
    observed: bool = True  # False when inferred from instructions only
    options: list[str] = Field(default_factory=list)
    options_total: int = 0  # real count when the list was truncated
    format_hint: str | None = None  # e.g. "DD/MM/YYYY"
    max_length: int | None = None
    in_iframe: bool = False
    depends_on: str | None = None  # key of the field that fills this one's options
    # How to fill it: a profile reference, or a key into extra_params.
    profile_ref: str | None = None
    answer_key: str | None = None
    notes: str | None = None


class DocumentRequirement(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    name: str  # plain language, e.g. "Income certificate (issued after 1 April 2026)"
    kind: DocumentKind = DocumentKind.OTHER
    accepted_formats: list[str] = Field(default_factory=list)  # lowercase extensions
    max_size_kb: int | None = Field(default=None, gt=0)
    min_size_kb: int | None = Field(default=None, ge=0)
    count: int = Field(default=1, ge=1)
    required: bool = True
    step_index: int | None = None
    notes: str | None = None


class EligibilityKind(str, Enum):
    AGE = "age"
    INCOME = "income"
    CATEGORY = "category"
    DOMICILE = "domicile"
    GENDER = "gender"
    EDUCATION = "education"
    OTHER = "other"


class EligibilityRule(BaseModel):
    kind: EligibilityKind
    description: str
    min_value: float | None = None
    max_value: float | None = None
    # Category/gender rules use enum values (e.g. "sc", "general_ews");
    # domicile rules use state names.
    allowed_values: list[str] = Field(default_factory=list)
    reference_date: date | None = None  # e.g. age "as on 01/07/2026"
    source_text: str | None = None


class ObstacleKind(str, Enum):
    CAPTCHA = "captcha"
    OTP = "otp"
    LOGIN = "login"
    REGISTRATION = "registration"
    IFRAME = "iframe"
    FILE_UPLOAD = "file_upload"
    CASCADING_DROPDOWN = "cascading_dropdown"
    MODAL = "modal"
    SESSION_TIMEOUT = "session_timeout"
    PAYMENT = "payment"
    DECLARATION = "declaration"
    E_KYC = "e_kyc"
    OTHER = "other"


class KnownObstacle(BaseModel):
    kind: ObstacleKind
    description: str = ""
    step_index: int | None = None


class StepKind(str, Enum):
    FORM = "form"
    LOGIN = "login"  # credentials are entered by the citizen (takeover), never planned
    REGISTRATION = "registration"
    REVIEW = "review"


class FormStep(BaseModel):
    index: int
    title: str
    kind: StepKind = StepKind.FORM
    url: str | None = None
    observed: bool = True  # False when behind a submit we must not press
    field_keys: list[str] = Field(default_factory=list)
    notes: str | None = None


class FormPlan(BaseModel):
    """What the form needs, learnt without submitting anything.

    Contains page structure only, never citizen data, so it can be cached.
    """

    portal_url: str
    portal_title: str = ""
    service_name: str | None = None
    form_version_hash: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    language: str = "en"
    steps: list[FormStep] = Field(default_factory=list)
    required_fields: list[PlanField] = Field(default_factory=list)
    optional_fields: list[PlanField] = Field(default_factory=list)
    required_documents: list[DocumentRequirement] = Field(default_factory=list)
    eligibility_rules: list[EligibilityRule] = Field(default_factory=list)
    estimated_steps: int = 0
    login_required: bool = False
    registration_required: bool = False
    known_obstacles: list[KnownObstacle] = Field(default_factory=list)
    scheme_status: SchemeStatus = SchemeStatus.UNKNOWN
    deadline: date | None = None
    instructions_summary: str = ""
    source_pages: list[str] = Field(default_factory=list)
    from_cache: bool = False
    changed_since_last_plan: bool = False

    @property
    def all_fields(self) -> list[PlanField]:
        return [*self.required_fields, *self.optional_fields]

    def has_obstacle(self, kind: ObstacleKind) -> bool:
        return any(o.kind == kind for o in self.known_obstacles)


class MissingField(BaseModel):
    key: str
    label: str
    reason: str


class DocumentProblem(str, Enum):
    MISSING = "missing"
    WRONG_FORMAT = "wrong_format"
    TOO_LARGE = "too_large"
    TOO_SMALL = "too_small"


class DocumentIssue(BaseModel):
    requirement_key: str
    name: str
    problem: DocumentProblem
    detail: str
    can_auto_convert: bool = False
    conversion: str | None = None  # e.g. "compress JPEG to under 200 KB"


class WarningSeverity(str, Enum):
    BLOCKER = "blocker"  # the citizen appears ineligible
    WARNING = "warning"
    UNVERIFIED = "unverified"  # could not check from the profile


class EligibilityWarning(BaseModel):
    kind: EligibilityKind
    severity: WarningSeverity
    message: str


class QuestionKind(str, Enum):
    FIELD = "field"
    DOCUMENT = "document"
    ELIGIBILITY = "eligibility"


class GapQuestion(BaseModel):
    id: str
    kind: QuestionKind
    prompt: str
    # "profile.<field>[.<sub>]", "extra.<key>", "doc.<requirement_key>" or
    # "eligibility.<index>".
    target: str
    options: list[str] = Field(default_factory=list)
    format_hint: str | None = None


class GapReport(BaseModel):
    missing_fields: list[MissingField] = Field(default_factory=list)
    missing_documents: list[DocumentIssue] = Field(default_factory=list)
    eligibility_warnings: list[EligibilityWarning] = Field(default_factory=list)
    questions_for_user: list[GapQuestion] = Field(default_factory=list)
    summary_en: str = ""
    summary_hi: str = ""

    @property
    def ready(self) -> bool:
        """Nothing left to ask; remaining document issues can be auto-fixed."""
        return not self.questions_for_user and all(d.can_auto_convert for d in self.missing_documents)

    @property
    def appears_ineligible(self) -> bool:
        return any(w.severity is WarningSeverity.BLOCKER for w in self.eligibility_warnings)
