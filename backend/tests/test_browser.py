"""Browser driver tests against a small local site in real headless Chromium.

The fixture pages are generic test HTML, not a model of any real portal.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from app.browser.actions import Actions, ProfileValueResolver, match_option, ActionError
from app.browser.observe import observe
from app.browser.screencast import Screencaster
from app.browser.session import SessionManager
from app.browser.takeover import InputEvent, TakeoverController, TakeoverError
from app.models import DocumentKind
from app.security import JobDataStore

FORM = """<html><head><title>Application</title><script>
function loadDistricts(sel) {
  var d = document.getElementById('district'); d.options.length = 1;
  setTimeout(function () {
    var m = {"Bihar": ["Gaya", "Patna"], "Uttar Pradesh": ["Lucknow", "Varanasi"]}[sel.value] || [];
    m.forEach(function (n) { var o = document.createElement('option'); o.text = n; o.value = n; d.add(o); });
  }, 300);
}
function nextStep() { if (!document.getElementById('name').value) document.getElementById('err').style.display = ''; }
function doSubmit() { if (confirm('Are you sure you want to submit?')) location.href = '/done.html'; }
</script></head><body>
<table>
<tr><td>Name of Applicant *</td><td><input id="name" name="txtName"></td></tr>
<tr><td>Aadhaar No.</td><td><input name="txtAadhaar" maxlength="12"></td></tr>
<tr><td>Date of Birth</td><td><input name="txtDob" placeholder="DD/MM/YYYY"></td></tr>
<tr><td>DOB (calendar)</td><td><input type="date" name="dobIso"></td></tr>
<tr><td>Pick date</td><td><input name="picker" readonly value=""></td></tr>
<tr><td>Category</td><td><select name="cat"><option value="">--Select--</option><option value="1">General</option>
  <option value="2">General (EWS)</option><option value="3">OBC</option></select></td></tr>
<tr><td>Institution</td><td><select name="inst"><option value="">--Select--</option>
  <option value="a">Government Degree College, Rampur</option><option value="b">Govt. Degree College Rampur (Evening)</option></select></td></tr>
<tr><td>State</td><td><select name="state" onchange="loadDistricts(this)"><option value="">--Select--</option>
  <option>Bihar</option><option>Uttar Pradesh</option></select></td></tr>
<tr><td>District</td><td><select id="district" name="district"><option value="">--Select--</option></select></td></tr>
<tr><td>Gender</td><td><input type="radio" name="g" value="M">Male <input type="radio" name="g" value="F">Female</td></tr>
<tr><td>Photo</td><td><input type="file" name="photo" accept=".jpg"></td></tr>
<tr><td><img src="/captcha.png" alt="captcha image" width="120" height="40"></td><td><input name="txtCaptcha" placeholder="Enter captcha"></td></tr>
<tr><td>Enter OTP</td><td><input name="otp"></td></tr>
</table>
<input type="checkbox" name="decl"> I hereby declare the above is true.
<span id="err" style="display:none;color:#ff0000">Please fill all mandatory fields</span><br>
<button type="button" onclick="nextStep()">Next</button>
<button type="button" onclick="doSubmit()">Submit</button>
<button type="button" onclick="document.cookie='sid=abc123; path=/'">Login</button>
<a href="https://partner.example.com/">Partner site</a>
<a href="/help.html" target="_blank">Open help</a>
<iframe src="/frame.html" width="300" height="80" style="margin-top:20px"></iframe>
</body></html>"""

PAGES = {
    "/form.html": FORM,
    "/frame.html": '<html><body><label for="m">Mobile Number</label><input id="m" name="mobile"></body></html>',
    "/done.html": "<html><title>Done</title><body>Application submitted. Reference SCH-1</body></html>",
    "/help.html": "<html><title>Help</title><body>Help page</body></html>",
    "/modal.html": "<html><body><div role='dialog' style='position:fixed;top:0;left:0;width:80%;height:80%;"
                   "background:#fff;z-index:999'>Important notice <button>Close</button></div></body></html>",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGES.get(urlparse(self.path).path, "")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")

JOB = "job_browser01"


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def fast(settings):
    settings.action_delay_min_seconds = 0
    settings.action_delay_max_seconds = 0
    settings.browser_headless = True
    return settings


@pytest.fixture
async def env(fast, site, profile):
    store = JobDataStore(fast)
    mgr = SessionManager(fast, store=store)
    session = await mgr.get_or_create(
        JOB, start_url=site + "/form.html", restore=False,
        navigation_guard=lambda url: urlparse(url).hostname in ("127.0.0.1", None),
    )
    actions = Actions(session, resolver=ProfileValueResolver(profile, {"course": "B.Sc."}), profile=profile, store=store)
    yield session, actions, mgr, store, site
    await mgr.shutdown()


def el(obs, label):
    return next(e for e in obs.elements if e.label == label)


async def test_observe_elements_and_flags(env):
    session, actions, *_ = env
    obs = await observe(session)
    assert obs.title == "Application"
    name = el(obs, "Name of Applicant *")
    assert name.tag == "input" and name.in_viewport
    cat = el(obs, "Category")
    assert [o["text"] for o in cat.options] == ["--Select--", "General", "General (EWS)", "OBC"]
    female = el(obs, "Female")
    assert female.type == "radio" and female.group == "Gender"
    mobile = el(obs, "Mobile Number")
    assert mobile.in_iframe and mobile.y > name.y  # iframe offset applied
    assert obs.has_captcha and obs.has_otp_field and not obs.has_modal and not obs.url_changed
    assert obs.screenshot[:3] == b"\xff\xd8\xff" and obs.thumbnail[:3] == b"\xff\xd8\xff"
    prompt = obs.to_prompt()
    assert '"Category"' in prompt and "General (EWS)" in prompt and "in-iframe" in prompt

    r = await actions.click(el(obs, "Next").id)
    assert r.ok
    obs = await observe(session)
    assert "Please fill all mandatory fields" in obs.error_banners


async def test_modal_detection(env):
    session, _, _, _, site = env
    await observe(session)
    await session.goto(site + "/modal.html")
    obs = await observe(session)
    assert obs.has_modal and obs.url_changed


async def test_type_profile_reference_is_masked_everywhere(env):
    session, actions, *_ = env
    obs = await observe(session)
    r = await actions.type(el(obs, "Aadhaar No.").id, "profile.id_number")
    assert r.ok, r.message
    assert "234567890123" not in str(r.masked_args) and "XXXXXXXX0123" in r.masked_args["text"]
    obs = await observe(session)
    aadhaar = el(obs, "Aadhaar No.")
    assert aadhaar.value == "[profile.id_number]"
    assert "234567890123" not in obs.to_prompt() and "234567890123" not in obs.text
    # The real value is on the page, and the temporary screenshot mask was removed.
    assert await session.page.locator("input[name=txtAadhaar]").input_value() == "234567890123"
    assert await session.page.locator("[data-gf-mask]").count() == 0

    r = await actions.type(el(obs, "Date of Birth").id, "profile.dob|DD/MM/YYYY")
    assert r.ok and await session.page.locator("input[name=txtDob]").input_value() == "15/08/2004"
    r = await actions.type(el(obs, "DOB (calendar)").id, "profile.dob|DD/MM/YYYY")
    assert r.ok and await session.page.locator("input[name=dobIso]").input_value() == "2004-08-15"
    r = await actions.type(el(obs, "Name of Applicant *").id, "extra.course")
    assert r.ok and await session.page.locator("#name").input_value() == "B.Sc."


async def test_type_errors(env):
    session, actions, *_ = env
    obs = await observe(session)
    assert (await actions.type(el(obs, "Pick date").id, "x")).error_code == "readonly"
    assert (await actions.type(el(obs, "Category").id, "x")).error_code == "wrong_action"
    r = await actions.type(el(obs, "Aadhaar No.").id, "12345678901234")
    assert not r.ok and r.error_code == "value_mismatch" and "12 characters" in r.message
    r = await actions.type(el(obs, "Name of Applicant *").id, "profile.email")
    assert r.ok
    session_profile = actions.resolver.profile
    session_profile.phone.value = None
    r = await actions.type(el(obs, "Name of Applicant *").id, "profile.phone")
    assert r.error_code == "missing_data" and "never guess" in r.message
    assert (await actions.click(9999)).error_code == "not_found"


async def test_select_option_matching_and_cascade(env):
    session, actions, *_ = env
    obs = await observe(session)
    r = await actions.select_option(el(obs, "Category").id, "profile.category|label")
    assert r.ok and "General (EWS)" in r.message
    r = await actions.select_option(el(obs, "Institution").id, "Degree College Rampur")
    assert r.error_code == "ambiguous" and len(r.details["candidates"]) == 2
    assert (await actions.select_option(el(obs, "Category").id, "Martian")).error_code == "no_match"
    assert (await actions.select_option(el(obs, "District").id, "Gaya")).error_code == "no_options"
    assert (await actions.select_option(el(obs, "State").id, "bihar")).ok
    obs = await observe(session)
    r = await actions.select_option(el(obs, "District").id, "Gaya")
    assert r.ok, r.message


def test_match_option_unit():
    opts = [{"text": "General", "value": "1"}, {"text": "General (EWS)", "value": "2"}, {"text": "OBC", "value": "3"}]
    assert match_option(opts, "General")["value"] == "1"
    assert match_option(opts, "general ews")["value"] == "2"
    assert match_option(opts, "3")["text"] == "OBC"
    with pytest.raises(ActionError) as e:
        match_option(opts, "Gen")
    assert e.value.code == "ambiguous"


async def test_check_and_upload(env, profile):
    session, actions, _, store, _ = env
    ref = store.save_document(JOB, "photo", DocumentKind.PHOTO, "me.jpg", "image/jpeg", b"\xff\xd8\xff" + b"0" * 2000)
    profile.documents = [ref]
    obs = await observe(session)
    assert (await actions.check(el(obs, "Female").id)).ok
    assert (await actions.check(el(obs, "Female").id, checked=False)).error_code == "invalid_args"
    assert (await actions.check(el(obs, "I hereby declare the above is true.").id)).ok
    r = await actions.upload_file(el(obs, "Photo").id, "photo")
    assert r.ok, r.message
    assert await session.page.locator("input[name=photo]").evaluate("e => e.files[0].name") == "me.jpg"
    assert not list(store.settings.uploads_dir.glob(".up-*"))  # decrypted temp copy is gone
    r = await actions.upload_file(el(obs, "Photo").id, "income_certificate")
    assert r.error_code == "missing_data" and "photo" in r.details["candidates"]


async def test_domain_guard_dialogs_and_tabs(env):
    session, actions, *_ = env
    obs = await observe(session)
    r = await actions.click(el(obs, "Partner site").id)
    assert r.error_code == "blocked_navigation" and "127.0.0.1" in session.page.url
    assert r.url_after.endswith("/form.html")  # back on the portal, not Chrome's error page

    obs = await observe(session)  # the agent re-observes after every action
    r = await actions.click(el(obs, "Submit").id)
    assert r.ok and not r.navigated
    assert r.dialogs and r.dialogs[0]["type"] == "confirm" and not r.dialogs[0]["accepted"]

    r = await actions.click(el(obs, "Open help").id)
    assert r.ok and r.new_tab and session.page.url.endswith("/help.html")
    await session.switch_to(0)

    session.allow_confirm_dialogs = True  # only after the citizen approved submitting
    obs = await observe(session)
    r = await actions.click(el(obs, "Submit").id)
    assert r.ok and r.navigated and r.url_after.endswith("/done.html") and r.dialogs[0]["accepted"]


async def test_stale_ids_and_misc_actions(env):
    session, actions, *_ = env
    obs = await observe(session)
    await session.page.reload()
    r = await actions.click(el(obs, "Next").id)
    assert r.error_code == "stale"
    assert (await actions.click_xy(5000, 10, "test")).error_code == "invalid_args"
    assert (await actions.click_xy(10, 10, "")).error_code == "invalid_args"
    assert (await actions.press_key("Control+W")).error_code == "invalid_args"
    assert (await actions.press_key("Tab")).ok
    assert (await actions.scroll("down", 400)).ok
    assert (await actions.wait(0.2)).ok
    assert (await actions.run("nope", {})).error_code == "invalid_args"
    assert (await actions.run("click", {})).error_code == "invalid_args"


async def test_checkpoint_and_restore(fast, site):
    store = JobDataStore(fast)
    mgr = SessionManager(fast, store=store)
    session = await mgr.get_or_create(JOB, start_url=site + "/form.html", restore=False)
    obs = await observe(session)
    await Actions(session).click(el(obs, "Login").id)
    await session.goto(site + "/help.html")
    await mgr.close(JOB)  # saves a checkpoint
    raw = (fast.profiles_dir / f"{JOB}.browser_state.rec").read_bytes()
    assert b"abc123" not in raw  # cookies are encrypted at rest
    await mgr.shutdown()

    mgr2 = SessionManager(fast, store=JobDataStore(fast))  # simulates a backend restart
    restored = await mgr2.get_or_create(JOB, start_url=site + "/form.html")
    assert restored.page.url.endswith("/help.html")
    cookies = await restored.context.cookies()
    assert any(c["name"] == "sid" and c["value"] == "abc123" for c in cookies)
    await mgr2.shutdown()


async def test_screencast_frames(env):
    session, *_ = env
    caster = Screencaster(session)
    q = caster.subscribe()
    await caster.start()
    await session.page.evaluate("document.body.style.background = '#eef'")
    frame = await asyncio.wait_for(q.get(), timeout=10)
    assert frame.data_b64 and frame.device_width > 0 and frame.seq >= 1
    await caster.stop()
    caster.unsubscribe(q)


async def test_takeover_blocks_agent_and_forwards_input(env, fast):
    session, actions, *_ = env
    obs = await observe(session)
    name = el(obs, "Name of Applicant *")
    takeover = TakeoverController(session)
    with pytest.raises(TakeoverError):
        await takeover.dispatch(InputEvent(kind="text", text="x"))
    await takeover.begin("captcha")
    r = await actions.click(name.id)
    assert r.error_code == "takeover_active"
    cx, cy = name.center
    # The viewer canvas is half the viewport size; coordinates are scaled up.
    await takeover.dispatch(InputEvent(kind="mouse", action="click", x=cx / 2, y=cy / 2,
                                       canvas_width=fast.browser_viewport_width / 2,
                                       canvas_height=fast.browser_viewport_height / 2))
    await takeover.dispatch(InputEvent(kind="text", text="Asha"))
    await takeover.dispatch(InputEvent(kind="key", action="press", key="Backspace"))
    await takeover.dispatch(InputEvent(kind="key", action="press", key="a"))
    assert await session.page.locator("#name").input_value() == "Asha"
    duration = await takeover.end()
    assert duration >= 0 and not takeover.active
    assert (await actions.click(name.id)).ok
