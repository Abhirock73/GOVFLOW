"""Official-domain heuristics and scam/lookalike detection.

Pure and offline: uses the bundled Public Suffix List snapshot, never the
network. The result drives the trust badge the citizen sees and whether the
resolver must warn loudly before anything else happens.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from functools import lru_cache
from urllib.parse import urlparse

import tldextract

from ..config import Settings, get_settings
from ..models import DomainAssessment, DomainTrust

# Registered-domain suffixes owned by governments.
OFFICIAL_SUFFIXES = ("gov.in", "nic.in", "gov")
# "gov.uk", "gov.au", "gov.sg", "gob.mx", "gouv.fr", ...
_FOREIGN_GOV_LABELS = {"gov", "gob", "gouv", "govt", "go"}
# Public institutions: usually legitimate, never the scheme owner by default.
LIKELY_OFFICIAL_SUFFIXES = ("ac.in", "edu.in", "res.in", "mil.in")

# Well-known official portals; used only to spot typosquats of them.
KNOWN_OFFICIAL_LABELS = (
    "scholarships", "pmkisan", "parivahan", "sarathi", "uidai", "digilocker", "umang", "epfindia",
    "incometax", "passportindia", "india", "mygov", "pmjay", "nrega", "pfms", "ssc", "upsc", "eshram",
    "serviceonline", "edistrict", "jansunwai", "mahadbt", "kushalyuva", "pmaymis", "pmay",
)

SUSPICIOUS_TLDS = {
    "xyz", "top", "online", "site", "club", "info", "live", "buzz", "icu", "shop", "click", "link",
    "work", "vip", "loan", "win", "bid", "monster", "rest", "cfd", "sbs", "quest", "fun",
}

SCHEME_WORDS = (
    "yojana", "yojna", "scheme", "sarkari", "subsidy", "scholarship", "apply", "registration",
    "portal", "kisan", "awas", "pension", "ration", "aadhar", "aadhaar", "pmkisan", "pm-kisan",
)

_SUSPICIOUS_WARNING = "WARNING: this looks like a lookalike or unofficial site. Do not enter personal details here."

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registered_domain(host: str) -> str:
    ext = _extract(host)
    if not ext.suffix:
        return ext.domain or host
    return f"{ext.domain}.{ext.suffix}" if ext.domain else ext.suffix


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _has_official_suffix(reg: str, suffix: str, extra: tuple[str, ...]) -> tuple[DomainTrust | None, str]:
    for s in OFFICIAL_SUFFIXES:
        if suffix == s or suffix.endswith("." + s):
            return DomainTrust.OFFICIAL, f"government domain (.{s})"
    parts = suffix.split(".")
    if len(parts) >= 2 and parts[0] in _FOREIGN_GOV_LABELS:
        return DomainTrust.OFFICIAL, f"government domain (.{suffix})"
    for d in extra:
        d = d.lower().lstrip(".")
        if reg == d or reg.endswith("." + d):
            return DomainTrust.LIKELY_OFFICIAL, f"configured official domain ({d})"
    for s in LIKELY_OFFICIAL_SUFFIXES:
        if suffix == s:
            return DomainTrust.LIKELY_OFFICIAL, f"public institution domain (.{s})"
    return None, ""


@lru_cache(maxsize=1024)
def _assess_cached(url: str, extra_official: tuple[str, ...], insecure_hosts: tuple[str, ...]) -> DomainAssessment:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().rstrip(".")
    https = parsed.scheme == "https"
    reasons: list[str] = []
    warnings: list[str] = []

    if not host:
        return DomainAssessment(url=url, host="", registered_domain="", trust=DomainTrust.SUSPICIOUS,
                                https=https, reasons=["URL has no host"])

    if host in insecure_hosts or host.endswith(".localhost") or host.endswith(".test"):
        return DomainAssessment(url=url, host=host, registered_domain=host, trust=DomainTrust.LOCAL_TEST,
                                https=https, reasons=["local test environment"],
                                warnings=["This is a local test portal, not a real government site."])

    if _is_ip(host):
        return DomainAssessment(url=url, host=host, registered_domain=host, trust=DomainTrust.SUSPICIOUS,
                                https=https, reasons=["raw IP address instead of a domain name"],
                                warnings=[_SUSPICIOUS_WARNING, "Government portals do not use bare IP addresses."])

    ext = _extract(host)
    reg = registered_domain(host)
    suffix = ext.suffix.lower()
    label = ext.domain.lower()

    trust, why = _has_official_suffix(reg, suffix, extra_official)
    suspicious: list[str] = []

    # Homograph attacks: punycode labels or non-ASCII characters.
    if any(p.startswith("xn--") for p in host.split(".")) or any(ord(c) > 127 for c in host):
        suspicious.append("uses look-alike (internationalised) characters in the domain")
    if not https:
        warnings.append("Connection is not encrypted (http, not https).")

    if trust is None:
        tokens = re.split(r"[\-.]", host)
        # Imitating a government domain: "gov-in.org", "pmkisan.gov.in.apply.com", "govtschemes.in".
        if (
            "gov.in" in host
            or "nic.in" in host
            or any(t.startswith("gov") or t in {"nic", "sarkar"} for t in tokens)
        ):
            suspicious.append("contains 'gov'/'nic' but is not a government domain")
        for known in KNOWN_OFFICIAL_LABELS:
            if label == known:
                suspicious.append(f"uses the name of the official '{known}' portal on a non-government domain")
                break
            if len(known) >= 5 and (known in label or 0 < _levenshtein(label, known) <= 2):
                suspicious.append(f"looks like a copy of the official '{known}' portal name")
                break
        tld = suffix.split(".")[-1] if suffix else ""
        scheme_word = next((w for w in SCHEME_WORDS if w in host), None)
        if tld in SUSPICIOUS_TLDS:
            (suspicious if scheme_word else warnings).append(f"uses a low-cost domain ending (.{tld}) often used by scam sites")
        if scheme_word:
            warnings.append(f"Not a government domain, but uses the word '{scheme_word}'.")
        if host.count("-") >= 3 or sum(t.isdigit() for t in tokens) >= 2:
            warnings.append("Unusual domain shape (many hyphens or numbers).")

    if suspicious:
        final = DomainTrust.SUSPICIOUS
        reasons.extend(suspicious)
        if why:
            reasons.append(why)
    elif trust is not None:
        final = trust
        reasons.append(why)
    else:
        final = DomainTrust.UNKNOWN
        reasons.append("not a recognised government domain")

    if final is DomainTrust.SUSPICIOUS:
        warnings.insert(0, _SUSPICIOUS_WARNING)
    elif final is DomainTrust.UNKNOWN:
        warnings.insert(0, "This is not an official government domain. Check that it really belongs to the scheme owner.")

    return DomainAssessment(url=url, host=host, registered_domain=reg, trust=final, https=https,
                            reasons=reasons, warnings=warnings)


def assess_domain(url: str, settings: Settings | None = None) -> DomainAssessment:
    settings = settings or get_settings()
    url = unicodedata.normalize("NFC", url.strip())
    return _assess_cached(url, tuple(settings.extra_official_domains), tuple(settings.insecure_allowed_hosts))


def same_site(url_a: str, url_b: str) -> bool:
    """True when both URLs share a registered domain."""
    ha = (urlparse(url_a).hostname or "").lower()
    hb = (urlparse(url_b).hostname or "").lower()
    return bool(ha) and registered_domain(ha) == registered_domain(hb)


def redirect_is_acceptable(from_url: str, to_url: str, settings: Settings | None = None) -> bool:
    """A redirect may stay on the same site or move between official domains."""
    if same_site(from_url, to_url):
        return True
    a = assess_domain(from_url, settings).trust
    b = assess_domain(to_url, settings).trust
    trusted = {DomainTrust.OFFICIAL, DomainTrust.LIKELY_OFFICIAL}
    return a in trusted and b in trusted
