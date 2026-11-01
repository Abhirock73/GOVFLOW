from __future__ import annotations

import pytest

from app.discovery.domain_check import assess_domain, redirect_is_acceptable, registered_domain
from app.models import DomainTrust


@pytest.mark.parametrize(
    "url",
    [
        "https://scholarships.gov.in/fresh/newstdRegfrmInstruction",
        "https://pmkisan.gov.in/",
        "https://kushalyuva.bihar.gov.in/",
        "https://serviceonline.bihar.gov.in/",
        "https://edistrict.up.nic.in/",
        "https://www.usa.gov/benefits",
        "https://www.gov.uk/apply-passport",
    ],
)
def test_official(url):
    d = assess_domain(url)
    assert d.trust is DomainTrust.OFFICIAL, d.reasons
    assert not d.needs_loud_warning


@pytest.mark.parametrize(
    "url,why",
    [
        ("https://pmkisan-gov.in/apply", "gov"),
        ("https://gov-in.org/scholarship", "gov"),
        ("https://pmkisan.gov.in.apply-now.com/", "gov"),
        ("https://govtschemes.in/kushal-yuva", "gov"),
        ("https://pmkisaan.in/", "copy"),
        ("https://scholarship-apply.xyz/", "low-cost"),
        ("https://xn--pmkisn-9ua.in/", "look-alike"),
        ("http://103.21.58.9/apply", "IP"),
    ],
)
def test_suspicious(url, why):
    d = assess_domain(url)
    assert d.trust is DomainTrust.SUSPICIOUS
    assert any(why.lower() in r.lower() for r in d.reasons), d.reasons
    assert d.needs_loud_warning
    assert d.warnings[0].startswith("WARNING")


def test_unknown_domain_gets_warning():
    d = assess_domain("https://www.example-news.com/kushal-yuva-2026-apply")
    assert d.trust is DomainTrust.UNKNOWN
    assert d.needs_loud_warning
    assert "not an official" in d.warnings[0]


def test_likely_official_and_configured(settings, monkeypatch):
    assert assess_domain("https://www.iitb.ac.in/").trust is DomainTrust.LIKELY_OFFICIAL
    settings.extra_official_domains = ["onlinesbi.sbi"]
    assert assess_domain("https://retail.onlinesbi.sbi/", settings).trust is DomainTrust.LIKELY_OFFICIAL


def test_local_testbed():
    d = assess_domain("http://localhost:5001/register")
    assert d.trust is DomainTrust.LOCAL_TEST
    assert not d.needs_loud_warning


def test_http_warning_on_official():
    d = assess_domain("http://pmkisan.gov.in/")
    assert d.trust is DomainTrust.OFFICIAL
    assert not d.https and any("not encrypted" in w for w in d.warnings)


def test_registered_domain_and_redirects():
    assert registered_domain("kushalyuva.bihar.gov.in") == "bihar.gov.in"
    assert registered_domain("a.b.example.co.uk") == "example.co.uk"
    assert redirect_is_acceptable("https://a.bihar.gov.in/x", "https://b.bihar.gov.in/y")
    assert redirect_is_acceptable("https://scheme.gov.in/", "https://state.nic.in/apply")
    assert not redirect_is_acceptable("https://scheme.gov.in/", "https://apply-scheme.com/")
