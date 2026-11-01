"""Hybrid perception: what the agent sees at each step.

``observe()`` returns
  (a) a viewport screenshot annotated with numbered boxes (set-of-marks),
  (b) the numbered interactive-element list across the page and its iframes,
  (c) visible page text,
  (d) flags: has_captcha, has_otp_field, has_modal, url_changed, error_banners.

Privacy: values the agent typed from the profile are shown as their reference
(``[profile.id_number]``), other PII in values and text is redacted, and
sensitive values are visually hidden before the screenshot is taken, so
neither the model nor the timeline thumbnail sees raw ID numbers.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..security import redact_text
from .session import BrowserSession

logger = logging.getLogger(__name__)

MAX_ELEMENTS = 250
MAX_OPTIONS_SHOWN = 40
TEXT_LIMIT = 8000
ATTR = "data-gf-id"

# Marks interactive elements with data-gf-id (starting at `start`) and
# describes them. Generic DOM heuristics only.
MARK_JS = r"""
({start, limit}) => {
  const ATTR = 'data-gf-id';
  document.querySelectorAll('[' + ATTR + ']').forEach(e => e.removeAttribute(ATTR));
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const sel = 'a[href],button,input,select,textarea,summary,[contenteditable="true"],' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=option],[role=combobox],' +
    '[role=menuitem],[role=switch],[role=textbox],[role=listbox],[onclick]';
  const visible = el => {
    if (el.type === 'file') return true;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const byFor = el => { if (!el.id) return ''; const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); return l ? clean(l.innerText) : ''; };
  const labelOf = el => {
    let t = clean(el.getAttribute('aria-label')) || byFor(el);
    if (!t && el.getAttribute('aria-labelledby')) t = clean(el.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    const tag = el.tagName.toLowerCase();
    if (!t && ['a', 'button', 'summary', 'option'].includes(tag) || (!t && ['button', 'link', 'tab', 'menuitem', 'option'].includes(el.getAttribute('role')))) t = clean(el.innerText);
    if (!t && tag === 'input' && ['submit', 'button', 'reset'].includes(el.type)) t = clean(el.value);
    if (!t && tag === 'input' && el.type === 'image') t = clean(el.alt || el.title);
    if (!t) { const l = el.closest('label'); if (l) { const c = l.cloneNode(true); c.querySelectorAll('select,option,input').forEach(x => x.remove()); t = clean(c.innerText); } }
    if (!t) { const td = el.closest('td,th'); if (td) { let p = td.previousElementSibling; while (p && !clean(p.innerText)) p = p.previousElementSibling; if (p) t = clean(p.innerText); } }
    if (!t) {
      const item = el.closest('[role="listitem"], .geS5n, .Qr7Oae, .freebirdFormviewerViewNumberedItemContainer');
      if (item) {
        const h = item.querySelector('[role="heading"], .M7eMe, .freebirdFormviewerComponentsQuestionBaseTitle');
        if (h) t = clean(h.innerText);
      }
    }
    if (!t) { let p = el.previousSibling, n = 0; while (p && n < 3 && !t) { t = clean(p.textContent); p = p.previousSibling; n++; } }
    if (!t) t = clean(el.getAttribute('placeholder')) || clean(el.title) || clean(el.name) || clean(el.id);
    if (!t && tag === 'a') { const img = el.querySelector('img'); if (img) t = clean(img.alt); }
    return t.slice(0, 120);
  };
  const groupOf = el => {
    const fs = el.closest('fieldset'); const lg = fs && fs.querySelector('legend'); if (lg) return clean(lg.innerText).slice(0, 80);
    const td = el.closest('td'); const p = td && td.previousElementSibling; if (p) return clean(p.innerText).slice(0, 80);
    return '';
  };
  const out = []; let id = start;
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= limit) break;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    el.setAttribute(ATTR, String(id));
    const rec = { id, tag, type: tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (type || (tag === 'input' ? 'text' : '')),
      role: el.getAttribute('role') || '', label: labelOf(el), group: '', value: null, checked: null, options: [], options_total: 0,
      required: !!el.required || el.getAttribute('aria-required') === 'true', disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      readonly: !!el.readOnly, placeholder: clean(el.getAttribute('placeholder')).slice(0, 60),
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 160) : '', maxlength: el.maxLength > 0 ? el.maxLength : null,
      autocomplete: el.getAttribute('autocomplete') || '', name: el.name || '',
      x: r.left, y: r.top, w: r.width, h: r.height, in_viewport: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw };
    if (tag === 'select') {
      const opts = Array.from(el.options);
      rec.options_total = opts.length;
      rec.options = opts.slice(0, 400).map(o => ({ text: clean(o.text).slice(0, 80), value: o.value, selected: o.selected }));
      const s = opts[el.selectedIndex]; rec.value = s ? clean(s.text) : '';
    } else if (type === 'checkbox' || type === 'radio') {
      rec.checked = !!el.checked; rec.group = type === 'radio' ? groupOf(el) : '';
      let ol = byFor(el); if (!ol) { const n = el.nextSibling; ol = clean(n && n.textContent); }
      if (ol) rec.label = ol.slice(0, 120);
    } else if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'switch' || el.getAttribute('role') === 'radio') {
      rec.checked = el.getAttribute('aria-checked') === 'true';
    } else if (tag === 'input' || tag === 'textarea') {
      rec.value = type === 'password' ? (el.value ? '********' : '') : (type === 'file' ? Array.from(el.files || []).map(f => f.name).join(', ') : el.value);
      if (type === 'file') rec.accept = el.getAttribute('accept') || '';
    } else if (el.isContentEditable) {
      rec.value = clean(el.innerText).slice(0, 200);
    }
    out.push(rec); id++;
  }
  const body = document.body;
  const text = body ? body.innerText.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 12000) : '';
  const imgs = Array.from(document.images).map(i => ((i.alt || '') + ' ' + (i.src || '') + ' ' + (i.id || '') + ' ' + (i.className || '')).toLowerCase());
  const captcha = imgs.some(s => s.includes('captcha')) || !!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],.g-recaptcha,.h-captcha,.cf-turnstile,[id*=captcha i],[class*=captcha i],[name*=captcha i]');
  const isVis = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect(); return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0.05 && r.width > 1 && r.height > 1; };
  let modal = Array.from(document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open],.modal.show,.modal.in')).some(isVis);
  if (!modal) {
    for (const el of document.querySelectorAll('body *')) {
      const s = getComputedStyle(el);
      if ((s.position !== 'fixed' && s.position !== 'absolute') || (parseInt(s.zIndex) || 0) < 100 || !isVis(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width * r.height > 0.25 * vw * vh && clean(el.innerText).length > 0) { modal = true; break; }
    }
  }
  const errs = [];
  const pushErr = el => { const t = clean(el.innerText); if (t && t.length < 300 && !errs.includes(t) && isVis(el)) errs.push(t); };
  document.querySelectorAll('[role=alert],[aria-live=assertive],.error,.errors,.errormsg,.error-message,.alert-danger,.invalid-feedback,.text-danger,.field-validation-error,.validation-summary-errors,.has-error .help-block').forEach(pushErr);
  for (const el of document.querySelectorAll('span,font,div,p,li,label,b,strong,td')) {
    if (errs.length >= 8) break;
    if (el.children.length > 3) continue;
    const c = getComputedStyle(el).color.match(/\d+/g);
    if (c && +c[0] > 180 && +c[1] < 80 && +c[2] < 80) {
      const t = clean(el.innerText);
      if (t.length > 3 && t.length < 200 && !/^\*+$/.test(t) && /[a-z]{3}/i.test(t)) pushErr(el);
    }
  }
  const invalid = Array.from(document.querySelectorAll('[aria-invalid=true],:invalid')).filter(e => e.tagName !== 'FORM' && e.tagName !== 'FIELDSET' && e.value !== undefined && e.value !== '').length;
  return { url: location.href, title: document.title || '', elements: out, next: id, text, captcha, modal, errors: errs.slice(0, 8), invalid_fields: invalid,
           viewport: { w: vw, h: vh }, scroll_y: window.scrollY, page_h: document.documentElement.scrollHeight };
}
"""

MASK_JS = r"""
(values) => {
  const set = new Set(values);
  const hits = [];
  document.querySelectorAll('input,textarea').forEach(el => {
    if (el.value && set.has(el.value.trim())) {
      el.dataset.gfMask = el.style.cssText;
      el.style.setProperty('-webkit-text-security', 'disc', 'important');
      el.style.setProperty('color', 'transparent', 'important');
      el.style.setProperty('text-shadow', '0 0 0 #555', 'important');
      hits.push(1);
    }
  });
  return hits.length;
}
"""

UNMASK_JS = r"""
() => { document.querySelectorAll('[data-gf-mask]').forEach(el => { el.style.cssText = el.dataset.gfMask; delete el.dataset.gfMask; }); }
"""


@dataclass
class ElementInfo:
    id: int
    tag: str
    type: str
    role: str
    label: str
    group: str = ""
    value: str | None = None
    checked: bool | None = None
    options: list[dict[str, Any]] = field(default_factory=list)
    options_total: int = 0
    required: bool = False
    disabled: bool = False
    readonly: bool = False
    placeholder: str = ""
    href: str = ""
    maxlength: int | None = None
    autocomplete: str = ""
    name: str = ""
    accept: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    in_viewport: bool = True
    frame_index: int = 0

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    @property
    def in_iframe(self) -> bool:
        return self.frame_index > 0

    def describe(self) -> str:
        kind = self.type or self.role or self.tag
        if self.tag == "a":
            kind = "link"
        elif self.tag == "button" or self.type in ("submit", "button", "image", "reset"):
            kind = "button"
        parts = [f"[{self.id}] {kind}"]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.group and self.group != self.label:
            parts.append(f"group={self.group!r}")
        if self.value not in (None, ""):
            parts.append(f"value={self.value!r}")
        if self.checked is not None:
            parts.append("checked" if self.checked else "unchecked")
        if self.options:
            texts = [o["text"] for o in self.options if o["text"]][:MAX_OPTIONS_SHOWN]
            more = self.options_total - len(texts)
            parts.append("options: " + " | ".join(texts) + (f" (+{more} more; select by text)" if more > 0 else ""))
        flags = [n for n, on in (("required", self.required), ("disabled", self.disabled), ("readonly", self.readonly),
                                 ("in-iframe", self.in_iframe), ("off-screen", not self.in_viewport)) if on]
        if self.placeholder and not self.value:
            flags.append(f"placeholder={self.placeholder!r}")
        if self.accept:
            flags.append(f"accept={self.accept}")
        if self.maxlength:
            flags.append(f"maxlength={self.maxlength}")
        if flags:
            parts.append("(" + ", ".join(flags) + ")")
        return " ".join(parts)


@dataclass
class Observation:
    url: str
    title: str
    elements: list[ElementInfo]
    text: str
    screenshot: bytes  # annotated JPEG for the model
    thumbnail: bytes  # small masked JPEG for the timeline
    has_captcha: bool
    has_otp_field: bool
    has_modal: bool
    url_changed: bool
    error_banners: list[str]
    invalid_fields: int
    viewport: tuple[int, int]
    scroll_y: float
    page_height: float
    tab_count: int
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    taken_at: float = field(default_factory=time.time)

    def element(self, element_id: int) -> ElementInfo | None:
        return next((e for e in self.elements if e.id == element_id), None)

    @property
    def screenshot_b64(self) -> str:
        return base64.b64encode(self.screenshot).decode()

    @property
    def thumbnail_b64(self) -> str:
        return base64.b64encode(self.thumbnail).decode()

    @property
    def flags(self) -> dict[str, Any]:
        return {"has_captcha": self.has_captcha, "has_otp_field": self.has_otp_field, "has_modal": self.has_modal,
                "url_changed": self.url_changed, "error_banners": self.error_banners}

    def to_prompt(self) -> str:
        lines = [
            f"URL: {self.url}",
            f"Title: {self.title}",
            f"Tabs open: {self.tab_count}. Scroll: {int(self.scroll_y)}/{int(self.page_height)}px.",
        ]
        flags = [k for k, v in self.flags.items() if v and k != "error_banners"]
        if flags:
            lines.append("Flags: " + ", ".join(flags))
        if self.error_banners:
            lines.append("Error messages on page: " + " | ".join(self.error_banners))
        if self.invalid_fields:
            lines.append(f"Fields marked invalid: {self.invalid_fields}")
        for d in self.dialogs:
            lines.append(f"Dialog ({d['type']}, {'accepted' if d['accepted'] else 'dismissed'}): {d['message']}")
        lines.append("Interactive elements:")
        lines.extend("  " + e.describe() for e in self.elements)
        return "\n".join(lines)


def _clean_value(value: str | None, aliases: dict[str, str]) -> str | None:
    if value is None or value == "":
        return value
    v = value.strip()
    if v in aliases:
        return f"[{aliases[v]}]"
    return redact_text(value)[:200]


def _clean_text(text: str, aliases: dict[str, str]) -> str:
    for raw, ref in sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(raw) >= 4:
            text = text.replace(raw, f"[{ref}]")
    return redact_text(text)


def _annotate(jpeg: bytes, elements: list[ElementInfo]) -> tuple[bytes, bytes]:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    thumb = img.copy()
    thumb.thumbnail((360, 360))
    tbuf = io.BytesIO()
    thumb.save(tbuf, "JPEG", quality=60)

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()
    for e in elements:
        if not e.in_viewport or e.w < 2:
            continue
        box = [e.x, e.y, e.x + e.w, e.y + e.h]
        draw.rectangle(box, outline=(230, 30, 120), width=1)
        tag = str(e.id)
        tw = 7 * len(tag) + 4
        lx, ly = max(0, e.x - 1), max(0, e.y - 13)
        draw.rectangle([lx, ly, lx + tw, ly + 12], fill=(230, 30, 120))
        draw.text((lx + 2, ly), tag, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue(), tbuf.getvalue()


async def _frame_offset(frame: Any) -> tuple[float, float] | None:
    """Top-left of an iframe in page coordinates, or None if not visible."""
    offset_x = offset_y = 0.0
    f = frame
    while f.parent_frame is not None:
        try:
            el = await f.frame_element()
            box = await el.bounding_box()
        except Exception:
            return None
        if box is None:
            return None
        offset_x += box["x"]
        offset_y += box["y"]
        f = f.parent_frame
    return offset_x, offset_y


async def observe(session: BrowserSession, *, screenshot: bool = True) -> Observation:
    page = session.page
    elements: list[ElementInfo] = []
    texts: list[str] = []
    captcha = modal = False
    errors: list[str] = []
    invalid = 0
    main: dict[str, Any] | None = None
    next_id = 1

    for index, frame in enumerate(page.frames):
        if frame.is_detached():
            continue
        offset = (0.0, 0.0) if index == 0 else await _frame_offset(frame)
        if offset is None:
            continue
        try:
            data = await frame.evaluate(MARK_JS, {"start": next_id, "limit": MAX_ELEMENTS - len(elements)})
        except Exception as e:  # cross-origin frame that refuses evaluation, or navigating
            logger.debug("frame %d not observable: %s", index, type(e).__name__)
            continue
        if index == 0:
            main = data
        next_id = data["next"]
        for raw in data["elements"]:
            raw = {k: v for k, v in raw.items() if k in ElementInfo.__dataclass_fields__}
            info = ElementInfo(**raw, frame_index=index)
            info.x += offset[0]
            info.y += offset[1]
            elements.append(info)
        if data["text"]:
            texts.append(data["text"] if index == 0 else f"[frame {index}] {data['text']}")
        captcha = captcha or data["captcha"]
        modal = modal or data["modal"]
        errors.extend(e for e in data["errors"] if e not in errors)
        invalid += data["invalid_fields"]
        if len(elements) >= MAX_ELEMENTS:
            break

    if main is None:
        raise RuntimeError("could not read the page")

    aliases = session.value_aliases
    for e in elements:
        e.value = _clean_value(e.value, aliases)
        if e.options:
            for o in e.options:
                o["text"] = redact_text(o["text"])

    otp = any(
        "otp" in f"{e.label} {e.name} {e.placeholder}".lower() or "one time" in e.label.lower()
        or e.autocomplete == "one-time-code"
        for e in elements if e.tag in ("input",) and e.type in ("text", "tel", "number", "password", "")
    )
    if any("captcha" in f"{e.label} {e.name} {e.placeholder}".lower() for e in elements):
        captcha = True

    if screenshot:
        masked = [v for v in session.sensitive_values if v]
        try:
            if masked:
                for frame in page.frames:
                    try:
                        await frame.evaluate(MASK_JS, masked)
                    except Exception:
                        pass
            raw_shot = await page.screenshot(type="jpeg", quality=75, full_page=False)
        finally:
            if masked:
                for frame in page.frames:
                    try:
                        await frame.evaluate(UNMASK_JS)
                    except Exception:
                        pass
        shot, thumb = _annotate(raw_shot, elements)
    else:
        shot = thumb = b""

    url = main["url"]
    url_changed = session.last_url is not None and url.split("#")[0] != session.last_url.split("#")[0]
    obs = Observation(
        url=url,
        title=redact_text(main["title"]),
        elements=elements,
        text=_clean_text("\n".join(texts), aliases)[:TEXT_LIMIT],
        screenshot=shot,
        thumbnail=thumb,
        has_captcha=bool(captcha),
        has_otp_field=otp,
        has_modal=bool(modal),
        url_changed=url_changed,
        error_banners=[_clean_text(e, aliases) for e in errors[:8]],
        invalid_fields=invalid,
        viewport=(int(main["viewport"]["w"]), int(main["viewport"]["h"])),
        scroll_y=main["scroll_y"],
        page_height=main["page_h"],
        tab_count=len([p for p in session.pages if not p.is_closed()]),
        dialogs=[{"type": d.type, "message": d.message, "accepted": d.accepted} for d in session.pop_dialogs()],
    )
    session.last_observation = obs
    session.last_url = url
    return obs
