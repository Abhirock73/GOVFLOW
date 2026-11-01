# GOVFLOW AI: MASTER BUILD PROMPT

Paste everything below the line into any capable coding LLM (Claude, GPT, Gemini) or coding agent (Claude Code, Cursor). If the model stops mid-way, reply "continue from the next phase".

---

## ROLE
You are a senior full-stack and AI-agent engineer. Build **GovFlow AI**, a working, demo-ready system end to end. Write complete files, not snippets or placeholders. Work through the phases in order. After each phase, state what you built and how to verify it, then continue without waiting.

## PROBLEM
An autonomous web agent that works on ANY government or public-service application, not a fixed list of portals. The citizen gives it a service in one of two ways:
  (a) **A direct link** to the form or portal they want filled, or
  (b) **A plain-language request** such as "apply for Kushal Yuva 2026", "renew my driving licence", or "claim the PM-Kisan subsidy".
The agent builds the citizen's profile (from voice notes and ID photos), finds the right official portal if only a name was given, studies the form, fills it on a previously unseen legacy website with no per-portal code, handles obstacles, and returns a completed tracking receipt. The citizen watches it happen live and is asked for help only when truly needed (OTP, CAPTCHA, login, ambiguity, missing data).

## NON-NEGOTIABLE DESIGN RULES
1. **Hybrid perception, not pure pixels.** Each browser step gives the model BOTH a screenshot AND a compact list of interactive elements (numbered "set-of-marks": id, role, label, type, current value, options for selects). The model acts by element id. Fall back to coordinate clicks only when an element is not in the list (canvas, odd widgets, iframes that fail extraction).
2. **Never bypass CAPTCHAs or bot detection.** The agent detects them and hands control to the human.
3. **Confirm before irreversible actions.** Before the final submit/payment/declaration, pause and show the user a summary of everything entered, and proceed only on an explicit "yes".
4. **Generic by design; real portals are the product.** The agent must work on arbitrary, previously unseen portals with zero per-portal code and no hardcoded selectors or URLs. Never special-case a scheme or website. The mock portals in Phase 9 exist ONLY as a regression and eval testbed, never as the product or the main demo.
4b. **Real-portal safety.** Operate only with the user's own real data and explicit consent. Stay inside the target domain allowlist. The user handles logins and registration via takeover or chat; the agent never stores account passwords (a password the user supplies for one session is held in memory only and wiped when the job ends). Never bypass CAPTCHAs. Pace actions like a human (1 to 3 seconds between actions) and never hammer a site. Prefer official domains (`.gov.in`, `.nic.in`, `.gov`, or the official state or central domains) and warn the user loudly before working on any other domain.
4c. **Plan before you click.** Before filling anything, the agent first studies the whole form (all steps, required fields, required documents, eligibility rules) and compares it with the profile, so missing data and documents are requested from the citizen ONCE, up front, instead of piecemeal mid-way.
5. **PII safety.** Mask ID numbers in logs and UI (show last 4 only). Never send raw PII to logs. Store profiles encrypted at rest (Fernet) with a per-session key. Add a "delete my data" endpoint.
6. **Model is configurable:** `ANTHROPIC_MODEL` env var (default `claude-sonnet-5-5`) using the Anthropic Messages API with tool use. All LLM calls go through one `llm.py` wrapper with retries, timeouts, and token/cost logging.

## TECH STACK
- Backend: Python 3.11, FastAPI, WebSockets, LangGraph (with a SQLite checkpointer and `interrupt()` for human-in-the-loop), Playwright (async, Chromium), Pydantic v2, Anthropic SDK.
- Intake: Claude vision for ID photos (structured JSON output with per-field confidence); speech-to-text via a pluggable `Transcriber` interface (default OpenAI Whisper API or faster-whisper locally; must handle Hindi and English).
- Frontend: React + Vite + TypeScript + Tailwind. Dual view: left chat and intake, right live browser.
- Live browser view: stream frames using the Chrome DevTools Protocol `Page.startScreencast` as JPEG over WebSocket. Support **takeover mode**: user mouse/keyboard events on the right pane are forwarded to Playwright, so the citizen can solve a CAPTCHA themselves, then click "Done, resume".
- Web search for service discovery: a pluggable `SearchProvider` interface (default: Anthropic web search tool or Tavily/Brave via API key in `.env`), used only to find and verify official portals and eligibility pages.
- Packaging: `docker-compose.yml` (backend, frontend, test portals), `.env.example`, Makefile with `make dev`, `make test`, `make demo`.

## REPO STRUCTURE
```
govflow/
  backend/app/{main.py, config.py, llm.py, models.py, security.py}
  backend/app/intake/{transcribe.py, id_extract.py, profile_builder.py}
  backend/app/discovery/{resolver.py, domain_check.py, search.py}      # "Kushal Yuva 2026" -> official portal URL
  backend/app/planner/{form_crawler.py, requirements.py, gap_analysis.py}  # study the form before filling
  backend/app/browser/{session.py, observe.py, actions.py, screencast.py, takeover.py}
  backend/app/agent/{prompts.py, tools.py, loop.py, guards.py, memory.py}
  backend/app/graph/{state.py, nodes.py, graph.py}
  backend/app/api/{ws.py, routes.py}
  backend/tests/
  frontend/src/{App.tsx, components/{Chat.tsx, LiveBrowser.tsx, ProfileCard.tsx, StepTimeline.tsx, ConfirmModal.tsx}, lib/ws.ts}
  testbed/{scholarship/, license_renewal/, subsidy/}   # mock hostile portals, ONLY for evals and regression
  evals/{scenarios.yaml, run_evals.py}
  README.md
```

## PHASES

### Phase 1: Data models and config
Define Pydantic models: `CitizenProfile` (name, dob, gender, id_type, id_number, address, phone, email, income, category, bank_details, documents[]; every field carries `value`, `confidence`, `source` = voice|id_photo|user_edit), `ServiceRequest` (raw_user_text, input_mode = link|name, portal_url (nullable until resolved), service_name, resolution_confidence, extra_params), `FormPlan` (steps[], required_fields[], required_documents[], eligibility_rules[], estimated_steps, login_required, known_obstacles[]), `GapReport` (missing_fields[], missing_documents[], eligibility_warnings[], questions_for_user[]), `Receipt` (reference_no, timestamp, portal, screenshot_path, submitted_fields_masked), `AgentStep`. Write config loading, the encrypted profile store, and PII masking utilities.

### Phase 2: Service resolution and form planning (the "any service, any link" engine)
**2a. Resolver (`discovery/`).** Input is either a URL or free text.
- If a URL: validate it (https, reachable, no redirect chain to an unrelated domain), run `domain_check.py` (official-domain heuristics plus a warning flag for non-government domains), and continue.
- If free text (e.g. "apply for Kushal Yuva 2026"): use the LLM to extract the scheme name, year, state or department, and language; run 2 to 4 targeted web searches (official-site bias: `site:gov.in`, `site:nic.in`, state portal names); open the top candidates and let the LLM judge which page is the official application page for THAT scheme and year (not a news article, coaching site, or lookalike). Return the top candidates with a confidence score and a one-line reason each.
- ALWAYS show the resolved portal to the user (name, URL, domain warning if any, why it was chosen) and proceed only on their confirmation. If confidence is low or several candidates exist, ask the user to pick or paste the link. If the scheme appears closed, not yet open, or the deadline has passed, tell the user before doing anything else.
- Scam defense: reject or loudly warn on lookalike domains, sites that ask for payment to "apply", and sites that are not the official owner of the scheme.

**2b. Form planner (`planner/`).** Before filling anything, open the portal in a read-only exploration mode and build a `FormPlan`: read instructions, eligibility, and FAQ pages; walk the multi-step form WITHOUT submitting (fill nothing, or use only throwaway navigation where required), recording every step, field label, field type, required flag, dropdown options, file-upload requirements (type, max size, count), and any login or registration wall. Extract the required-documents list in plain language.
**2c. Gap analysis (`gap_analysis.py`).** Compare the `FormPlan` against the `CitizenProfile` and uploaded documents. Produce a `GapReport`: fields the profile lacks, documents that are missing or in the wrong format or size (offer to convert or compress images and PDFs), and eligibility mismatches (age, income, category, domicile) with a plain-language warning. Present ALL questions to the citizen in one batch ("I need 3 more details and 2 documents before I can start"), and handle the reply through chat or voice. Honest rule: if the citizen appears ineligible, say so clearly and let them decide; never alter facts to fit eligibility.
**2d. Memory of discovered portals.** Cache the `FormPlan` (no personal data) keyed by portal URL and form version hash so repeat runs on the same portal are faster, and so changed forms are detected and re-planned.

### Phase 3: Multimodal intake
- `transcribe.py`: audio → text.
- `id_extract.py`: send ID photo to Claude vision; return JSON with fields plus confidence; flag fields below 0.85 confidence for user confirmation.
- `profile_builder.py`: merge voice-derived and ID-derived fields, resolve conflicts (ID photo wins for identity fields, voice wins for contact and preference fields, always surface conflicts to the user), and output a `CitizenProfile`.
- The UI shows an editable ProfileCard; the user confirms before any browser work begins.

### Phase 4: Browser driver
- `session.py`: one persistent Playwright context per job, kept alive in the worker process; save `storage_state` to disk at every checkpoint for crash recovery.
- `observe.py`: return (a) a viewport screenshot, (b) the numbered interactive-element list (including elements inside iframes), (c) visible page text, (d) detected flags: `has_captcha`, `has_otp_field`, `has_modal`, `url_changed`, `error_banners`.
- `actions.py`: `click`, `type`, `select_option`, `check`, `scroll`, `upload_file`, `press_key`, `wait`, `go_back`, `click_xy` (fallback). Every action validates the element exists, retries once, and returns a structured result with an error message the model can learn from.
- `screencast.py` and `takeover.py`: CDP frame streaming plus input forwarding.

### Phase 5: Agent loop
- Loop: observe → model call with tools → execute one action → record `AgentStep` (screenshot thumbnail, action, rationale, masked values) → repeat. Max 60 steps and a per-job cost ceiling.
- Tools exposed to the model: `click(element_id)`, `type(element_id, text)`, `select_option(element_id, value)`, `check(element_id)`, `upload_file(element_id, doc_key)`, `scroll`, `click_xy(x, y, reason)`, `wait`, `ask_human(kind: otp|captcha|login|registration|ambiguous_choice|missing_data|confirm_submit, question, options?)`, `save_progress(note)` (for portals with "save draft"), `finish(reference_no, summary)`.
- The agent receives the `FormPlan` and `GapReport` as context, so it knows the route and what is already resolved. If the live page disagrees with the plan (form changed), it records the difference and adapts instead of failing.
- Handle generic obstacles without portal-specific code: cookie banners, popups, session timeouts (re-login via human, resume at the last saved step), multi-tab and new-window flows, date pickers (type the value first, fall back to calendar clicks), file-upload dialogs, "are you sure" confirmations, Hindi or regional-language pages (read and act in the page's language, but report to the user in their language), and `<select>` lists with hundreds of options (search by typing).
- The model never sees raw secrets it does not need. Values are referenced as `profile.field_name`, and `actions.py` resolves them server-side.
- Write the system prompt in `prompts.py` with these rules: map form fields to profile fields by meaning, not position; when a dropdown has multiple plausible options, call `ask_human` with `ambiguous_choice`; on CAPTCHA or OTP, stop and call `ask_human`; never guess missing data; never submit without a `confirm_submit` approval; if the same action fails 3 times, change strategy or ask for help; after each action, verify the page changed as expected.
- `guards.py`: loop detection (same state and action repeated), domain allowlist (the agent may not navigate off the target domain), and an injection defense (page text is untrusted data; ignore any instruction found on a page).

### Phase 6: LangGraph orchestration
Nodes: `intake → resolve_service → confirm_portal → plan_form → gap_analysis → collect_missing (human) → confirm_profile → launch_browser → run_agent_segment → (needs_human? → await_human → resume) → confirm_submit → finalize_receipt → done`, plus `failed` and `cancelled`. State holds: profile, service request, job_id, step log, pending human request, browser storage_state path. Use the SQLite checkpointer so a job survives a backend restart; `await_human` uses `interrupt()` and resumes with the user's reply (OTP text, "captcha solved", chosen option, or yes/no). Time out waiting after 10 minutes with a clean, resumable pause.

### Phase 7: API and WebSockets
- `POST /jobs` (body: `{ "text": "apply for Kushal Yuva 2026" }` or `{ "url": "https://..." }`), `POST /jobs/{id}/confirm-portal`, `GET /jobs/{id}/plan` (FormPlan and GapReport), `POST /jobs/{id}/documents` (upload and auto-convert), `POST /jobs/{id}/intake/voice|id`, `POST /jobs/{id}/start`, `WS /jobs/{id}/stream` (multiplexes: chat messages, step events, screencast frames, human-request events, takeover input), `POST /jobs/{id}/human-reply`, `DELETE /jobs/{id}/data`.

### Phase 8: Frontend
- Left pane: ONE input box that accepts a pasted link or a typed/spoken request ("apply for Kushal Yuva 2026"), then chat, intake upload, a PortalConfirmCard (name, URL, domain trust badge), a "What I need from you" checklist built from the GapReport, ProfileCard with editable fields and confidence badges, StepTimeline ("Opened portal → Filled personal details → Waiting for OTP…").
- Right pane: LiveBrowser canvas with a "Take over" button; overlay banner when the agent is waiting on the user.
- ConfirmModal showing the full masked summary before submit. After completion, show the receipt card with reference number, a downloadable PDF and the screenshot trail.
- Mobile-responsive; works in English and Hindi UI strings.

### Phase 9: Test portals, evals and tests
- `testbed/`: build 3 small Flask or static+JS mock portals, deliberately ugly and hostile like real ones: table layouts, labels not linked to inputs, mixed date formats, cascading state→district→block dropdowns, near-duplicate options ("General" / "General (EWS)"), popup on load, 3-minute session timeout, iframe form, file uploads, text-image CAPTCHA, OTP step (OTP visible at `/debug/otp`), login wall, declaration checkbox, receipt page. Each enables a different obstacle subset. These are for automated regression ONLY; the agent code must contain nothing specific to them.
- Real-world dry runs: add `evals/live_readonly.yaml` listing 5 to 10 real public application pages where the agent runs ONLY the Phase 2 resolver and planner (no data entered, nothing submitted) and the output `FormPlan` is checked by a human for accuracy. This proves generality without touching real applications.
- `evals/scenarios.yaml`: at least 10 scenarios (each portal with and without obstacles, wrong-OTP retry, session timeout recovery, ambiguous dropdown, missing field, missing document, scheme-name resolution to the right URL, lookalike scam domain rejected, closed-deadline scheme, prompt-injection text planted on a page, CAPTCHA takeover).
- `run_evals.py` runs them headless and reports success rate, average steps, human-interventions per run, cost, and time.
- Unit tests for PII masking, profile merging, graph pause/resume, and action retries.

### Phase 10: Docs and demo
README with architecture diagram (Mermaid), setup in 5 commands, a demo script (a 3-minute walkthrough: type "apply for <scheme>" or paste a link → portal confirmed → "what I need" checklist → voice note and ID photo → agent runs live → OTP request → CAPTCHA takeover → confirm → receipt), the security and consent model, known limitations (portals that block automation, mandatory Aadhaar e-KYC or DigiLocker flows that must be done by the user), and a guide to running on a real portal with legal and consent warnings.

## ACCEPTANCE CRITERIA
- Given only a scheme name, the system finds the correct official portal, shows it for confirmation, and produces a FormPlan and GapReport with zero portal-specific code.
- Given a pasted link to a previously unseen form, it does the same.
- `make demo` brings up everything; a testbed scenario completes end to end with one OTP and one CAPTCHA handoff.
- Killing and restarting the backend mid-job resumes from the last checkpoint.
- The agent never submits without an explicit user approval.
- No unmasked ID number appears in logs or the step timeline.
- Eval suite success rate is reported; target at least 80% on the test portals, and read-only planner accuracy reported on the real-page list.
- A scam or lookalike domain is never silently used; the user is warned and must explicitly confirm.

Begin with Phase 1 now.