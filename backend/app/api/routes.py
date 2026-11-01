"""REST API endpoints for GovFlow AI."""

from __future__ import annotations

import asyncio
import io
import logging
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
    Job,
    JobStatus,
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

    # Initialize empty profile
    profile = CitizenProfile()
    store.save_profile(job_id, profile)

    job = Job(
        job_id=job_id,
        status=JobStatus.RESOLVING,
        service=service,
    )
    _JOBS[job_id] = job

    # Kick off background resolution
    async def run_resolution():
        try:
            resolver = Resolver(settings=settings)
            res = await resolver.resolve(service, job_id=job_id)
            _JOB_RESOLUTIONS[job_id] = res

            if res.recommended and not res.needs_user_choice:
                job.service = confirm_candidate(res, res.recommended)
                job.status = JobStatus.PLANNING
                # Trigger planning
                planner = FormPlanner(settings=settings)
                plan = await planner.plan(str(job.service.portal_url), service_name=job.service.service_name, job_id=job_id)
                _JOB_PLANS[job_id] = plan

                gap = analyze_gaps(plan, profile, job.service)
                _JOB_GAPS[job_id] = gap
                job.status = JobStatus.AWAITING_MISSING_DATA if gap.questions_for_user else JobStatus.INTAKE

                hub = get_hub()
                await hub.broadcast(job_id, {
                    "type": "plan_ready",
                    "plan": plan.model_dump(),
                    "gap_report": gap.model_dump(),
                    "status": job.status.value,
                })
            else:
                job.status = JobStatus.AWAITING_PORTAL_CONFIRMATION
                job.pending_request = HumanRequest(
                    kind=HumanRequestKind.CONFIRM_PORTAL,
                    question=res.message_for_user or "Please confirm the target official portal:",
                    options=[c.url for c in res.candidates],
                )

            hub = get_hub()
            await hub.broadcast(job_id, {
                "type": "status",
                "status": job.status.value,
                "service": job.service.model_dump() if job.service else None,
                "resolution": res.model_dump() if res else None,
            })
            if job.pending_request:
                await hub.broadcast(job_id, {
                    "type": "human_request",
                    "request": job.pending_request.model_dump(mode="json"),
                })
            if res.message_for_user:
                await hub.broadcast(job_id, {
                    "type": "chat",
                    "text": res.message_for_user,
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
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.post("/jobs/{job_id}/confirm-portal")
async def confirm_portal(job_id: str, req: ConfirmPortalRequest) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    res = _JOB_RESOLUTIONS.get(job_id)
    settings = get_settings()
    store = get_store()
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

    # Run planning in background
    async def run_plan():
        try:
            planner = FormPlanner(settings=settings)
            plan = await planner.plan(str(job.service.portal_url), service_name=job.service.service_name, job_id=job_id)
            _JOB_PLANS[job_id] = plan

            gap = analyze_gaps(plan, profile, job.service)
            _JOB_GAPS[job_id] = gap
            job.status = JobStatus.AWAITING_MISSING_DATA if gap.questions_for_user else JobStatus.INTAKE

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
    store.save_profile(job_id, req.profile)

    # Re-evaluate gap analysis if plan exists
    plan = _JOB_PLANS.get(job_id)
    job = _JOBS.get(job_id)
    if plan and job and job.service:
        gap = analyze_gaps(plan, req.profile, job.service)
        _JOB_GAPS[job_id] = gap

    hub = get_hub()
    await hub.broadcast(job_id, {
        "type": "profile_updated",
        "profile": req.profile.model_dump(),
    })
    return req.profile


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

    ref = store.save_document(
        job_id=job_id,
        doc_key=doc_key,
        kind=doc_kind,
        filename=filename,
        mime_type=mime_type,
        data=content,
    )

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
    settings = get_settings()
    store = get_store()
    audio_bytes = await file.read()

    intake_svc = IntakeService(settings=settings, store=store)
    update = await intake_svc.add_voice(job_id, audio_bytes, mime_type=file.content_type, language=language)

    hub = get_hub()
    await hub.broadcast(job_id, {
        "type": "voice_transcribed",
        "transcript": update.transcript,
        "profile": update.profile.model_dump(),
    })
    return {
        "transcript": update.transcript,
        "profile": update.profile.model_dump(),
        "conflicts": [c.model_dump() for c in update.conflicts],
    }


@router.post("/jobs/{job_id}/intake/id")
async def intake_id_photo(
    job_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    image_bytes = await file.read()
    mime = file.content_type or "image/jpeg"

    intake_svc = IntakeService(settings=settings, store=store)
    update = await intake_svc.add_id_photos(job_id, [(image_bytes, mime)])

    hub = get_hub()
    await hub.broadcast(job_id, {
        "type": "id_extracted",
        "profile": update.profile.model_dump(),
    })
    return {
        "profile": update.profile.model_dump(),
        "conflicts": [c.model_dump() for c in update.conflicts],
    }


@router.post("/jobs/{job_id}/start")
async def start_job_execution(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    settings = get_settings()
    store = get_store()
    profile = store.load_profile(job_id) or CitizenProfile()
    session_mgr = get_session_manager()
    hub = get_hub()

    job.status = JobStatus.RUNNING
    await hub.broadcast(job_id, {"type": "status", "status": job.status.value})

    async def run_automation():
        from ..agent.loop import AgentLoop
        from ..llm import LLM

        try:
            portal_url = str(job.service.portal_url or "http://localhost")
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

            res = await agent.run_until_pause_or_done()
            if agent.receipt is not None:
                job.receipt = agent.receipt
                job.status = JobStatus.COMPLETED
                await hub.broadcast(job_id, {
                    "type": "receipt",
                    "receipt": agent.receipt.model_dump(mode="json"),
                    "status": job.status.value,
                })
        except Exception as e:
            logger.error("Execution error for job %s: %s", job_id, e)
            job.status = JobStatus.FAILED
            job.error = str(e)
            await hub.broadcast(job_id, {"type": "status", "status": job.status.value, "error": str(e)})

    _JOB_TASKS[job_id] = asyncio.create_task(run_automation())
    return {"status": "ok", "message": "Automation started"}


@router.post("/jobs/{job_id}/human-reply")
async def handle_human_reply(job_id: str, reply: HumanReply) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    hub = get_hub()
    job.pending_request = None
    job.status = JobStatus.RUNNING

    await hub.broadcast(job_id, {
        "type": "human_reply_received",
        "reply": reply.model_dump(mode="json"),
        "status": job.status.value,
    })

    # Restart automation loop with the human reply delivered
    settings = get_settings()
    store = get_store()
    profile = store.load_profile(job_id) or CitizenProfile()
    session_mgr = get_session_manager()

    async def resume_automation():
        from ..agent.loop import AgentLoop
        from ..llm import LLM

        try:
            session = session_mgr.get(job_id)
            if not session:
                return

            async def on_step_callback(step):
                job.step_count += 1
                await hub.broadcast(job_id, {
                    "type": "step",
                    "step": step.model_dump(mode="json"),
                })

            async def on_human_callback(req):
                job.status = JobStatus.AWAITING_HUMAN
                job.pending_request = req
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
            agent.resume_with_reply(reply)

            res = await agent.run_until_pause_or_done()
            if agent.receipt is not None:
                job.receipt = agent.receipt
                job.status = JobStatus.COMPLETED
                await hub.broadcast(job_id, {
                    "type": "receipt",
                    "receipt": agent.receipt.model_dump(mode="json"),
                    "status": job.status.value,
                })
        except Exception as e:
            logger.error("Resume failed for job %s: %s", job_id, e)
            job.status = JobStatus.FAILED
            job.error = str(e)

    _JOB_TASKS[job_id] = asyncio.create_task(resume_automation())
    return {"status": "ok", "message": "Reply received and execution resumed"}


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
