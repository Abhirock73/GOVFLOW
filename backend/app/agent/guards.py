"""Safety guards protecting the agent loop.

1. Loop detection: detects repetitive actions or cycles.
2. Domain allowlist: prevents the agent from leaving the target portal.
3. Prompt injection defense: detects and neutralizes malicious page instructions.
4. Irreversible action guard: ensures final submission is approved by a human.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from urllib.parse import urlparse

from ..browser.observe import ElementInfo, Observation
from ..config import Settings, get_settings
from ..discovery.domain_check import assess_domain

logger = logging.getLogger(__name__)

INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions",
    r"disregard\s+(?:all\s+)?(?:previous|prior)\s+instructions",
    r"system\s*override",
    r"you\s+are\s+now\s+(?:in\s+developer|unrestricted|maintenance)\s+mode",
    r"new\s+system\s+prompt\s*:",
    r"print\s+(?:the\s+)?system\s+prompt",
    r"reveal\s+(?:all\s+)?passwords",
    r"transfer\s+(?:funds|money|balance)",
    r"delete\s+my\s+data\s+immediately",
    r"bypass\s+(?:all\s+)?safety\s+checks",
    r"admin\s+token\s*:",
    r"curl\s+https?://",
    r"<script\b[^>]*>",
]

SUBMIT_KEYWORDS = [
    "submit",
    "final submit",
    "pay now",
    "make payment",
    "proceed to payment",
    "confirm and submit",
    "जमा करें",
    "भुगतान करें",
    "आवेदन जमा करें",
    "declare and submit",
]


class GuardViolation(Exception):
    def __init__(self, guard_name: str, message: str):
        super().__init__(f"[{guard_name}] {message}")
        self.guard_name = guard_name
        self.message = message


class LoopGuard:
    """Detects cycles and repeated failed actions."""

    def __init__(self, max_repeats: int = 3) -> None:
        self.max_repeats = max_repeats
        self.history: list[tuple[str, str, str]] = []  # (state_hash, action_name, args_hash)

    def _state_hash(self, obs: Observation) -> str:
        tokens = [obs.url]
        for el in obs.elements[:50]:
            tokens.append(f"{el.id}:{el.tag}:{el.label}:{el.value}")
        return hashlib.sha256("|".join(tokens).encode()).hexdigest()[:16]

    def _args_hash(self, args: dict[str, Any]) -> str:
        s = sorted((str(k), str(v)) for k, v in args.items())
        return hashlib.sha256(str(s).encode()).hexdigest()[:16]

    def check(self, obs: Observation, action_name: str, args: dict[str, Any]) -> None:
        st_hash = self._state_hash(obs)
        arg_hash = self._args_hash(args)
        entry = (st_hash, action_name, arg_hash)

        self.history.append(entry)

        # Count how many consecutive times this exact action on this state occurred
        count = 0
        for item in reversed(self.history):
            if item == entry:
                count += 1
            else:
                break

        if count >= self.max_repeats:
            raise GuardViolation(
                "LoopGuard",
                f"Action '{action_name}' repeated {count} times on the same page state without progress.",
            )


class DomainGuard:
    """Ensures browser navigation stays within target portal domain and safe testbed hosts."""

    def __init__(self, target_url: str, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.target_url = target_url
        p = urlparse(target_url)
        self.target_host = (p.hostname or "").lower()

        assessment = assess_domain(target_url, self.settings)
        self.registered_domain = assessment.registered_domain.lower()
        self.allowed_hosts = {
            self.target_host,
            *self.settings.insecure_allowed_hosts,
            *[h.lower() for h in self.settings.extra_allowed_domains],
        }

    def is_url_allowed(self, url: str) -> bool:
        if not url:
            return True
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if not host:
            return True  # relative url

        if host in self.allowed_hosts:
            return True

        if self.registered_domain and (host == self.registered_domain or host.endswith(f".{self.registered_domain}")):
            return True

        return False

    def check_navigation(self, candidate_url: str) -> None:
        if not self.is_url_allowed(candidate_url):
            raise GuardViolation(
                "DomainGuard",
                f"Navigation to off-target domain '{candidate_url}' is prohibited (target was '{self.target_url}').",
            )


class PromptInjectionGuard:
    """Scans visible text and element content for prompt injection payloads."""

    def __init__(self) -> None:
        self.regexes = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

    def detect(self, text: str) -> list[str]:
        findings = []
        for r in self.regexes:
            m = r.search(text)
            if m:
                findings.append(m.group(0))
        return findings

    def check_observation(self, obs: Observation) -> None:
        findings = self.detect(obs.text)
        if findings:
            logger.warning("Prompt injection signature detected on page: %r", findings)
            # Log warning, but we don't abort navigation entirely; we flag and neutralize instructions


class SubmitGuard:
    """Guards final submit and irreversible payment actions."""

    def __init__(self) -> None:
        self.confirmed: bool = False

    def is_submit_action(self, el: ElementInfo) -> bool:
        label = (el.label or el.value or el.name or "").lower().strip()
        tag = el.tag.lower()
        el_type = (el.type or "").lower()

        if el_type == "submit" or tag == "button" or el.role == "button":
            return any(k in label for k in SUBMIT_KEYWORDS)
        return False

    def check(self, el: ElementInfo | None) -> None:
        if el is None:
            return
        if self.is_submit_action(el) and not self.confirmed:
            raise GuardViolation(
                "SubmitGuard",
                f"Element [{el.id}] '{el.label}' appears to be a final submit or payment action. "
                "Explicit human approval via ask_human(kind='confirm_submit', ...) is required before clicking.",
            )

