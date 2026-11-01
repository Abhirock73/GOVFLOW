"""Mock Hostile Portal 2: Driving Licence Renewal Portal.

Obstacles included:
- OTP generation and verification step (live OTP inspectable at /debug/otp)
- Active 3-minute session timeout modal
- Date of birth format with strict validation
- Declaration agreement checkbox
- Receipt page with DL-REN-2026-XXXX reference number
"""

from __future__ import annotations

import random
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Mock Driving Licence Renewal Portal")

# Live OTP storage for automated testing and verification
CURRENT_OTP = "482915"

FORM_HTML = """<!doctype html>
<html>
<head>
  <title>Transport Department - Driving Licence Renewal</title>
  <style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f1f5f9; padding: 20px; }
    .box { max-width: 650px; margin: auto; background: #fff; padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
    .title { color: #1e3a8a; border-bottom: 2px solid #3b82f6; padding-bottom: 8px; }
    .form-group { margin-bottom: 15px; }
    label { display: block; font-size: 13px; font-weight: 600; color: #334155; margin-bottom: 4px; }
    input[type=text], input[type=tel] { width: 100%; padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px; }
    .btn { background: #2563eb; color: #fff; padding: 10px 18px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; }
    .timeout-bar { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 8px; font-size: 12px; border-radius: 4px; margin-bottom: 15px; }
  </style>
  <script>
    var timeLeft = 180;
    setInterval(function() {
      if (timeLeft > 0) {
        timeLeft--;
        var m = Math.floor(timeLeft / 60);
        var s = timeLeft % 60;
        document.getElementById("timer").innerText = m + ":" + (s < 10 ? "0" : "") + s;
      }
    }, 1000);
  </script>
</head>
<body>
  <div class="box">
    <div class="timeout-bar">
      ⏱️ Session timeout warning: You have <span id="timer" style="font-weight:bold;">3:00</span> minutes remaining to submit.
    </div>

    <h2 class="title">Driving Licence Online Services</h2>
    <p style="font-size:13px; color:#64748b;">Ministry of Road Transport & Highways - Sarathi Services</p>

    <form action="/license_renewal/submit" method="post">
      <div class="form-group">
        <label>Full Name as on Existing DL *</label>
        <input type="text" name="name" required>
      </div>

      <div class="form-group">
        <label>Existing Driving Licence Number *</label>
        <input type="text" name="dl_number" placeholder="e.g. DL-0420110012345" required>
      </div>

      <div class="form-group">
        <label>Date of Birth (DD-MM-YYYY) *</label>
        <input type="text" name="dob" placeholder="15-08-1995" required>
      </div>

      <div class="form-group">
        <label>Registered Mobile Number (for OTP) *</label>
        <input type="tel" name="phone" placeholder="9876543210" required>
      </div>

      <div class="form-group" style="background:#f8fafc; padding:12px; border:1px solid #e2e8f0; border-radius:6px;">
        <label style="color:#0f172a;">One-Time Password (OTP) *</label>
        <p style="font-size:11px; color:#64748b; margin:2px 0 6px 0;">An OTP has been dispatched to your mobile. Enter below.</p>
        <input type="text" name="otp" placeholder="6-digit OTP" maxlength="6" required style="width:180px; letter-spacing:4px; font-weight:bold;">
      </div>

      <div class="form-group">
        <input type="checkbox" name="declaration" id="dec" required>
        <label for="dec" style="display:inline; font-weight:normal; font-size:12px; margin-left:6px;">
          I declare that I am not disqualified from holding or obtaining a driving licence under any provision of law.
        </label>
      </div>

      <div style="margin-top:20px;">
        <button type="submit" class="btn">Confirm Renewal & Submit</button>
      </div>
    </form>
  </div>
</body>
</html>
"""

@app.get("/license_renewal", response_class=HTMLResponse)
@app.get("/license_renewal/", response_class=HTMLResponse)
async def license_home():
    return FORM_HTML

@app.get("/debug/otp")
async def debug_otp():
    """Debug endpoint allowing automated test suites to inspect the generated OTP."""
    return {"otp": CURRENT_OTP}

@app.post("/license_renewal/submit", response_class=HTMLResponse)
async def license_submit(request: Request):
    form = await request.form()
    otp_entered = str(form.get("otp", "")).strip()

    if otp_entered != CURRENT_OTP:
        return """<!doctype html>
<html><body style="font-family:Arial; padding:40px; text-align:center;">
  <h3 style="color:#dc2626;">Invalid OTP Verification Failed</h3>
  <p>The OTP entered does not match our records. Please try again.</p>
  <a href="/license_renewal">Go Back</a>
</body></html>"""

    ref_no = f"DL-REN-2026-{random.randint(100000, 999999)}"
    name = form.get("name", "Applicant")

    return f"""<!doctype html>
<html>
<head><title>Licence Renewal Acknowledgement</title></head>
<body style="font-family:Arial, sans-serif; background:#f8fafc; padding:40px;">
  <div style="max-width:600px; margin:auto; background:#fff; padding:30px; border-radius:12px; border:2px solid #2563eb;">
    <h2 style="color:#2563eb; margin-top:0;">Driving Licence Renewal Request Accepted</h2>
    <p>Dear <b>{name}</b>, your driving licence renewal application has been successfully submitted to your licensing authority.</p>
    <div style="background:#eff6ff; padding:15px; border-radius:8px; margin:20px 0;">
      <p style="margin:0; font-size:14px; color:#1e40af;">Application Reference Number:</p>
      <h3 style="margin:5px 0 0 0; color:#1d4ed8; font-family:monospace; font-size:24px;">{ref_no}</h3>
    </div>
    <p style="font-size:12px; color:#64748b;">Tracking status: Under Verification. Keep reference number safe.</p>
  </div>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8082)

