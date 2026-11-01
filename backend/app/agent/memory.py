"""Memory of discovered portals: cached FormPlans keyed by portal URL and form
version hash.

A FormPlan is built from page structure only and never contains citizen data,
so it is safe to keep across jobs and users. Repeat runs on the same portal
skip the expensive planning step; a different version hash means the form
changed and is re-planned.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..config import Settings, get_settings
from ..models import FormPlan

# Query parameters that carry per-visit state rather than identify a page.
_VOLATILE_PARAMS = {
    "jsessionid", "sessionid", "sid", "session", "token", "csrf", "_csrf", "_", "ts", "t", "nocache", "rand",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    url_key TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created REAL NOT NULL,
    last_used REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (url_key, version_hash)
);
CREATE INDEX IF NOT EXISTS plans_by_url ON plans (url_key, created DESC);
"""


def normalize_portal_url(url: str) -> str:
    p = urlparse(url.strip())
    path = p.path.split(";")[0].rstrip("/") or "/"  # drops ;jsessionid=...
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                             if k.lower() not in _VOLATILE_PARAMS))
    host = (p.hostname or "").lower()
    if p.port and not ((p.scheme == "http" and p.port == 80) or (p.scheme == "https" and p.port == 443)):
        host = f"{host}:{p.port}"
    return urlunparse((p.scheme.lower() or "https", host, path, "", query, ""))


class PlanCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, url: str, version_hash: str) -> FormPlan | None:
        key = normalize_portal_url(url)
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT plan_json FROM plans WHERE url_key = ? AND version_hash = ?", (key, version_hash)
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE plans SET hits = hits + 1, last_used = ? WHERE url_key = ? AND version_hash = ?",
                (time.time(), key, version_hash),
            )
        return FormPlan.model_validate_json(row[0])

    def latest(self, url: str) -> FormPlan | None:
        key = normalize_portal_url(url)
        with self._conn() as c:
            row = c.execute(
                "SELECT plan_json FROM plans WHERE url_key = ? ORDER BY created DESC LIMIT 1", (key,)
            ).fetchone()
        return FormPlan.model_validate_json(row[0]) if row else None

    def put(self, plan: FormPlan) -> None:
        if not plan.form_version_hash:
            raise ValueError("plan has no form_version_hash")
        key = normalize_portal_url(plan.portal_url)
        stored = plan.model_copy(update={"from_cache": False, "changed_since_last_plan": False})
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO plans (url_key, version_hash, plan_json, created, last_used, hits) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT (url_key, version_hash) DO UPDATE SET plan_json = excluded.plan_json, "
                "last_used = excluded.last_used",
                (key, plan.form_version_hash, stored.model_dump_json(), now, now),
            )

    def history(self, url: str) -> list[tuple[str, float, int]]:
        """``(version_hash, created, hits)`` newest first, for change audits."""
        key = normalize_portal_url(url)
        with self._conn() as c:
            return c.execute(
                "SELECT version_hash, created, hits FROM plans WHERE url_key = ? ORDER BY created DESC", (key,)
            ).fetchall()

    def purge_older_than(self, days: float) -> int:
        cutoff = time.time() - days * 86400
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM plans WHERE last_used < ?", (cutoff,)).rowcount


_cache: PlanCache | None = None
_cache_lock = threading.Lock()


def get_plan_cache(settings: Settings | None = None) -> PlanCache:
    global _cache
    settings = settings or get_settings()
    with _cache_lock:
        if _cache is None or _cache.path != settings.plan_cache_path:
            _cache = PlanCache(settings.plan_cache_path)
        return _cache
