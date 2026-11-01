"""One persistent Playwright browser context per job.

* All jobs in the worker process share one Chromium; each job gets its own
  isolated context (cookies, storage), kept alive between agent segments.
* ``checkpoint()`` saves the context's ``storage_state`` (session cookies,
  local storage) plus open tab URLs, encrypted with the job's key, so a job can
  resume after a crash or restart.
* Top-level navigation is checked by a pluggable ``navigation_guard`` (the
  domain allowlist from agent/guards.py).
* JavaScript dialogs: alerts are accepted; "are you sure?" confirms are only
  accepted while ``allow_confirm_dialogs`` is set (after the citizen approved
  the submit), otherwise dismissed and reported to the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urlparse

from ..config import Settings, get_settings
from ..security import JobDataStore, get_store, redact_text, secure_delete_file

logger = logging.getLogger(__name__)

NavigationGuard = Callable[[str], bool]
PageListener = Callable[["BrowserSession"], Awaitable[None]]

STATE_RECORD = "browser_state"
META_RECORD = "browser_meta"


@dataclass
class DialogEvent:
    type: str
    message: str
    accepted: bool
    at: float = field(default_factory=time.time)


class SessionClosed(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, job_id: str, *, browser: Any, settings: Settings | None = None,
                 store: JobDataStore | None = None, navigation_guard: NavigationGuard | None = None) -> None:
        self.job_id = job_id
        self.settings = settings or get_settings()
        self.store = store or get_store()
        self._browser = browser
        self.navigation_guard = navigation_guard
        self.context: Any = None
        self.pages: list[Any] = []
        self._active: Any = None
        self.closed = False

        # Agent-facing state.
        self.allow_confirm_dialogs = False
        self.dialogs: list[DialogEvent] = []
        self.blocked_navigations: list[str] = []
        self.last_observation: Any = None
        self.last_url: str | None = None
        # Typed values -> "profile.x" reference, so observations show the
        # reference instead of the value; sensitive ones are also masked in
        # screenshots.
        self.value_aliases: dict[str, str] = {}
        self.sensitive_values: set[str] = set()

        # Agent and citizen (takeover) never drive the page at the same time.
        self.action_lock = asyncio.Lock()
        self.takeover_active = False
        self._page_listeners: list[PageListener] = []
        self.staged_uploads: list[Path] = []

    # -- lifecycle --------------------------------------------------------------

    async def start(self, start_url: str | None = None, *, restore: bool = True) -> "BrowserSession":
        s = self.settings
        state = None
        meta: dict[str, Any] = {}
        if restore:
            raw = self.store.load_record(self.job_id, STATE_RECORD)
            state = json.loads(raw) if raw else None
            raw_meta = self.store.load_record(self.job_id, META_RECORD)
            meta = json.loads(raw_meta) if raw_meta else {}
        self.context = await self._browser.new_context(
            viewport={"width": s.browser_viewport_width, "height": s.browser_viewport_height},
            user_agent=s.http_user_agent, locale="en-IN", timezone_id="Asia/Kolkata",
            accept_downloads=True, storage_state=state,
        )
        self.context.set_default_timeout(15_000)
        self.context.on("page", lambda p: asyncio.ensure_future(self._adopt(p)))
        await self.context.route("**/*", self._route)
        page = await self.context.new_page()
        await self._adopt(page)
        url = meta.get("active_url") if restore and meta.get("active_url") else start_url
        if url:
            await self.goto(url)
        logger.info("browser session started job=%s restored=%s", self.job_id, bool(state))
        return self

    async def close(self, *, save: bool = True) -> None:
        if self.closed:
            return
        if save:
            try:
                await self.checkpoint()
            except Exception as e:
                logger.warning("final checkpoint failed for job %s: %s", self.job_id, type(e).__name__)
        self.closed = True
        try:
            await self.context.close()
        except Exception:
            pass
        self.value_aliases.clear()
        self.sensitive_values.clear()
        for tmp in self.staged_uploads:
            try:
                for p in tmp.glob("*"):
                    secure_delete_file(p)
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
        self.staged_uploads.clear()

    async def checkpoint(self) -> None:
        """Persist cookies/storage and open tabs, encrypted with the job key."""
        if self.closed or self.context is None:
            return
        state = await self.context.storage_state()
        self.store.save_record(self.job_id, STATE_RECORD, json.dumps(state).encode())
        meta = {
            "active_url": self.page.url if self.page and not self.page.is_closed() else None,
            "tabs": [p.url for p in self.pages if not p.is_closed()],
            "saved_at": time.time(),
        }
        self.store.save_record(self.job_id, META_RECORD, json.dumps(meta).encode())

    # -- pages ------------------------------------------------------------------

    @property
    def page(self) -> Any:
        if self.closed:
            raise SessionClosed(f"browser session for {self.job_id} is closed")
        if self._active is None or self._active.is_closed():
            open_pages = [p for p in self.pages if not p.is_closed()]
            self._active = open_pages[-1] if open_pages else None
        return self._active

    def on_page_change(self, listener: PageListener) -> None:
        self._page_listeners.append(listener)

    async def _adopt(self, page: Any) -> None:
        if page in self.pages or self.closed:
            return
        try:
            await self._adopt_inner(page)
        except Exception as e:  # page closed while being adopted
            logger.debug("could not adopt page: %s", type(e).__name__)

    async def _adopt_inner(self, page: Any) -> None:
        self.pages.append(page)
        page.on("dialog", lambda d: asyncio.ensure_future(self._on_dialog(d)))
        page.on("close", lambda _p: asyncio.ensure_future(self._on_close(page)))
        page.on("requestfailed", lambda req: logger.debug("request failed: %s %s: %s", req.method, req.url, req.failure))
        page.on("pageerror", lambda err: logger.debug("page error: %s", err))
        page.set_default_timeout(15_000)
        previous = self._active
        self._active = page
        if previous is not None:
            # A popup / new tab: follow it, like a person would.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            logger.info("job %s switched to a new tab", self.job_id)
            await self._notify()

    async def sync_pages(self) -> None:
        """Adopt any tab Playwright knows about that the event handler hasn't yet."""
        if self.closed or self.context is None:
            return
        for p in list(self.context.pages):
            if p not in self.pages and not p.is_closed():
                await self._adopt(p)

    async def _on_close(self, page: Any) -> None:
        if self.closed:
            return
        if page is self._active:
            self._active = None
            if self.page is not None:
                await self._notify()

    async def switch_to(self, index: int) -> None:
        open_pages = [p for p in self.pages if not p.is_closed()]
        self._active = open_pages[index]
        await self._active.bring_to_front()
        await self._notify()

    async def _notify(self) -> None:
        for listener in list(self._page_listeners):
            try:
                await listener(self)
            except Exception as e:
                logger.warning("page listener failed: %s", type(e).__name__)

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.settle()

    async def settle(self, timeout_ms: int = 5000) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    async def pace(self) -> None:
        """Human-like pause between actions (1-3 s by default)."""
        s = self.settings
        if s.action_delay_max_seconds > 0:
            await asyncio.sleep(random.uniform(s.action_delay_min_seconds, s.action_delay_max_seconds))

    # -- network / dialogs ------------------------------------------------------

    async def _route(self, route: Any) -> None:
        req = route.request
        try:
            top_level = req.is_navigation_request() and req.frame.parent_frame is None
        except Exception:
            top_level = False
        if top_level and self.navigation_guard and not self.navigation_guard(req.url):
            host = urlparse(req.url).hostname or "?"
            self.blocked_navigations.append(urldefrag(req.url)[0])
            logger.warning("blocked navigation off the allowed domains job=%s host=%s", self.job_id, host)
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _on_dialog(self, dialog: Any) -> None:
        kind = dialog.type
        accept = kind in ("alert", "beforeunload") or (kind in ("confirm", "prompt") and self.allow_confirm_dialogs)
        self.dialogs.append(DialogEvent(kind, redact_text(dialog.message or "")[:300], accept))
        try:
            await (dialog.accept() if accept else dialog.dismiss())
        except Exception:
            pass

    def pop_dialogs(self) -> list[DialogEvent]:
        out, self.dialogs = self.dialogs, []
        return out


class SessionManager:
    """Owns the Playwright driver and every job's live session in this process."""

    def __init__(self, settings: Settings | None = None, store: JobDataStore | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = store
        self._pw: Any = None
        self._browser: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Any:
        if self._browser is None or not self._browser.is_connected():
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=self.settings.browser_headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        return self._browser

    async def get_or_create(self, job_id: str, *, start_url: str | None = None, restore: bool = True,
                            navigation_guard: NavigationGuard | None = None) -> BrowserSession:
        async with self._lock:
            existing = self._sessions.get(job_id)
            if existing is not None and not existing.closed:
                if navigation_guard is not None:
                    existing.navigation_guard = navigation_guard
                return existing
            browser = await self._ensure_browser()
            session = BrowserSession(job_id, browser=browser, settings=self.settings, store=self.store,
                                     navigation_guard=navigation_guard)
            await session.start(start_url, restore=restore)
            self._sessions[job_id] = session
            return session

    def get(self, job_id: str) -> BrowserSession | None:
        s = self._sessions.get(job_id)
        return s if s is not None and not s.closed else None

    async def close(self, job_id: str, *, save: bool = True) -> None:
        session = self._sessions.pop(job_id, None)
        if session is not None:
            await session.close(save=save)

    async def shutdown(self) -> None:
        for job_id in list(self._sessions):
            await self.close(job_id)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager
