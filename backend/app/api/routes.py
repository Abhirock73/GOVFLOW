"""REST API endpoints for GovFlow AI."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from langgraph.types import Command
from pydantic import BaseModel, Field

from ..browser.session import get_session_manager
from ..config import get_settings
from ..discovery.resolver import Resolver, confirm_candidate
from ..graph.graph import get_compiled_graph
from ..intake.profile_builder import IntakeService
from ..models import (
    CitizenProfile,
    DocumentKind,
    DocumentRef,
    FieldSource,
    FormPlan,
    GapReport,
    HumanReply,
    HumanRequest,
    HumanRequestKind,
    Job,
    JobStatus,
    ProfileField,
    Receipt,
    ResolutionResult,
    ServiceRequest,
    new_id,
)
from ..planner.gap_analysis import analyze_gaps, convert_for_requirement
from ..planner.requirements import FormPlanner
from ..security import get_store
from .ws import get_hub

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory registry of active jobs and background runner tasks
_JOBS: dict[str, Job] = {}
_JOB_PLANS: dict[str, FormPlan] = {}
_JOB_GAPS: dict[str, GapReport] = {}
_JOB_RESOLUTIONS: dict[str, ResolutionResult] = {}
_JOB_TASKS: dict[str, asyncio.Task] = {}
_JOB_AGENTS: dict[str, Any] = {}  # Persistent AgentLoop instances per job


def _save_job_state(job_id: str, job: Job | None = None) -> None:
    store = get_store()
    j = job or _JOBS.get(job_id)
    if j:
        try:
            store.save_record(job_id, "job_meta", j.model_dump_json().encode("utf-8"))
        except Exception as e:
            logger.warning("Could not persist job %s: %s", job_id, e)
    plan = _JOB_PLANS.get(job_id)
    if plan:
        try:
            store.save_record(job_id, "job_plan", plan.model_dump_json().encode("utf-8"))
        except Exception:
            pass
    gap = _JOB_GAPS.get(job_id)
    if gap:
        try:
            store.save_record(job_id, "job_gap", gap.model_dump_json().encode("utf-8"))
        except Exception:
            pass


def _get_or_load_job(job_id: str) -> Job | None:
    job = _JOBS.get(job_id)
    if job and job.service:
        return job

    store = get_store()
    rec = store.load_record(job_id, "job_meta")
    if rec:
        try:
            job = Job.model_validate_json(rec.decode("utf-8"))
            _JOBS[job_id] = job
            plan_rec = store.load_record(job_id, "job_plan")
            if plan_rec:
                _JOB_PLANS[job_id] = FormPlan.model_validate_json(plan_rec.decode("utf-8"))
            gap_rec = store.load_record(job_id, "job_gap")
            if gap_rec:
                _JOB_GAPS[job_id] = GapReport.model_validate_json(gap_rec.decode("utf-8"))
            if job.service:
                return job
        except Exception as e:
            logger.warning("Could not load persisted job %s: %s", job_id, e)

    portal_url = None
    b_meta = store.load_record(job_id, "browser_meta")
    if b_meta:
        try:
            import json
            data = json.loads(b_meta.decode("utf-8"))
            portal_url = data.get("active_url")
        except Exception:
            pass

    if job is None:
        if store.load_profile(job_id) is not None or b_meta is not None:
            job = Job(job_id=job_id, status=JobStatus.INTAKE)
            _JOBS[job_id] = job
        else:
            return None

    if not job.service and portal_url and portal_url not in ("http://localhost", "http://localhost/"):
        job.service = ServiceRequest.from_user_input(portal_url)
    return job


class CreateJobRequest(BaseModel):
    text: str | None = None
    url: str | None = None


class ConfirmPortalRequest(BaseModel):
    url: str
    confirmed: bool = True


class ProfileUpdateRequest(BaseModel):
    profile: CitizenProfile


@router.post("/jobs", response_model=Job)
async def create_job(req: CreateJobRequest) -> Job:
    input_text = (req.url or req.text or "").strip()
    if not input_text:
        raise HTTPException(status_code=400, detail="Either 'text' or 'url' must be provided.")

    service = ServiceRequest.from_user_input(input_text)
    job_id = new_id()
    settings = get_settings()
    store = get_store()
    session_mgr = get_session_manager()

    # Initialize empty profile
    profile = CitizenProfile()
    store.save_profile(job_id, profile)

    job = Job(
        job_id=job_id,
        status=JobStatus.RESOLVING,
        service=service,
    )
    _JOBS[job_id] = job
    _save_job_state(job_id, job)

    # Immediately pre-warm browser session if user provided a link
    if service.portal_url:
        target_portal = str(service.portal_url)
        if target_portal not in ("http://localhost", "http://localhost/"):
            asyncio.create_task(session_mgr.get_or_create(job_id, start_url=target_portal, restore=True))

    # Kick off background resolution
    async def run_resolution():
        try:
            resolver = Resolver(settings=settings)
            res = await resolver.resolve(service, job_id=job_id)
            _JOB_RESOLUTIONS[job_id] = res

            if res.recommended and not res.needs_user_choice:
                job.service = confirm_candidate(res, res.recommended)
                job.status = JobStatus.PLANNING
                _save_job_state(job_id, job)
                # Pre-warm browser session so the live view is immediately available to citizen
                if job.service.portal_url:
                    asyncio.create_task(session_mgr.get_or_create(job_id, start_url=str(job.service.portal_url), restore=True))
                # Trigger planning
                planner = FormPlanner(settings=settings)
                plan = await planner.plan(str(job.service.portal_url), service_name=job.service.service_name, job_id=job_id)
                _JOB_PLANS[job_id] = plan
                _save_job_state(job_id, job)

                gap = analyze_gaps(plan, profile, job.service)
                _JOB_GAPS[job_id] = gap
                _save_job_state(job_id, job)
                job.status = JobStatus.AWAITING_MISSING_DATA if gap.questions_for_user else JobStatus.INTAKE
                _save_job_state(job_id, job)
            else:
                job.status = JobStatus.AWAITING_PORTAL_CONFIRMATION
                cand_url = (res.candidates[0].url if res.candidates else None) or (str(service.portal_url) if service.portal_url else None)
                if cand_url and cand_url not in ("http://localhost", "http://localhost/"):
                    asyncio.create_task(session_mgr.get_or_create(job_id, start_url=cand_url, restore=True))
                job.pending_request = HumanRequest(
                    kind=HumanRequestKind.CONFIRM_PORTAL,
                    question=res.message_for_user or "Please confirm the target official portal:",
                    options=[c.url for c in res.candidates],
                )
                _save_job_state(job_id, job)

            hub = get_hub()
            await hub.broadcast(job_id, {
                "type": "status",
                "status": job.status.value,
                "service": job.service.model_dump() if job.service else None,
                "resolution": res.model_dump() if res else None,
            })
        except Exception as e:
            logger.error("Resolution failed for job %s: %s", job_id, e)
            job.status = JobStatus.FAILED
            err_msg = str(e)
            if "authentication method" in err_msg or "api_key" in err_msg.lower():
                err_msg = "Anthropic API Key is missing. Please set ANTHROPIC_API_KEY in your .env file."
            job.error = err_msg
            hub = get_hub()
            await hub.broadcast(job_id, {
                "type": "status",
                "status": job.status.value,
                "error": job.error,
            })

    asyncio.create_task(run_resolution())
    return job


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    job = _get_or_load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.post("/jobs/{job_id}/confirm-portal")
async def confirm_portal(job_id: str, req: ConfirmPortalRequest) -> dict[str, Any]:
    job = _get_or_load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    res = _JOB_RESOLUTIONS.get(job_id)
    settings = get_settings()
    store = get_store()
    session_mgr = get_session_manager()
    profile = store.load_profile(job_id) or CitizenProfile()

    candidate = next((c for c in (res.candidates if res else []) if c.url == req.url), None)
    if candidate and res:
        job.service = confirm_candidate(res, candidate)
    else:
        job.service = ServiceRequest(
            raw_user_text=job.service.raw_user_text if job.service else "",
            input_mode=ServiceRequest.from_user_input(req.url).input_mode,
            portal_url=req.url,
        )

    job.status = JobStatus.PLANNING
    job.pending_request = None
    _save_job_state(job_id, job)
    if job.service and job.service.portal_url:
        asyncio.create_task(session_mgr.get_or_create(job_id, start_url=str(job.service.portal_url), restore=True))

    # Run planning in background
    async def run_plan():
        try:
            planner = FormPlanner(settings=settings)
            plan = await planner.plan(str(job.service.portal_url), service_name=job.service.service_name, job_id=job_id)
            _JOB_PLANS[job_id] = plan

            gap = analyze_gaps(plan, profile, job.service)
            _JOB_GAPS[job_id] = gap
            job.status = JobStatus.AWAITING_MISSING_DATA if gap.questions_for_user else JobStatus.INTAKE
            _save_job_state(job_id, job)

            hub = get_hub()
            await hub.broadcast(job_id, {
                "type": "plan_ready",
                "plan": plan.model_dump(),
                "gap_report": gap.model_dump(),
                "status": job.status.value,
            })
        except Exception as e:
            logger.error("Planning failed for job %s: %s", job_id, e)
            job.status = JobStatus.FAILED
            job.error = str(e)

    asyncio.create_task(run_plan())
    return {"status": "ok", "message": "Portal confirmed; planning started"}


@router.get("/jobs/{job_id}/plan")
async def get_plan(job_id: str) -> dict[str, Any]:
    plan = _JOB_PLANS.get(job_id)
    gap = _JOB_GAPS.get(job_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Form plan not ready yet.")
    return {
        "plan": plan.model_dump(),
        "gap_report": gap.model_dump() if gap else None,
    }


@router.get("/jobs/{job_id}/profile")
async def get_profile(job_id: str) -> CitizenProfile:
    settings = get_settings()
    store = get_store()
    profile = store.load_profile(job_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return profile


@router.put("/jobs/{job_id}/profile")
async def update_profile(job_id: str, req: ProfileUpdateRequest) -> CitizenProfile:
    settings = get_settings()
    store = get_store()
    current = store.load_profile(job_id) or CitizenProfile()

    for field_name in CitizenProfile.FIELD_NAMES:
        new_f = getattr(req.profile, field_name, None)
        if new_f and new_f.is_set:
            setattr(current, field_name, new_f)
    if req.profile.documents:
        current.documents = req.profile.documents
    store.save_profile(job_id, current)

    # Re-evaluate gap analysis if plan exists
    plan = _JOB_PLANS.get(job_id)
    job = _JOBS.get(job_id)
    if plan and job and job.service:
        gap = analyze_gaps(plan, current, job.service)
        _JOB_GAPS[job_id] = gap

    hub = get_hub()
    await hub.broadcast(job_id, {
        "type": "profile_updated",
        "profile": current.model_dump(),
    })
    return current


@router.post("/jobs/{job_id}/documents")
async def upload_document(
    job_id: str,
    file: UploadFile = File(...),
    doc_key: str = Form(...),
    kind: str = Form("other"),
) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    content = await file.read()
    filename = file.filename or f"{doc_key}.bin"
    mime_type = file.content_type or "application/octet-stream"

    # Check requirement from plan if available
    plan = _JOB_PLANS.get(job_id)
    req = next((r for r in (plan.required_documents if plan else []) if r.key == doc_key), None)

    if req is not None:
        try:
            converted = convert_for_requirement(content, mime_type, filename, req)
            content = converted.data
            mime_type = converted.mime_type
            filename = converted.filename
        except Exception as e:
            logger.warning("Document auto-conversion failed: %s", e)

    try:
        doc_kind = DocumentKind(kind)
    except ValueError:
        doc_kind = DocumentKind.OTHER

    ref = store.save_document(job_id, doc_key, content, filename, mime_type, kind=doc_kind)

    # Attach to citizen profile
    profile = store.load_profile(job_id) or CitizenProfile()
    profile.add_document(ref)
    store.save_profile(job_id, profile)

    hub = get_hub()
    await hub.broadcast(job_id, {
        "type": "document_uploaded",
        "doc_key": doc_key,
        "filename": filename,
        "size_kb": len(content) // 1024,
    })
    return {"status": "ok", "doc_key": doc_key, "filename": filename, "size_bytes": len(content)}


@router.post("/jobs/{job_id}/intake/voice")
async def intake_voice(
    job_id: str,
    file: UploadFile = File(...),
    language: str = Form("en"),
) -> dict[str, Any]:
    try:
        settings = get_settings()
        store = get_store()
        audio_bytes = await file.read()

        intake_svc = IntakeService(settings=settings, store=store)
        update = await intake_svc.add_voice(job_id, audio_bytes, mime_type=file.content_type, language_hint=language)
        state = intake_svc.load_state(job_id)
        transcript = state.transcripts[-1].text if state.transcripts else ""
        profile = store.load_profile(job_id) or CitizenProfile()

        hub = get_hub()
        await hub.broadcast(job_id, {
            "type": "voice_transcribed",
            "transcript": transcript,
            "profile": profile.model_dump(),
        })
        return {
            "transcript": transcript,
            "profile": profile.model_dump(),
            "conflicts": [c.model_dump() for c in update.conflicts],
            "card": [c.model_dump() for c in update.card],
            "issues": update.issues,
        }
    except Exception as e:
        logger.exception("Error during voice intake for job %s: %s", job_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/jobs/{job_id}/intake/id")
async def intake_id_photo(
    job_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    try:
        settings = get_settings()
        store = get_store()
        image_bytes = await file.read()
        mime = file.content_type or "image/jpeg"

        intake_svc = IntakeService(settings=settings, store=store)
        update = await intake_svc.add_id_photos(job_id, [(image_bytes, mime)])
        profile = store.load_profile(job_id) or CitizenProfile()

        hub = get_hub()
        await hub.broadcast(job_id, {
            "type": "id_extracted",
            "profile": profile.model_dump(),
        })
        return {
            "profile": profile.model_dump(),
            "conflicts": [c.model_dump() for c in update.conflicts],
            "card": [c.model_dump() for c in update.card],
            "issues": update.issues,
        }
    except Exception as e:
        logger.exception("Error during ID intake for job %s: %s", job_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/jobs/{job_id}/start")
async def start_job_execution(job_id: str) -> dict[str, Any]:
    job = _get_or_load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    settings = get_settings()
    store = get_store()
    profile = store.load_profile(job_id) or CitizenProfile()
    session_mgr = get_session_manager()
    hub = get_hub()

    job.status = JobStatus.RUNNING
    _save_job_state(job_id, job)
    await hub.broadcast(job_id, {"type": "status", "status": job.status.value})

    async def run_automation():
        from ..agent.loop import AgentLoop
        from ..llm import LLM

        try:
            portal_url = str((job.service.portal_url if job.service else None) or "http://localhost")
            session = await session_mgr.get_or_create(job_id, start_url=portal_url, restore=True)

            async def on_step_callback(step):
                job.step_count += 1
                await hub.broadcast(job_id, {
                    "type": "step",
                    "step": step.model_dump(mode="json"),
                })

            async def on_human_callback(req):
                job.status = JobStatus.AWAITING_HUMAN
                job.pending_request = req
                _save_job_state(job_id, job)
                await hub.broadcast(job_id, {
                    "type": "human_request",
                    "request": req.model_dump(mode="json"),
                })

            agent = AgentLoop(
                session=session,
                profile=profile,
                service=job.service,
                plan=_JOB_PLANS.get(job_id),
                gap_report=_JOB_GAPS.get(job_id),
                llm=LLM(settings),
                store=store,
                settings=settings,
                job_id=job_id,
                on_step=on_step_callback,
                on_human_request=on_human_callback,
            )
            _JOB_AGENTS[job_id] = agent

            res = await agent.run_until_pause_or_done()
            if agent.receipt is not None:
                job.receipt = agent.receipt
                job.status = JobStatus.COMPLETED
                _save_job_state(job_id, job)
                await hub.broadcast(job_id, {
                    "type": "receipt",
                    "receipt": agent.receipt.model_dump(mode="json"),
                    "status": job.status.value,
                })
        except Exception as e:
            logger.error("Execution error for job %s: %s", job_id, e)
            job.status = JobStatus.FAILED
            job.error = str(e)
            _save_job_state(job_id, job)
            await hub.broadcast(job_id, {"type": "status", "status": job.status.value, "error": str(e)})

    _JOB_TASKS[job_id] = asyncio.create_task(run_automation())
    return {"status": "ok", "message": "Automation started"}


@router.post("/jobs/{job_id}/human-reply")
async def handle_human_reply(job_id: str, reply: HumanReply) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    profile = store.load_profile(job_id) or CitizenProfile()
    session_mgr = get_session_manager()
    hub = get_hub()

    job = _get_or_load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    prev_req = job.pending_request
    job.pending_request = None
    job.status = JobStatus.RUNNING

    if reply.text:
        text = reply.text.strip()
        q_lower = (prev_req.question.lower() if prev_req else "")
        email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if email_match:
            profile.email = ProfileField(value=email_match.group(0), confidence=1.0, source=FieldSource.USER_EDIT)
            store.save_profile(job_id, profile)
            await hub.broadcast(job_id, {
                "type": "chat",
                "sender": "agent",
                "text": f"Saved your email: {email_match.group(0)} to profile.",
            })
        elif "name" in q_lower and len(text) > 1:
            profile.name = ProfileField(value=text, confidence=1.0, source=FieldSource.USER_EDIT)
            store.save_profile(job_id, profile)
        elif "phone" in q_lower or "mobile" in q_lower:
            digits = re.sub(r"\D", "", text)
            if digits:
                profile.phone = ProfileField(value=digits[-10:], confidence=1.0, source=FieldSource.USER_EDIT)
                store.save_profile(job_id, profile)
        if job.service:
            job.service.extra_params["last_answer"] = text

    _save_job_state(job_id, job)

    await hub.broadcast(job_id, {
        "type": "human_reply_received",
        "reply": reply.model_dump(mode="json"),
        "status": job.status.value,
        "profile": profile.model_dump(),
    })

    # Resume the persistent agent loop if one exists, otherwise create a fresh one

    async def resume_automation():
        from ..agent.loop import AgentLoop
        from ..llm import LLM

        try:
            session = session_mgr.get(job_id)
            if not session or session.closed:
                portal_url = str((job.service.portal_url if job.service else None) or "http://localhost")
                session = await session_mgr.get_or_create(job_id, start_url=portal_url, restore=True)

            async def on_step_callback(step):
                job.step_count += 1
                await hub.broadcast(job_id, {
                    "type": "step",
                    "step": step.model_dump(mode="json"),
                })

            async def on_human_callback(req):
                job.status = JobStatus.AWAITING_HUMAN
                job.pending_request = req
                _save_job_state(job_id, job)
                await hub.broadcast(job_id, {
                    "type": "human_request",
                    "request": req.model_dump(mode="json"),
                })

            # Reuse the persistent agent so submit_guard / loop_guard state is preserved
            agent: AgentLoop = _JOB_AGENTS.get(job_id)  # type: ignore[assignment]
            if agent is None:
                agent = AgentLoop(
                    session=session,
                    profile=profile,
                    service=job.service,
                    plan=_JOB_PLANS.get(job_id),
                    gap_report=_JOB_GAPS.get(job_id),
                    llm=LLM(settings),
                    store=store,
                    settings=settings,
                    job_id=job_id,
                    on_step=on_step_callback,
                    on_human_request=on_human_callback,
                )
                _JOB_AGENTS[job_id] = agent
            else:
                # Update callbacks and session in case they changed
                agent.on_step = on_step_callback
                agent.on_human_request = on_human_callback
                agent.session = session
                # Sync profile in case email/name was captured during reply
                agent.profile = profile
                if hasattr(agent.actions, "resolver") and agent.actions.resolver:
                    agent.actions.resolver.profile = profile

            agent.resume_with_reply(reply)

            res = await agent.run_until_pause_or_done()
            if agent.receipt is not None:
                job.receipt = agent.receipt
                job.status = JobStatus.COMPLETED
                _save_job_state(job_id, job)
                _JOB_AGENTS.pop(job_id, None)  # Clean up completed agent
                await hub.broadcast(job_id, {
                    "type": "receipt",
                    "receipt": agent.receipt.model_dump(mode="json"),
                    "status": job.status.value,
                })
        except Exception as e:
            logger.error("Resume failed for job %s: %s", job_id, e, exc_info=True)
            job.status = JobStatus.FAILED
            job.error = str(e)
            _save_job_state(job_id, job)

    _JOB_TASKS[job_id] = asyncio.create_task(resume_automation())
    return {"status": "ok", "message": "Reply received and execution resumed", "profile": profile.model_dump()}


@router.get("/jobs/{job_id}/receipt")
async def get_receipt(job_id: str) -> Receipt:
    job = _JOBS.get(job_id)
    if not job or not job.receipt:
        raise HTTPException(status_code=404, detail="Receipt not ready yet.")
    return job.receipt


@router.delete("/jobs/{job_id}/data")
async def delete_job_data(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    session_mgr = get_session_manager()

    # Cancel any running task
    task = _JOB_TASKS.pop(job_id, None)
    if task and not task.done():
        task.cancel()

    # Close browser session
    await session_mgr.close(job_id)

    # Securely delete all encrypted files from disk
    removed = store.delete_job_data(job_id)

    # Clear memory
    _JOBS.pop(job_id, None)
    _JOB_PLANS.pop(job_id, None)
    _JOB_GAPS.pop(job_id, None)
    _JOB_RESOLUTIONS.pop(job_id, None)

    return {"status": "ok", "deleted": True, "job_id": job_id, "removed_files": removed}
