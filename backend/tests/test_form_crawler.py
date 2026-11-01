"""Read-only crawl against a small local site (real headless Chromium).

The fixture site is generic test HTML written for this test, not a model of
any real portal. The server records every POST it receives; the crawler must
cause none.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.planner.form_crawler import FormCrawler

INDEX = """<html><head><title>Demo Welfare Scheme 2026</title></head><body>
<h2>Demo Welfare Scheme</h2>
<a href="/apply.html">Apply Online</a> | <a href="/guidelines.html">Guidelines &amp; Eligibility</a> |
<a href="https://partner.example.com/">Partner site</a> | <a href="/logout">Logout</a>
</body></html>"""

GUIDELINES = """<html><head><title>Guidelines</title></head><body>
Eligibility: applicants aged 18 to 35. Annual family income must not exceed Rs 3,00,000.
Documents: photograph (JPG, max 200 KB) and income certificate (PDF).
</body></html>"""

APPLY = """<html><head><title>Application Form</title>
<script>
  fetch('/api/track', {method: 'POST', body: 'opened'});
  function show(n) { document.getElementById('sec1').style.display = n == 1 ? '' : 'none';
                     document.getElementById('sec2').style.display = n == 2 ? '' : 'none'; }
</script></head><body>
<div role="tablist">
  <button type="button" role="tab" onclick="show(1)">Personal Details</button>
  <button type="button" role="tab" onclick="show(2)">Bank Details</button>
</div>
<form method="post" action="/submit">
<table id="sec1">
  <tr><td>Name of Applicant <font color=red>*</font></td><td><input name="ctl00$txtName"></td></tr>
  <tr><td>Date of Birth</td><td><input name="ctl00$txtDob"> (DD/MM/YYYY)</td></tr>
  <tr><td>State *</td><td><select name="ddlState"><option>--Select--</option><option>Bihar</option><option>Uttar Pradesh</option></select></td></tr>
  <tr><td>District *</td><td><select name="ddlDistrict"><option>--Select--</option></select></td></tr>
  <tr><td>Gender</td><td><input type="radio" name="g" value="M">Male <input type="radio" name="g" value="F">Female</td></tr>
  <tr><td>Photograph *</td><td><input type="file" name="filePhoto" accept=".jpg,.jpeg"> (JPG only, max 200 KB)</td></tr>
</table>
<table id="sec2" style="display:none">
  <tr><td>Account Number *</td><td><input name="acc"></td></tr>
  <tr><td>IFSC Code *</td><td><input name="ifsc"></td></tr>
</table>
<input type="checkbox" name="decl"> I hereby declare that the above information is true.
<input type="submit" value="Submit Application">
</form>
<button type="button" onclick="fetch('/api/final', {method: 'POST'})">Final Submit</button>
<iframe src="/frame.html" width="400" height="120"></iframe>
</body></html>"""

FRAME = """<html><body><label for="m">Mobile Number</label><input id="m" name="mobile" required></body></html>"""

PAGES = {"/": INDEX, "/guidelines.html": GUIDELINES, "/apply.html": APPLY, "/frame.html": FRAME}


class _Handler(BaseHTTPRequestHandler):
    posts: list[str] = []

    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path.split("?")[0])
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def do_POST(self):  # noqa: N802
        type(self).posts.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    _Handler.posts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def fast_settings(settings):
    settings.action_delay_min_seconds = 0
    settings.action_delay_max_seconds = 0
    settings.browser_headless = True
    return settings


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


async def test_read_only_crawl(site, fast_settings):
    crawl = await FormCrawler(fast_settings).crawl(site + "/")

    assert _Handler.posts == [], "the crawler must never cause a POST"
    assert any("POST" in b and "/api/track" in b for b in crawl.blocked_requests)
    assert not any("/api/final" in b for b in crawl.blocked_requests), "Final Submit must never be clicked"

    assert crawl.form_pages, crawl.errors
    labels = {f["label"] for p in crawl.form_pages for f in p.fillable}
    assert "Name of Applicant *" in labels
    assert "Date of Birth" in labels and "Photograph *" in labels
    assert "Mobile Number" in labels  # from the iframe
    assert "Account Number *" in labels  # revealed by clicking the "Bank Details" tab
    assert len(crawl.form_pages) >= 2

    first = crawl.form_pages[0]
    by_label = {f["label"]: f for f in first.fillable}
    assert by_label["State *"]["options"] == ["Bihar", "Uttar Pradesh"]
    assert by_label["District *"]["options_total"] == 0
    assert by_label["Gender"]["options"] == ["Male", "Female"]
    assert "max 200 KB" in by_label["Photograph *"]["hint"]
    assert by_label["Mobile Number"]["in_iframe"] and by_label["Mobile Number"]["required"]
    assert first.flags["declaration"]
    assert all("value" not in f for f in first.fields)

    assert any("Guidelines" in p.title or "Eligibility" in p.text for p in crawl.info_pages)
    assert crawl.version_hash()
