"""Takeover mode: the citizen drives the live browser themselves.

Used for CAPTCHAs, logins, Aadhaar e-KYC and anything the agent must not do.
While takeover is active the agent cannot act (the session's action lock is
held). Mouse and keyboard events from the viewer are mapped from canvas
coordinates to the viewport and sent through CDP ``Input.*``. Nothing the
citizen types is logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from .session import BrowserSession

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_SECOND = 80
MAX_TEXT = 500

# Non-printable keys: (code, windowsVirtualKeyCode, text sent with keyDown)
SPECIAL_KEYS: dict[str, tuple[str, int, str | None]] = {
    "Enter": ("Enter", 13, "\r"), "Tab": ("Tab", 9, None), "Backspace": ("Backspace", 8, None),
    "Delete": ("Delete", 46, None), "Escape": ("Escape", 27, None), "ArrowUp": ("ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", 40, None), "ArrowLeft": ("ArrowLeft", 37, None), "ArrowRight": ("ArrowRight", 39, None),
    "Home": ("Home", 36, None), "End": ("End", 35, None), "PageUp": ("PageUp", 33, None),
    "PageDown": ("PageDown", 34, None), " ": ("Space", 32, " "),
}


class InputEvent(BaseModel):
    kind: Literal["mouse", "wheel", "key", "text"]
    # mouse: down | up | move | click | dblclick ; key: down | up | press
    action: str = "click"
    x: float = 0
    y: float = 0
    canvas_width: float = Field(default=0, ge=0)
    canvas_height: float = Field(default=0, ge=0)
    button: Literal["left", "right", "middle", "none"] = "left"
    delta_x: float = 0
    delta_y: float = 0
    key: str = ""
    text: str = ""
    alt: bool = False
    ctrl: bool = False
    meta: bool = False
    shift: bool = False


class TakeoverError(RuntimeError):
    pass


class TakeoverController:
    def __init__(self, session: BrowserSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.reason: str | None = None
        self.started_at: float | None = None
        self._cdp: Any = None
        self._cdp_page: Any = None
        self._window_start = 0.0
        self._window_count = 0

    @property
    def active(self) -> bool:
        return self.session.takeover_active

    async def begin(self, reason: str = "") -> None:
        if self.active:
            return
        await self.session.action_lock.acquire()  # waits for any agent action in flight
        self.session.takeover_active = True
        self.reason = reason[:200] or None
        self.started_at = time.time()
        logger.info("takeover started job=%s reason=%s", self.session.job_id, self.reason)

    async def end(self) -> float:
        """Hand control back to the agent. Returns the takeover duration in seconds."""
        if not self.active:
            return 0.0
        self.session.takeover_active = False
        await self._drop_cdp()
        if self.session.action_lock.locked():
            self.session.action_lock.release()
        duration = time.time() - (self.started_at or time.time())
        logger.info("takeover ended job=%s after %.0fs", self.session.job_id, duration)
        self.started_at = None
        try:
            await self.session.checkpoint()  # e.g. the citizen just logged in
        except Exception:
            pass
        return duration

    async def _drop_cdp(self) -> None:
        cdp, self._cdp, self._cdp_page = self._cdp, None, None
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass

    async def _client(self) -> Any:
        page = self.session.page
        if self._cdp is None or self._cdp_page is not page:
            await self._drop_cdp()
            self._cdp = await page.context.new_cdp_session(page)
            self._cdp_page = page
        return self._cdp

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start, self._window_count = now, 0
        self._window_count += 1
        return self._window_count <= MAX_EVENTS_PER_SECOND

    def _to_viewport(self, ev: InputEvent) -> tuple[float, float]:
        vw, vh = self.settings.browser_viewport_width, self.settings.browser_viewport_height
        x, y = ev.x, ev.y
        if ev.canvas_width and ev.canvas_height:
            x, y = x * vw / ev.canvas_width, y * vh / ev.canvas_height
        return max(0.0, min(x, vw - 1)), max(0.0, min(y, vh - 1))

    @staticmethod
    def _modifiers(ev: InputEvent) -> int:
        return (1 if ev.alt else 0) | (2 if ev.ctrl else 0) | (4 if ev.meta else 0) | (8 if ev.shift else 0)

    async def dispatch(self, ev: InputEvent) -> None:
        if not self.active:
            raise TakeoverError("Takeover is not active; press 'Take over' first.")
        if not self._rate_ok():
            return
        cdp = await self._client()
        mods = self._modifiers(ev)
        if ev.kind == "mouse":
            x, y = self._to_viewport(ev)
            button = ev.button if ev.button != "none" else "left"
            base = {"x": x, "y": y, "modifiers": mods}
            if ev.action == "move":
                await cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseMoved", "button": "none"})
            elif ev.action in ("down", "up"):
                await cdp.send("Input.dispatchMouseEvent", {
                    **base, "type": "mousePressed" if ev.action == "down" else "mouseReleased",
                    "button": button, "clickCount": 1})
            elif ev.action in ("click", "dblclick"):
                clicks = 2 if ev.action == "dblclick" else 1
                await cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseMoved", "button": "none"})
                for n in range(1, clicks + 1):
                    await cdp.send("Input.dispatchMouseEvent", {**base, "type": "mousePressed", "button": button,
                                                               "clickCount": n})
                    await cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseReleased", "button": button,
                                                               "clickCount": n})
            else:
                raise TakeoverError(f"unknown mouse action {ev.action!r}")
        elif ev.kind == "wheel":
            x, y = self._to_viewport(ev)
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y, "deltaX": max(-2000, min(ev.delta_x, 2000)),
                "deltaY": max(-2000, min(ev.delta_y, 2000)), "modifiers": mods})
        elif ev.kind == "text":
            if ev.text:
                await cdp.send("Input.insertText", {"text": ev.text[:MAX_TEXT]})
        elif ev.kind == "key":
            await self._key(cdp, ev, mods)

    async def _key(self, cdp: Any, ev: InputEvent, mods: int) -> None:
        key = ev.key
        if key in SPECIAL_KEYS:
            code, vk, text = SPECIAL_KEYS[key]
        elif len(key) == 1:
            if ev.action in ("press", "down") and not (ev.ctrl or ev.meta or ev.alt):
                await cdp.send("Input.insertText", {"text": key})
                return
            code, vk, text = f"Key{key.upper()}" if key.isalpha() else "", ord(key.upper()), None
        else:
            raise TakeoverError(f"unsupported key {key!r}")
        down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk, "modifiers": mods}
        if text and not (mods & 6):
            down["text"] = text
        up = {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk,
              "nativeVirtualKeyCode": vk, "modifiers": mods}
        if ev.action in ("press", "down"):
            await cdp.send("Input.dispatchKeyEvent", down)
        if ev.action in ("press", "up"):
            await cdp.send("Input.dispatchKeyEvent", up)
