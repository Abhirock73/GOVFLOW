"""Read-only exploration of an unseen portal.

Opens the portal in an isolated browser context and records every form step it
can reach WITHOUT submitting anything:

* every non-GET request is aborted at the network layer, so no form can be
  submitted and no OTP can be triggered, whatever the page's JavaScript does;
* top-level navigation off the target site is blocked;
* only GET links (apply / instructions pages) and in-page controls that look
  like tabs or wizard "Next" buttons are clicked; anything resembling submit,
  pay, save, send OTP, verify or declare is never clicked;
* field *values* are never read, only structure (labels, types, options).

The extraction script is generic DOM heuristics: there is no per-portal code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urldefrag, urlparse

from ..config import Settings, get_settings
from ..discovery.domain_check import redirect_is_acceptable, same_site
from ..security import redact_text

logger = logging.getLogger(__name__)

APPLY_RE = re.compile(
    r"\bapply\b|apply online|application form|online application|new registration|fresh (application|registration)|"
    r"register|registration|new user|sign up|आवेदन|पंजीकरण|रजिस्ट्रेशन|नया पंजीकरण",
    re.I,
)
INFO_RE = re.compile(
    r"instruction|guideline|eligib|faq|how to apply|documents? required|required documents|notification|"
    r"advertisement|prospectus|user manual|help|दिशा|निर्देश|पात्रता|अधिसूचना|आवश्यक दस्तावेज",
    re.I,
)
NEXT_RE = re.compile(r"^(next|continue|proceed|next step|step \d+|आगे|अगला)\b|>>|»|→", re.I)
DANGER_RE = re.compile(
    r"submit|pay|payment|final|save|send|otp|verify|confirm|declare|delete|remove|logout|log out|sign out|"
    r"upload|reset|clear|print|cancel|download|e-?sign|जमा|सबमिट|भुगतान|सहेजें|भेजें|सत्यापित",
    re.I,
)
LOGOUT_RE = re.compile(r"logout|log out|sign out|लॉगआउट", re.I)

EXTRACT_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    if (el.type === 'file') return true;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return (r.width > 0 && r.height > 0) || el.offsetParent !== null;
  };
  const byFor = el => {
    if (!el.id) return '';
    const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    return l ? clean(l.innerText) : '';
  };
  const labelOf = el => {
    let t = byFor(el);
    if (!t) { const l = el.closest('label'); if (l) { const c = l.cloneNode(true); c.querySelectorAll('select,option').forEach(x => x.remove()); t = clean(c.innerText || c.textContent); } }
    if (!t && el.getAttribute('aria-labelledby')) t = clean(el.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    if (!t) t = clean(el.getAttribute('aria-label'));
    if (!t) {
      const td = el.closest('td,th');
      if (td) { let p = td.previousElementSibling; while (p && !clean(p.innerText)) p = p.previousElementSibling; if (p) t = clean(p.innerText); }
    }
    if (!t) { let p = el.previousSibling, n = 0; while (p && n < 4 && !t) { t = clean(p.textContent); p = p.previousSibling; n++; } }
    if (!t) {
      const box = el.closest('div,li,p,dd');
      if (box) { const c = box.cloneNode(true); c.querySelectorAll('input,select,textarea,option,script,button').forEach(x => x.remove()); t = clean(c.innerText || c.textContent); }
    }
    if (!t) t = clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) || clean(el.name);
    return t.slice(0, 160);
  };
  const hintOf = el => {
    const parts = []; let s = el.nextSibling, n = 0;
    while (s && n < 3) { const x = clean(s.textContent); if (x) parts.push(x); s = s.nextSibling; n++; }
    const ph = el.getAttribute('placeholder'); if (ph) parts.push(ph);
    const t = el.getAttribute('title'); if (t) parts.push(t);
    return clean(parts.join(' ')).slice(0, 200);
  };
  const isRequired = (el, label) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (/\*\s*:?\s*$/.test(label)) return true;
    const td = el.closest('td,th'); const prev = td && td.previousElementSibling;
    return !!(prev && /\*/.test(prev.innerText || ''));
  };
  const fields = [], radios = {};
  document.querySelectorAll('input,select,textarea').forEach(el => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.getAttribute('type') || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type) || !vis(el)) return;
    const label = labelOf(el);
    const f = { tag, type, name: el.name || '', id: el.id || '', label, hint: hintOf(el), required: isRequired(el, label),
      maxlength: el.maxLength > 0 ? el.maxLength : null, pattern: el.getAttribute('pattern') || '', accept: el.getAttribute('accept') || '',
      multiple: !!el.multiple, autocomplete: el.getAttribute('autocomplete') || '', disabled: !!el.disabled, readonly: !!el.readOnly,
      options: [], options_total: 0, option_label: '' };
    if (type === 'select') {
      const opts = Array.from(el.options).map(o => clean(o.text)).filter(t => t && !/^[-\s]*(select|choose|please select|--|चुनें)/i.test(t));
      f.options_total = opts.length; f.options = opts.slice(0, 300);
    }
    if (type === 'radio' || type === 'checkbox') {
      let ol = byFor(el); if (!ol) { const n = el.nextSibling; ol = clean(n && n.textContent); }
      ol = ol.slice(0, 100); f.option_label = ol;
      if (type === 'radio' && el.name) {
        const g = radios[el.name];
        if (g) { g.options.push(ol); g.options_total++; return; }
        f.options = [ol]; f.options_total = 1;
        const fs = el.closest('fieldset'); const leg = fs && fs.querySelector('legend');
        if (leg) f.label = clean(leg.innerText);
        else { const td = el.closest('td'); const p = td && td.previousElementSibling; if (p && clean(p.innerText)) f.label = clean(p.innerText); }
        radios[el.name] = f;
      } else if (!f.label || f.label === ol) { f.label = ol; }
    }
    fields.push(f);
  });
  const btnSel = 'button,input[type=submit],input[type=button],input[type=image],a[role=button],[role=tab],a[href]';
  const buttons = Array.from(document.querySelectorAll(btnSel)).filter(vis).slice(0, 400).map((el, i) => ({
    idx: i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(), role: el.getAttribute('role') || '',
    text: clean(el.innerText || el.value || el.getAttribute('aria-label') || el.title).slice(0, 100),
    href: el.tagName === 'A' ? el.href : '', in_form: !!el.closest('form') }));
  const body = document.body;
  const text = clean(body ? body.innerText : '').slice(0, 20000);
  const imgs = Array.from(document.images).map(i => ((i.alt || '') + ' ' + (i.src || '') + ' ' + (i.id || '')).toLowerCase());
  const anyVisible = sel => Array.from(document.querySelectorAll(sel)).some(vis);
  const flags = {
    captcha: imgs.some(s => s.includes('captcha')) || fields.some(f => /captcha/i.test(f.label + f.name + f.id)) ||
      !!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],.g-recaptcha,.h-captcha,[class*=captcha i]'),
    otp: fields.some(f => /\botp\b|one.?time/i.test(f.label + ' ' + f.name + ' ' + f.id) || f.autocomplete === 'one-time-code'),
    password: fields.some(f => f.type === 'password'),
    iframes: document.querySelectorAll('iframe').length,
    modal: anyVisible('[role=dialog],[aria-modal=true],.modal.show,.modal.in'),
    declaration: /declar|undertaking|hereby|घोषणा/i.test(text) && fields.some(f => f.type === 'checkbox'),
    payment: /pay now|make payment|payment gateway|pay fee|भुगतान करें/i.test(text),
    ekyc: /e-?kyc|aadhaar (based )?authentication|biometric|digilocker/i.test(text),
    session_timeout: /session (will )?(expire|time.?out)|सत्र समाप्त/i.test(text),
  };
  return { url: location.href, title: document.title || '', lang: document.documentElement.lang || '', text, fields, buttons, flags };
}
"""

FILLABLE = {"text", "email", "tel", "number", "date", "select", "textarea", "radio", "checkbox", "file",
            "password", "month", "url", "search", "datetime-local", "week", "time"}


@dataclass
class PageSnapshot:
    url: str
    title: str
    lang: str
    text: str
    fields: list[dict[str, Any]]
    buttons: list[dict[str, Any]]
    flags: dict[str, Any]
    via: str = "entry"
    kind: str = "other"  # form | login | info | other

    @property
    def fillable(self) -> list[dict[str, Any]]:
        return [f for f in self.fields if f["type"] in FILLABLE and not f.get("disabled")]

    def signature(self) -> str:
        sig = [
            (f["label"].lower(), f["type"], f.get("options_total", 0), tuple(f.get("options", [])[:50]))
            for f in self.fillable
            if "captcha" not in (f["label"] + f["name"]).lower()
        ]
        return hashlib.sha256(json.dumps(sig, ensure_ascii=False).encode()).hexdigest()[:16]


@dataclass
class InfoPage:
    url: str
    title: str
    text: str


@dataclass
class CrawlResult:
    start_url: str
    final_url: str = ""
    entry_title: str = ""
    form_pages: list[PageSnapshot] = field(default_factory=list)
    info_pages: list[InfoPage] = field(default_factory=list)
    blocked_requests: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    login_wall: bool = False
    registration_wall: bool = False

    def version_hash(self) -> str:
        """Fingerprint of the form structure; changes when the form changes."""
        sigs = [p.signature() for p in self.form_pages]
        payload = json.dumps({"sigs": sigs, "login": self.login_wall}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(snap: PageSnapshot) -> str:
    fill = snap.fillable
    non_pw = [f for f in fill if f["type"] != "password"]
    if snap.flags.get("password") and len(non_pw) <= 3:
        return "login"
    if len(fill) >= 2 or any(f["type"] == "file" for f in fill):
        # A lone search box is not an application form.
        if len(fill) <= 2 and all(re.search(r"search|खोज", f["label"] + f["name"], re.I) for f in fill):
            return "other"
        return "form"
    return "other"


class ReadOnlyViolation(RuntimeError):
    pass


class FormCrawler:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def _pause(self) -> None:
        s = self.settings
        await asyncio.sleep(random.uniform(s.action_delay_min_seconds, s.action_delay_max_seconds))

    def _nav_allowed(self, start_url: str, url: str) -> bool:
        if url.startswith(("about:", "data:", "blob:")):
            return True
        return same_site(start_url, url) or redirect_is_acceptable(start_url, url, self.settings)

    async def crawl(self, url: str, *, include_info: bool = True) -> CrawlResult:
        from playwright.async_api import async_playwright

        result = CrawlResult(start_url=url)
        s = self.settings
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=s.browser_headless)
            try:
                context = await browser.new_context(
                    viewport={"width": s.browser_viewport_width, "height": s.browser_viewport_height},
                    user_agent=s.http_user_agent, locale="en-IN", accept_downloads=False,
                )
                await context.route("**/*", lambda route: self._guard(route, url, result))
                page = await context.new_page()
                page.set_default_timeout(s.planner_page_timeout_seconds * 1000)
                page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
                context.on("page", lambda p: asyncio.ensure_future(self._close_popup(p, page, result)))
                await self._explore(page, url, result, include_info)
                await context.close()
            finally:
                await browser.close()
        return result

    async def _guard(self, route, start_url: str, result: CrawlResult) -> None:
        req = route.request
        if req.method not in ("GET", "HEAD", "OPTIONS"):
            result.blocked_requests.append(f"{req.method} {redact_text(urldefrag(req.url)[0])[:200]}")
            await route.abort()
            return
        if req.is_navigation_request() and req.frame.parent_frame is None and not self._nav_allowed(start_url, req.url):
            result.blocked_requests.append(f"NAVIGATE {urlparse(req.url).hostname}")
            await route.abort()
            return
        await route.continue_()

    async def _close_popup(self, popup, main_page, result: CrawlResult) -> None:
        if popup is main_page:
            return
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=5000)
            result.notes.append(f"Portal opened a new window: {urldefrag(popup.url)[0][:200]}")
        except Exception:
            pass
        await popup.close()

    async def _goto(self, page, url: str, result: CrawlResult) -> bool:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            if resp is not None and resp.status >= 400:
                result.errors.append(f"{url} returned HTTP {resp.status}")
                return False
            return True
        except Exception as e:
            result.errors.append(f"could not open {url}: {type(e).__name__}")
            return False

    async def snapshot(self, page, via: str) -> PageSnapshot:
        data = await page.main_frame.evaluate(EXTRACT_JS)
        fields = data["fields"]
        buttons = data["buttons"]
        flags = data["flags"]
        for i, frame in enumerate(page.frames[1:], start=1):
            try:
                fd = await frame.evaluate(EXTRACT_JS)
            except Exception:
                continue
            for f in fd["fields"]:
                f["in_iframe"] = True
                f["frame"] = i
            for b in fd["buttons"]:
                b["frame"] = i
            fields.extend(fd["fields"])
            buttons.extend(fd["buttons"])
            for k, v in fd["flags"].items():
                if isinstance(v, bool):
                    flags[k] = flags.get(k, False) or v
            if fd["text"]:
                data["text"] = (data["text"] + "\n" + fd["text"])[:20000]
        snap = PageSnapshot(url=urldefrag(data["url"])[0], title=data["title"], lang=data["lang"], text=data["text"],
                            fields=fields, buttons=buttons, flags=flags, via=via)
        snap.kind = classify(snap)
        return snap

    async def _explore(self, page, url: str, result: CrawlResult, include_info: bool) -> None:
        s = self.settings
        if not await self._goto(page, url, result):
            return
        entry = await self.snapshot(page, "entry")
        result.final_url = entry.url
        result.entry_title = entry.title
        seen_sigs: set[str] = set()
        visited_urls: set[str] = {entry.url}

        def record(snap: PageSnapshot) -> bool:
            if snap.kind == "login":
                result.login_wall = True
                if re.search(r"new user|register|sign up|registration|पंजीकरण", snap.text, re.I):
                    result.registration_wall = True
            if snap.kind not in ("form", "login"):
                return False
            sig = snap.signature()
            if sig in seen_sigs:
                return False
            seen_sigs.add(sig)
            result.form_pages.append(snap)
            return True

        record(entry)
        snapshots = [entry]

        # A) follow GET links that lead to the application / registration form.
        if not any(p.kind == "form" for p in result.form_pages):
            links = self._links(entry, url, APPLY_RE)
            for link in links[:4]:
                if len(result.form_pages) >= s.planner_max_form_pages or link in visited_urls:
                    continue
                visited_urls.add(link)
                await self._pause()
                if await self._goto(page, link, result):
                    snap = await self.snapshot(page, f"link:{link}")
                    snapshots.append(snap)
                    record(snap)
                    if any(p.kind == "form" for p in result.form_pages):
                        break

        # B) walk client-side steps (tabs, wizard "Next" buttons) on form pages.
        clicks = 0
        for form_page in list(result.form_pages):
            if form_page.kind != "form" or clicks >= s.planner_max_clicks:
                continue
            if page.url.split("#")[0] != form_page.url and not await self._goto(page, form_page.url, result):
                continue
            current_sig = form_page.signature()
            for target in self._safe_controls(form_page):
                if clicks >= s.planner_max_clicks or len(result.form_pages) >= s.planner_max_form_pages:
                    break
                clicks += 1
                await self._pause()
                if not await self._click(page, target):
                    continue
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                snap = await self.snapshot(page, f"click:{target['text']}")
                if snap.signature() != current_sig and record(snap):
                    current_sig = snap.signature()
                    snapshots.append(snap)

        if not result.form_pages:
            result.notes.append("No fillable form was reachable without logging in or submitting.")
        if result.blocked_requests:
            result.notes.append(f"Blocked {len(result.blocked_requests)} non-read-only request(s) during exploration.")

        # C) instructions / eligibility / FAQ pages and PDFs.
        if include_info and s.planner_max_info_pages:
            info_links: list[str] = []
            for snap in snapshots:
                for link in self._links(snap, url, INFO_RE):
                    if link not in info_links and link not in visited_urls:
                        info_links.append(link)
            for link in info_links[: s.planner_max_info_pages]:
                await self._pause()
                info = await self._read_info(page, link, result)
                if info:
                    result.info_pages.append(info)
            # Entry page text often carries the instructions itself.
            if entry.text:
                result.info_pages.insert(0, InfoPage(entry.url, entry.title, entry.text[:12000]))

    def _links(self, snap: PageSnapshot, start_url: str, pattern: re.Pattern[str]) -> list[str]:
        out: list[str] = []
        for b in snap.buttons:
            href = b.get("href") or ""
            if b["tag"] != "a" or not href.startswith(("http://", "https://")):
                continue
            if LOGOUT_RE.search(b["text"]) or not (pattern.search(b["text"]) or pattern.search(href.rsplit("/", 1)[-1])):
                continue
            href = urldefrag(href)[0]
            if self._nav_allowed(start_url, href) and href not in out:
                out.append(href)
        return out

    @staticmethod
    def _safe_controls(snap: PageSnapshot) -> list[dict[str, Any]]:
        out = []
        for b in snap.buttons:
            text = b["text"]
            if not text or DANGER_RE.search(text) or b["type"] == "submit" or b["tag"] == "input" and b["type"] == "image":
                continue
            is_tab = b["role"] == "tab"
            # A <button> with no type inside a form is a submit button.
            plain_button = b["type"] == "button" or (b["tag"] == "button" and b["type"] == "" and not b["in_form"])
            is_next = bool(NEXT_RE.search(text)) and plain_button
            is_hash_link = b["tag"] == "a" and (b.get("href") or "").split("#")[0] == snap.url and "#" in (b.get("href") or "")
            if is_tab or is_next or (is_hash_link and re.search(r"step|tab|section|चरण", text, re.I)):
                out.append(b)
        return out

    async def _click(self, page, target: dict[str, Any]) -> bool:
        idx = target.get("frame", 0)
        frame = page.frames[idx] if idx < len(page.frames) else page.main_frame
        want = " ".join(target["text"].split())
        try:
            for el in await frame.query_selector_all(target["tag"]):
                if not await el.is_visible():
                    continue
                text = " ".join(((await el.inner_text()) or (await el.get_attribute("value")) or "").split())
                if text[:100] != want:
                    continue
                if DANGER_RE.search(text) or (await el.get_attribute("type") or "").lower() == "submit":
                    raise ReadOnlyViolation(f"refused to click {text!r}")
                await el.click(timeout=5000, no_wait_after=True)
                return True
            return False
        except ReadOnlyViolation:
            raise
        except Exception as e:
            logger.info("planner click failed: %s", type(e).__name__)
            return False

    async def _read_info(self, page, link: str, result: CrawlResult) -> InfoPage | None:
        if link.lower().split("?")[0].endswith(".pdf"):
            try:
                resp = await page.context.request.get(link, timeout=20000, max_redirects=5)
                body = await resp.body()
                if resp.ok and len(body) <= 8 * 1024 * 1024:
                    from ..discovery.resolver import _pdf_text

                    return InfoPage(link, link.rsplit("/", 1)[-1], _pdf_text(body, max_pages=8)[:12000])
            except Exception as e:
                result.errors.append(f"could not read PDF {link}: {type(e).__name__}")
            return None
        if not await self._goto(page, link, result):
            return None
        try:
            text = await page.inner_text("body")
        except Exception:
            return None
        return InfoPage(urldefrag(page.url)[0], await page.title(), " ".join(text.split())[:12000])


async def render_page_text(url: str, settings: Settings | None = None) -> str:
    """Render a JavaScript-heavy page read-only and return its visible text."""
    from playwright.async_api import async_playwright

    settings = settings or get_settings()
    crawler = FormCrawler(settings)
    result = CrawlResult(start_url=url)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=settings.http_user_agent, accept_downloads=False)
            await context.route("**/*", lambda route: crawler._guard(route, url, result))
            page = await context.new_page()
            page.set_default_timeout(settings.planner_page_timeout_seconds * 1000)
            if not await crawler._goto(page, url, result):
                return ""
            return " ".join((await page.inner_text("body")).split())
        finally:
            await browser.close()
