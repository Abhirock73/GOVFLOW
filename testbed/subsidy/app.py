"""Mock Hostile Portal 3: Direct Farmer Subsidy Portal (PM-Kisan style).

Obstacles included:
- Login / Registration wall (user credentials check)
- Iframe-embedded application form
- Bank account & IFSC code fields
- Scheme-specific field (Land holding in acres)
- Receipt page with SUB-2026-XXXX tracking number
"""

from __future__ import annotations

import random
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mock Farmer Subsidy Portal")

IFRAME_CONTENT = """<!doctype html>
<html>
<head>
  <style>
    body { font-family: sans-serif; font-size: 13px; margin: 0; padding: 10px; }
    .row { margin-bottom: 12px; }
    label { display: block; font-weight: bold; margin-bottom: 3px; }
    input[type=text], select { width: 95%; padding: 6px; border: 1px solid #ccc; border-radius: 3px; }
    button { background: #d97706; color: #fff; font-weight: bold; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; }
  </style>
</head>
<body>
  <form action="/subsidy/submit" method="post" target="_top">
    <div class="row">
      <label>Farmer / Beneficiary Full Name *</label>
      <input type="text" name="farmer_name" required>
    </div>

    <div class="row">
      <label>Aadhaar Number (12 digits) *</label>
      <input type="text" name="aadhaar" maxlength="14" required>
    </div>

    <div class="row">
      <label>Bank Account Number *</label>
      <input type="text" name="bank_account" required>
    </div>

    <div class="row">
      <label>Bank IFSC Code *</label>
      <input type="text" name="ifsc" style="text-transform:uppercase;" required>
    </div>

    <div class="row">
      <label>Cultivable Land Holding (in Acres) *</label>
      <input type="text" name="land_area" placeholder="e.g. 2.5" required>
    </div>

    <div class="row">
      <input type="checkbox" name="declaration" id="dec" required>
      <label for="dec" style="display:inline; font-weight:normal;">I certify that I am a small/marginal farmer eligible under the scheme.</label>
    </div>

    <div style="margin-top:15px;">
      <button type="submit">Submit Subsidy Application</button>
    </div>
  </form>
</body>
</html>
"""

MAIN_PAGE = """<!doctype html>
<html>
<head>
  <title>Direct Farmer Benefit Subsidy Portal 2026</title>
  <style>
    body { font-family: Arial, sans-serif; background: #fafaf9; margin: 0; padding: 0; }
    .topbar { background: #78350f; color: #fff; padding: 12px 20px; font-size: 16px; font-weight: bold; }
    .wrapper { max-width: 800px; margin: 25px auto; background: #fff; padding: 25px; border-radius: 8px; border: 1px solid #e7e5e4; }
    .iframe-box { border: 1px solid #d6d3d1; border-radius: 6px; padding: 5px; height: 420px; margin-top: 15px; }
  </style>
</head>
<body>
  <div class="topbar">PM-Kisan Direct Benefit Transfer Portal - 2026 Registration</div>
  <div class="wrapper">
    <h3 style="color:#78350f; margin-top:0;">Claim Farmer Income Support Installment</h3>
    <p style="font-size:13px; color:#57534e;">Please complete the verified form below. Payment will be credited directly to your registered bank account via Aadhaar-linked DBT.</p>

    <div class="iframe-box">
      <iframe src="/subsidy/form_iframe" width="100%" height="100%" frameborder="0"></iframe>
    </div>
  </div>
</body>
</html>
"""

@app.get("/subsidy", response_class=HTMLResponse)
@app.get("/subsidy/", response_class=HTMLResponse)
async def subsidy_home():
    return MAIN_PAGE

@app.get("/subsidy/form_iframe", response_class=HTMLResponse)
async def subsidy_iframe():
    return IFRAME_CONTENT

@app.post("/subsidy/submit", response_class=HTMLResponse)
async def subsidy_submit(request: Request):
    form = await request.form()
    ref_no = f"SUB-2026-{random.randint(100000, 999999)}"
    farmer_name = form.get("farmer_name", "Beneficiary")

    return f"""<!doctype html>
<html>
<head><title>Subsidy Registration Receipt</title></head>
<body style="font-family:Arial, sans-serif; background:#f5f5f4; padding:40px;">
  <div style="max-width:600px; margin:auto; background:#fff; padding:30px; border-radius:12px; border:2px solid #d97706;">
    <h2 style="color:#d97706; margin-top:0;">Subsidy Application Submitted!</h2>
    <p>Dear <b>{farmer_name}</b>, your DBT subsidy claim has been submitted to the Department of Agriculture & Farmers Welfare.</p>
    <div style="background:#fffbeb; padding:15px; border-radius:8px; margin:20px 0;">
      <p style="margin:0; font-size:14px; color:#92400e;">DBT Tracking Reference Number:</p>
      <h3 style="margin:5px 0 0 0; color:#b45309; font-family:monospace; font-size:24px;">{ref_no}</h3>
    </div>
    <p style="font-size:12px; color:#78716c;">Status: Dispatched to Public Financial Management System (PFMS).</p>
  </div>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8083)

