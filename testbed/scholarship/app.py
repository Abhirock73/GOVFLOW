"""Mock Hostile Portal 1: Scholarship Application Portal.

Obstacles included:
- Table layout with unlinked labels
- Cascading State -> District dropdowns
- Near-duplicate options: General vs General (EWS)
- Dismissable popup banner on load
- File upload for marksheet
- Text CAPTCHA image with input field
- Mandatory declaration checkbox
- Receipt page with NSP-2026-XXXX reference number
"""

from __future__ import annotations

import random
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mock National Scholarship Portal")

POPUP_JS = """
<div id="advisoryPopup" style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;">
  <div style="background:#fff;padding:24px;border-radius:8px;max-width:500px;text-align:center;">
    <h3 style="color:#b91c1c;">IMPORTANT ADVISORY - NSP 2026</h3>
    <p>Please ensure you have read the scholarship guidelines before proceeding. Incomplete applications will be rejected.</p>
    <button onclick="document.getElementById('advisoryPopup').style.display='none';" style="padding:8px 16px;background:#0284c7;color:#fff;border:none;border-radius:4px;cursor:pointer;">I Understand and Acknowledge</button>
  </div>
</div>
"""

FORM_HTML = f"""<!doctype html>
<html>
<head>
  <title>National Scholarship Application Portal 2026</title>
  <style>
    body {{ font-family: 'Times New Roman', serif; background: #e2e8f0; margin: 20px; }}
    .container {{ max-width: 800px; margin: auto; background: #fff; padding: 25px; border: 2px solid #0f172a; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
    td {{ padding: 8px; border: 1px solid #cbd5e1; font-size: 13px; }}
    .header {{ text-align: center; border-bottom: 2px solid #0f172a; padding-bottom: 10px; }}
    .submit-btn {{ padding: 10px 20px; background: #15803d; color: #fff; font-weight: bold; border: none; cursor: pointer; }}
  </style>
  <script>
    function updateDistricts(stateSel) {{
      var distSel = document.getElementById("distSelect");
      distSel.innerHTML = "<option value=''>--Select District--</option>";
      var map = {{
        "Bihar": ["Patna", "Gaya", "Muzaffarpur", "Bhagalpur"],
        "Uttar Pradesh": ["Varanasi", "Lucknow", "Kanpur", "Prayagraj"],
        "Maharashtra": ["Mumbai", "Pune", "Nagpur"]
      }};
      var list = map[stateSel.value] || [];
      for (var i = 0; i < list.length; i++) {{
        var opt = document.createElement("option");
        opt.value = list[i];
        opt.innerText = list[i];
        distSel.appendChild(opt);
      }}
    }}
  </script>
</head>
<body>
  {POPUP_JS}
  <div class="container">
    <div class="header">
      <h2>GOVERNMENT SCHOLARSHIP PORTAL 2026</h2>
      <p>Ministry of Education - Central Sector Scheme</p>
    </div>

    <form action="/scholarship/submit" method="post" enctype="multipart/form-data">
      <table>
        <tr>
          <td>Applicant's Full Name *</td>
          <td><input type="text" name="cand_name" id="cand_name" size="35" required></td>
        </tr>
        <tr>
          <td>Date of Birth (DD/MM/YYYY) *</td>
          <td><input type="text" name="dob" id="dob" placeholder="DD/MM/YYYY" size="15" required></td>
        </tr>
        <tr>
          <td>Gender *</td>
          <td>
            <input type="radio" name="gender" value="male"> Male
            <input type="radio" name="gender" value="female"> Female
            <input type="radio" name="gender" value="other"> Other
          </td>
        </tr>
        <tr>
          <td>Category / Caste *</td>
          <td>
            <select name="category" required>
              <option value="">--Select Category--</option>
              <option value="general">General</option>
              <option value="general_ews">General (EWS)</option>
              <option value="obc">OBC</option>
              <option value="sc">SC</option>
              <option value="st">ST</option>
            </select>
          </td>
        </tr>
        <tr>
          <td>Aadhaar / ID Card Number *</td>
          <td><input type="text" name="id_number" size="25" required></td>
        </tr>
        <tr>
          <td>State of Domicile *</td>
          <td>
            <select name="state" onchange="updateDistricts(this)" required>
              <option value="">--Select State--</option>
              <option value="Bihar">Bihar</option>
              <option value="Uttar Pradesh">Uttar Pradesh</option>
              <option value="Maharashtra">Maharashtra</option>
            </select>
          </td>
        </tr>
        <tr>
          <td>District *</td>
          <td>
            <select name="district" id="distSelect" required>
              <option value="">--Select District--</option>
            </select>
          </td>
        </tr>
        <tr>
          <td>Upload High School Marksheet (.jpg/.pdf max 500KB) *</td>
          <td><input type="file" name="marksheet" accept=".jpg,.jpeg,.pdf" required></td>
        </tr>
        <tr>
          <td>Security Verification (CAPTCHA) *</td>
          <td>
            <div style="background:#f1f5f9;padding:6px;font-family:monospace;font-size:18px;font-weight:bold;letter-spacing:4px;display:inline-block;">
              7K9X2
            </div>
            <br>
            <input type="text" name="captcha" placeholder="Enter characters above" required style="margin-top:4px;">
          </td>
        </tr>
        <tr>
          <td colspan="2">
            <input type="checkbox" name="declaration" id="declaration" required>
            <label for="declaration">I hereby declare that all particulars stated in this application form are true and correct to the best of my knowledge.</label>
          </td>
        </tr>
      </table>

      <div style="margin-top: 20px; text-align: center;">
        <button type="submit" class="submit-btn">Final Submit Application</button>
      </div>
    </form>
  </div>
</body>
</html>
"""

@app.get("/scholarship", response_class=HTMLResponse)
@app.get("/scholarship/", response_class=HTMLResponse)
async def scholarship_home():
    return FORM_HTML

@app.post("/scholarship/submit", response_class=HTMLResponse)
async def scholarship_submit(request: Request):
    form = await request.form()
    ref_no = f"NSP-2026-{random.randint(100000, 999999)}"
    cand_name = form.get("cand_name", "Applicant")

    return f"""<!doctype html>
<html>
<head><title>Application Acknowledgement Receipt</title></head>
<body style="font-family:Arial, sans-serif; background:#f8fafc; padding:40px;">
  <div style="max-width:600px; margin:auto; background:#fff; padding:30px; border-radius:12px; border:2px solid #16a34a;">
    <h2 style="color:#16a34a; margin-top:0;">Application Submitted Successfully!</h2>
    <p>Thank you <b>{cand_name}</b>. Your scholarship application has been registered with the Ministry of Education.</p>
    <div style="background:#f0fdf4; padding:15px; border-radius:8px; margin:20px 0;">
      <p style="margin:0; font-size:14px;">Tracking / Reference Number:</p>
      <h3 style="margin:5px 0 0 0; color:#15803d; font-family:monospace; font-size:24px;">{ref_no}</h3>
    </div>
    <p style="font-size:12px; color:#64748b;">Please print or save this receipt for future reference.</p>
  </div>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8081)

