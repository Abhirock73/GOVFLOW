"""Browser actions the agent can take, addressed by element id.

Every action:
  * checks the element exists in the latest observation and still in the DOM,
  * paces itself like a person (1-3 s by default),
  * retries once on transient failures (detached node, timeout, re-render),
  * returns an ``ActionResult`` with a machine-readable ``error_code`` and a
    message the model can learn from.

Values may be profile references (``profile.dob|DD/MM/YYYY``) or answer keys
(``extra.course``); they are resolved here, server-side, so the model never
handles the raw data. Results only ever carry masked values.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..models import CitizenProfile, InvalidProfileReference, MissingProfileData
from ..security import JobDataStore, get_store, mask_resolved_reference, redact_text, secure_delete_file
from .observe import ElementInfo
from .session import BrowserSession

logger = logging.getLogger(__name__)

ALLOWED_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "PageUp", "PageDown", "Home", "End", "Shift+Tab",
}
_TRANSIENT = ("Timeout", "detached", "not attached", "Execution context was destroyed", "Target closed",
              "element is not stable", "intercepts pointer events")
_DDMMYYYY = re.compile(r"^(\d{2})[/\-.](\d{2})[/\-.](\d{4})$")


@dataclass
class ActionResult:
    ok: bool
    action: str
    message: str
    element_id: int | None = None
    error_code: str | None = None
    url_before: str | None = None
    url_after: str | None = None
    navigated: bool = False
    new_tab: bool = False
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    masked_args: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def for_model(self) -> str:
        """Compact text returned to the model as the tool result."""
        status = "OK" if self.ok else f"FAILED ({self.error_code})"
        parts = [f"{self.action}: {status}. {self.message}"]
        if self.navigated:
            parts.append(f"Page changed to {self.url_after}.")
        if self.new_tab:
            parts.append("A new tab opened and is now active.")
        for d in self.dialogs:
            parts.append(f"A {d['type']} dialog said: {d['message']!r} ({'accepted' if d['accepted'] else 'dismissed'}).")
        if self.details.get("candidates"):
            parts.append("Candidates: " + " | ".join(self.details["candidates"]))
        return " ".join(parts)


class ValueResolver(Protocol):
    def resolve(self, text: str) -> tuple[str, str, str | None]:
        """Return ``(value, masked_value, reference_or_None)``."""


class ProfileValueResolver:
    """Resolves ``profile.*`` and ``extra.*`` references; other text is literal."""

    def __init__(self, profile: CitizenProfile, extra_params: dict[str, Any] | None = None) -> None:
        self.profile = profile
        self.extra = extra_params or {}

    def resolve(self, text: str) -> tuple[str, str, str | None]:
        t = text.strip()
        if t.startswith("profile."):
            value = self.profile.resolve_reference(t)
            return value, mask_resolved_reference(t, value), t
        if t.startswith("extra."):
            key = t.split(".", 1)[1].split("|", 1)[0]
            value = self.extra.get(key)
            if value in (None, ""):
                raise MissingProfileData(t)
            return str(value), redact_text(str(value)), t
        return text, redact_text(text), None


class ActionError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", "", s)


def match_option(options: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """Pick a <select> option by text or value; raise on no or several matches."""
    real = [o for o in options if (o.get("text") or "").strip() or o.get("value")]
    for test in (
        lambda o: o["text"] == wanted or o["value"] == wanted,
        lambda o: o["text"].strip().lower() == wanted.strip().lower(),
        lambda o: _norm(o["text"]) == _norm(wanted) or _norm(o["value"]) == _norm(wanted),
    ):
        hits = [o for o in real if test(o)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ActionError("ambiguous", f"Several options match {wanted!r}.",
                              candidates=[o["text"] for o in hits][:8])
    nw = _norm(wanted)
    if nw:
        partial = [o for o in real if nw in _norm(o["text"]) or (_norm(o["text"]) and _norm(o["text"]) in nw)]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ActionError("ambiguous", f"Several options look like {wanted!r}; ask the citizen which one.",
                              candidates=[o["text"] for o in partial][:8])
    close = difflib.get_close_matches(wanted, [o["text"] for o in real], n=5, cutoff=0.4)
    raise ActionError("no_match", f"No option matches {wanted!r}.", candidates=close or [o["text"] for o in real][:8])


class Actions:
    def __init__(self, session: BrowserSession, *, resolver: ValueResolver | None = None,
                 profile: CitizenProfile | None = None, store: JobDataStore | None = None) -> None:
        self.session = session
        self.resolver = resolver
        self.profile = profile
        self.store = store or session.store or get_store()

    # -- plumbing -----------------------------------------------------------------

    def _info(self, element_id: int) -> ElementInfo:
        obs = self.session.last_observation
        if obs is None:
            raise ActionError("no_observation", "Observe the page before acting.")
        info = obs.element(int(element_id))
        if info is None:
            raise ActionError("not_found", f"There is no element [{element_id}] on the current page. "
                                           "Use an id from the latest element list.")
        return info

    async def _locate(self, info: ElementInfo):
        frames = self.session.page.frames
        if info.frame_index >= len(frames):
            raise ActionError("stale", "The page changed since it was observed. Observe again.")
        loc = frames[info.frame_index].locator(f'[data-gf-id="{info.id}"]')
        if await loc.count() == 0:
            raise ActionError("stale", f"Element [{info.id}] is no longer on the page (it re-rendered). Observe again.")
        return loc.first

    def _resolve(self, text: str) -> tuple[str, str, str | None]:
        if self.resolver is None:
            return text, redact_text(text), None
        try:
            return self.resolver.resolve(text)
        except MissingProfileData as e:
            raise ActionError("missing_data", f"{e.reference} is not in the profile. Ask the citizen "
                                              "(ask_human kind=missing_data); never guess.") from e
        except InvalidProfileReference as e:
            raise ActionError("invalid_args", str(e)) from e

    async def _run(self, name: str, element_id: int | None, fn, masked_args: dict[str, Any]) -> ActionResult:
        s = self.session
        if s.takeover_active:
            return ActionResult(False, name, "The citizen is controlling the browser right now.", element_id,
                                "takeover_active")
        async with s.action_lock:
            page = s.page
            url_before = page.url
            tabs_before = len([p for p in s.pages if not p.is_closed()])
            blocked_before = len(s.blocked_navigations)
            await s.pace()
            result: ActionResult | None = None
            for attempt in (1, 2):
                try:
                    msg, details = await fn()
                    result = ActionResult(True, name, msg, element_id, masked_args=masked_args, details=details or {})
                    break
                except ActionError as e:
                    if e.code == "stale" and attempt == 1 and element_id is not None:
                        await asyncio.sleep(0.6)  # let a re-render finish, then try the same id once more
                        continue
                    result = ActionResult(False, name, str(e), element_id, e.code, masked_args=masked_args,
                                          details=e.details)
                    break
                except Exception as e:  # Playwright errors
                    text = str(e)
                    if attempt == 1 and any(t in text for t in _TRANSIENT):
                        await asyncio.sleep(0.8)
                        continue
                    code = "timeout" if "Timeout" in text else "error"
                    result = ActionResult(False, name, _short_error(text), element_id, code, masked_args=masked_args)
                    break
            assert result is not None
            if name in ("click", "click_xy", "press_key"):
                for _ in range(4):  # popups open asynchronously after the click
                    if len(s.context.pages) > tabs_before:
                        break
                    await asyncio.sleep(0.15)
            await s.sync_pages()
            try:
                await s.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await s.settle(3000)
            page_after = s.page
            result.url_before = url_before
            result.url_after = page_after.url if page_after else None
            result.navigated = (result.url_after or "").split("#")[0] != url_before.split("#")[0]
            result.new_tab = len([p for p in s.pages if not p.is_closed()]) > tabs_before
            result.dialogs = [{"type": d.type, "message": d.message, "accepted": d.accepted} for d in s.pop_dialogs()]
            if len(s.blocked_navigations) > blocked_before:
                # The aborted navigation leaves Chrome's error page; return to where we were.
                if s.page.url.startswith("chrome-error://"):
                    try:
                        await s.page.go_back(wait_until="domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    if s.page.url.startswith("chrome-error://"):
                        await s.page.goto(url_before, wait_until="domcontentloaded")
                result.url_after = s.page.url
                result.navigated = result.url_after.split("#")[0] != url_before.split("#")[0]
                result.ok = False
                result.error_code = "blocked_navigation"
                result.message = ("That would leave the allowed government site, so it was blocked. "
                                  "Stay on the portal or ask the citizen.")
            logger.info("action %s id=%s ok=%s code=%s nav=%s job=%s", name, element_id, result.ok,
                        result.error_code, result.navigated, s.job_id)
            return result

    # -- actions ---------------------------------------------------------------------

    async def click(self, element_id: int) -> ActionResult:
        async def go():
            info = self._info(element_id)
            if info.disabled:
                raise ActionError("disabled", f"[{element_id}] is disabled. Something required is probably missing.")
            loc = await self._locate(info)
            await loc.scroll_into_view_if_needed(timeout=5000)
            await loc.click(timeout=8000)
            return f"Clicked [{element_id}] {info.label!r}.", None

        return await self._run("click", element_id, go, {"element_id": element_id})

    async def type(self, element_id: int, text: str, *, press_enter: bool = False) -> ActionResult:
        masked_holder: dict[str, Any] = {"element_id": element_id, "text": "?"}

        async def go():
            info = self._info(element_id)
            value, masked, ref = self._resolve(text)
            masked_holder["text"] = masked if ref is None else f"{ref} -> {masked}"
            if info.tag == "select":
                raise ActionError("wrong_action", f"[{element_id}] is a dropdown; use select_option.")
            if info.type in ("checkbox", "radio"):
                raise ActionError("wrong_action", f"[{element_id}] is a {info.type}; use check.")
            if info.type == "file":
                raise ActionError("wrong_action", f"[{element_id}] is a file input; use upload_file.")
            if info.disabled:
                raise ActionError("disabled", f"[{element_id}] is disabled.")
            if info.readonly:
                raise ActionError("readonly", f"[{element_id}] is read-only (often a date picker). Click it and pick "
                                              "the value from the calendar, or use click_xy.")
            loc = await self._locate(info)
            await loc.scroll_into_view_if_needed(timeout=5000)
            if info.type == "date":
                m = _DDMMYYYY.match(value)
                fill_value = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else value
                await loc.fill(fill_value)
                expected = fill_value
            else:
                await loc.click(timeout=5000)
                await loc.fill("")
                await loc.press_sequentially(value, delay=35)
                expected = value
            if press_enter:
                await loc.press("Enter")
            actual = await loc.input_value() if info.tag in ("input", "textarea") else None
            if ref is not None:
                self.session.value_aliases[value.strip()] = ref
                if masked != value:
                    self.session.sensitive_values.add(value.strip())
            if actual is not None and not press_enter and _norm(actual) != _norm(expected):
                if info.maxlength and len(expected) > info.maxlength:
                    raise ActionError("value_mismatch", f"The field accepts only {info.maxlength} characters; the value "
                                                        "was cut off. Check the expected format.")
                if not actual:
                    raise ActionError("value_mismatch", "The field stayed empty after typing (it may reject the format "
                                                        "or need a different input method).")
                return (f"Typed into [{element_id}], but the page reformatted it to "
                        f"{redact_text(actual)!r}. Verify it is correct."), {"reformatted": True}
            return f"Typed {masked_holder['text']} into [{element_id}] {info.label!r}.", None

        return await self._run("type", element_id, go, masked_holder)

    async def select_option(self, element_id: int, value: str) -> ActionResult:
        masked_holder: dict[str, Any] = {"element_id": element_id, "value": "?"}

        async def go():
            info = self._info(element_id)
            wanted, masked, ref = self._resolve(value)
            masked_holder["value"] = masked if ref is None else f"{ref} -> {masked}"
            loc = await self._locate(info)
            if info.disabled:
                raise ActionError("disabled", f"[{element_id}] is disabled; an earlier choice may be needed first.")
            if info.tag == "select":
                read = "el => Array.from(el.options).map(o => ({text: o.text.replace(/\\s+/g,' ').trim(), value: o.value}))"
                options = await loc.evaluate(read)
                for _ in range(12):  # cascading lists often fill a moment after the parent changes
                    if len([o for o in options if o["value"] or o["text"]]) > 1:
                        break
                    await asyncio.sleep(0.25)
                    options = await loc.evaluate(read)
                if len([o for o in options if o["value"] or o["text"]]) <= 1:
                    raise ActionError("no_options", "This dropdown has no options yet. Choose the field it depends on "
                                                    "first (e.g. state before district), then wait.")
                opt = match_option(options, wanted)
                await loc.select_option(value=opt["value"])
                await self.session.settle(4000)  # dependent dropdowns load now
                chosen = await loc.evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''")
                if _norm(chosen) != _norm(opt["text"]):
                    raise ActionError("value_mismatch", f"The dropdown shows {chosen!r} instead of {opt['text']!r}.")
                return f"Selected {opt['text']!r} in [{element_id}] {info.label!r}.", None
            # Custom (non-native) dropdown: open it, then pick the option by its text.
            await loc.click(timeout=5000)
            frame = self.session.page.frames[info.frame_index]
            if info.tag == "input" or info.role in ("combobox", "textbox"):
                await loc.fill("")
                await loc.press_sequentially(wanted[:40], delay=40)
            await asyncio.sleep(0.5)
            opts = frame.get_by_role("option")
            texts = [t.strip() for t in await opts.all_inner_texts()]
            if not texts:
                raise ActionError("no_options", "No options appeared after opening the dropdown.")
            opt = match_option([{"text": t, "value": t} for t in texts], wanted)
            await opts.nth(texts.index(opt["text"])).click(timeout=5000)
            return f"Selected {opt['text']!r} in [{element_id}].", None

        return await self._run("select_option", element_id, go, masked_holder)

    async def check(self, element_id: int, checked: bool = True) -> ActionResult:
        async def go():
            info = self._info(element_id)
            if info.disabled:
                raise ActionError("disabled", f"[{element_id}] is disabled.")
            loc = await self._locate(info)
            await loc.scroll_into_view_if_needed(timeout=5000)
            if info.type in ("checkbox", "radio"):
                if checked:
                    await loc.check(timeout=5000)
                elif info.type == "radio":
                    raise ActionError("invalid_args", "A radio button cannot be unchecked; choose another option.")
                else:
                    await loc.uncheck(timeout=5000)
                state = await loc.is_checked()
            else:  # role=checkbox/switch widgets
                current = (await loc.get_attribute("aria-checked")) == "true"
                if current != checked:
                    await loc.click(timeout=5000)
                state = (await loc.get_attribute("aria-checked")) == "true"
            if state != checked:
                raise ActionError("value_mismatch", f"[{element_id}] did not change state.")
            return f"{'Checked' if checked else 'Unchecked'} [{element_id}] {info.label!r}.", None

        return await self._run("check", element_id, go, {"element_id": element_id, "checked": checked})

    async def upload_file(self, element_id: int, doc_key: str) -> ActionResult:
        async def go():
            info = self._info(element_id)
            if self.profile is None:
                raise ActionError("missing_data", "No documents are available for this job.")
            ref = self.profile.document(doc_key)
            if ref is None:
                keys = [d.doc_key for d in self.profile.documents]
                raise ActionError("missing_data", f"No document {doc_key!r}. Available: {', '.join(keys) or 'none'}. "
                                                  "Ask the citizen to upload it.", candidates=keys)
            loc = await self._locate(info)
            data = self.store.load_document(self.session.job_id, ref.doc_key)
            payload = {"name": ref.filename, "mimeType": ref.mime_type, "buffer": data}
            if info.type == "file":
                await loc.set_input_files(payload)
                count = await loc.evaluate("el => el.files ? el.files.length : 0")
                if not count:
                    raise ActionError("value_mismatch", "The file input did not accept the file.")
            else:
                async with self.session.page.expect_file_chooser(timeout=8000) as chooser:
                    await loc.click(timeout=5000)
                await (await chooser.value).set_files(payload)
            return f"Attached {ref.filename} ({ref.size_bytes // 1024} KB) to [{element_id}] {info.label!r}.", None

        return await self._run("upload_file", element_id, go, {"element_id": element_id, "doc_key": doc_key})

    async def scroll(self, direction: str = "down", amount: int = 600, element_id: int | None = None) -> ActionResult:
        async def go():
            if element_id is not None:
                loc = await self._locate(self._info(element_id))
                await loc.scroll_into_view_if_needed(timeout=5000)
                return f"Scrolled [{element_id}] into view.", None
            if direction not in ("up", "down", "left", "right"):
                raise ActionError("invalid_args", "direction must be up, down, left or right.")
            px = max(50, min(int(amount), 3000))
            dx = px if direction == "right" else -px if direction == "left" else 0
            dy = px if direction == "down" else -px if direction == "up" else 0
            await self.session.page.mouse.wheel(dx, dy)
            await asyncio.sleep(0.3)
            return f"Scrolled {direction} {px}px.", None

        return await self._run("scroll", element_id, go, {"direction": direction, "amount": amount,
                                                          "element_id": element_id})

    async def press_key(self, key: str) -> ActionResult:
        async def go():
            if key not in ALLOWED_KEYS:
                raise ActionError("invalid_args", f"Key {key!r} is not allowed. Use one of: {', '.join(sorted(ALLOWED_KEYS))}.")
            await self.session.page.keyboard.press(key)
            return f"Pressed {key}.", None

        return await self._run("press_key", None, go, {"key": key})

    async def wait(self, seconds: float = 1.0) -> ActionResult:
        async def go():
            secs = max(0.2, min(float(seconds), 10.0))
            await asyncio.sleep(secs)
            await self.session.settle(3000)
            return f"Waited {secs:.1f}s.", None

        return await self._run("wait", None, go, {"seconds": seconds})

    async def go_back(self) -> ActionResult:
        async def go():
            resp = await self.session.page.go_back(wait_until="domcontentloaded", timeout=15000)
            if resp is None:
                raise ActionError("no_history", "There is no previous page in this tab.")
            return "Went back.", None

        return await self._run("go_back", None, go, {})

    async def click_xy(self, x: float, y: float, reason: str = "") -> ActionResult:
        async def go():
            if not reason.strip():
                raise ActionError("invalid_args", "click_xy needs a reason (why the element is not in the list).")
            vw, vh = self.session.settings.browser_viewport_width, self.session.settings.browser_viewport_height
            if not (0 <= x < vw and 0 <= y < vh):
                raise ActionError("invalid_args", f"({x}, {y}) is outside the {vw}x{vh} viewport.")
            await self.session.page.mouse.click(x, y)
            return f"Clicked at ({int(x)}, {int(y)}).", None

        return await self._run("click_xy", None, go, {"x": x, "y": y, "reason": reason[:200]})

    async def run(self, name: str, args: dict[str, Any]) -> ActionResult:
        """Dispatch a tool call by name (used by the agent loop)."""
        handlers = {
            "click": lambda a: self.click(int(a["element_id"])),
            "type": lambda a: self.type(int(a["element_id"]), str(a["text"]), press_enter=bool(a.get("press_enter"))),
            "select_option": lambda a: self.select_option(int(a["element_id"]), str(a["value"])),
            "check": lambda a: self.check(int(a["element_id"]), bool(a.get("checked", True))),
            "upload_file": lambda a: self.upload_file(int(a["element_id"]), str(a["doc_key"])),
            "scroll": lambda a: self.scroll(a.get("direction", "down"), int(a.get("amount", 600)),
                                            int(a["element_id"]) if a.get("element_id") is not None else None),
            "press_key": lambda a: self.press_key(str(a["key"])),
            "wait": lambda a: self.wait(float(a.get("seconds", 1.0))),
            "go_back": lambda a: self.go_back(),
            "click_xy": lambda a: self.click_xy(float(a["x"]), float(a["y"]), str(a.get("reason", ""))),
        }
        if name not in handlers:
            return ActionResult(False, name, f"Unknown action {name!r}.", error_code="invalid_args")
        try:
            return await handlers[name](args)
        except (KeyError, TypeError, ValueError) as e:
            return ActionResult(False, name, f"Bad arguments: {e}", error_code="invalid_args")


def _short_error(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else "unknown error"
    return redact_text(first)[:300]
