"""LangGraph node implementations for GovFlow AI.

Every phase of the application pipeline is encapsulated in a graph node.
Human-in-the-loop interactions use `interrupt()` for clean pausing and SQLite resumption.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from langgraph.types import interrupt

from ..agent.loop import AgentLoop
from ..browser.session import SessionManager, get_session_manager
from ..config import Settings, get_settings
from ..discovery.resolver import Resolver, confirm_candidate
from ..llm import LLM
from ..models import (
    CitizenProfile,
    FormPlan,
    GapReport,
    HumanReply,
    HumanRequest,
    HumanRequestKind,
    InputMode,
    JobStatus,
    Receipt,
    ResolutionResult,
    ServiceRequest,
    new_id,
)
from ..planner.gap_analysis import analyze_gaps, apply_answers
from ..planner.requirements import FormPlanner
from ..security import JobDataStore, get_store
from .state import GovFlowState

logger = logging.getLogger(__name__)


def _ensure_profile(state: GovFlowState) -> CitizenProfile:
    p = state.get("profile")
    if p is None:
        p = CitizenProfile()
    elif isinstance(p, dict):
        p = CitizenProfile.model_validate(p)
    return p


def _ensure_service(state: GovFlowState) -> ServiceRequest:
    s = state.get("service")
    if s is None:
        s = ServiceRequest.from_user_input("")
    elif isinstance(s, dict):
        s = ServiceRequest.model_validate(s)
    return s


def intake_node(state: GovFlowState) -> dict[str, Any]:
    """Validate initial intake state."""
    job_id = state.get("job_id") or new_id()
    service = _ensure_service(state)
    profile = _ensure_profile(state)

    logger.info("Intake node for job %s: input_mode=%s", job_id, service.input_mode)
    return {
        "job_id": job_id,
        "service": service,
        "profile": profile,
        "status": JobStatus.RESOLVING,
        "steps": state.get("steps", []),
    }


async def resolve_service_node(state: GovFlowState) -> dict[str, Any]:
    """Resolve plain language scheme or URL into official portal candidates."""
    service = _ensure_service(state)
    job_id = state.get("job_id")
    settings = get_settings()

    resolver = Resolver(settings=settings)
    res: ResolutionResult = await resolver.resolve(service, job_id=job_id)

    updated_service = service
    if res.recommended is not None and not res.needs_user_choice:
        updated_service = confirm_candidate(res, res.recommended)

    return {
        "resolution": res,
        "service": updated_service,
        "status": JobStatus.AWAITING_PORTAL_CONFIRMATION if res.needs_user_choice else JobStatus.PLANNING,
    }


def confirm_portal_node(state: GovFlowState) -> dict[str, Any]:
    """Pause for user confirmation if the portal has multiple candidates or warnings."""
    res = state.get("resolution")
    service = _ensure_service(state)

    if res is not None and (res.needs_user_choice or service.portal_url is None):
        options = [c.url for c in res.candidates]
        req = HumanRequest(
            kind=HumanRequestKind.CONFIRM_PORTAL,
            question=res.message_for_user or "Please confirm the official portal to use for this service:",
            options=options,
        )
        # Interrupt graph execution until human responds
        user_input = interrupt(req.model_dump())
        reply = HumanReply.model_validate(user_input) if isinstance(user_input, dict) else HumanReply(request_id=req.request_id, text=str(user_input))

        chosen_url = reply.text
        chosen_candidate = next((c for c in res.candidates if c.url == chosen_url), None)
        if chosen_candidate is None and res.recommended is not None:
            chosen_candidate = res.recommended
        if chosen_candidate is not None:
            service = confirm_candidate(res, chosen_candidate)
        elif chosen_url and chosen_url.startswith("http"):
            service = ServiceRequest(raw_user_text=service.raw_user_text, input_mode=InputMode.LINK, portal_url=chosen_url)

    return {
        "service": service,
        "status": JobStatus.PLANNING,
    }


async def plan_form_node(state: GovFlowState) -> dict[str, Any]:
    """Read-only exploration of the portal to build FormPlan."""
    service = _ensure_service(state)
    portal_url = str(service.portal_url)
    job_id = state.get("job_id")
    settings = get_settings()

    planner = FormPlanner(settings=settings)
    plan = await planner.plan(portal_url, service_name=service.service_name, job_id=job_id)

    return {
        "plan": plan,
        "status": JobStatus.PLANNING,
    }


def gap_analysis_node(state: GovFlowState) -> dict[str, Any]:
    """Compare FormPlan against CitizenProfile and produce GapReport."""
    plan = state.get("plan")
    profile = _ensure_profile(state)
    service = _ensure_service(state)

    if plan is None:
        gap_report = GapReport(summary_en="No form plan available.")
    else:
        gap_report = analyze_gaps(plan, profile, service)

    return {
        "gap_report": gap_report,
        "status": JobStatus.AWAITING_MISSING_DATA if gap_report.questions_for_user else JobStatus.INTAKE,
    }


def collect_missing_node(state: GovFlowState) -> dict[str, Any]:
    """Batch-collect missing fields or documents from citizen if any are required."""
    gap_report: GapReport | None = state.get("gap_report")
    profile = _ensure_profile(state)
    service = _ensure_service(state)
    plan = state.get("plan")

    if gap_report and gap_report.questions_for_user:
        req = HumanRequest(
            kind=HumanRequestKind.COLLECT_MISSING,
            question=gap_report.summary_en or "We need a few more details to fill this form.",
            options=[q.prompt for q in gap_report.questions_for_user],
        )
        user_input = interrupt(req.model_dump())
        reply = HumanReply.model_validate(user_input) if isinstance(user_input, dict) else HumanReply(request_id=req.request_id, text=str(user_input))

        # Apply user answers
        if reply.text:
            answers = {}
            for q in gap_report.questions_for_user:
                answers[q.id] = reply.text
            ans_res = apply_answers(gap_report, answers, profile, service)
            profile = ans_res.profile
            service = ans_res.request

            # Re-evaluate gaps
            if plan is not None:
                gap_report = analyze_gaps(plan, profile, service)

    return {
        "profile": profile,
        "service": service,
        "gap_report": gap_report,
        "status": JobStatus.INTAKE,
    }


def confirm_profile_node(state: GovFlowState) -> dict[str, Any]:
    """Require explicit citizen review and approval of their profile before browser actions."""
    profile = _ensure_profile(state)

    req = HumanRequest(
        kind=HumanRequestKind.CONFIRM_PROFILE,
        question="Please review and confirm your details before GovFlow begins filling your application.",
        summary=profile.masked_summary(),
    )
    user_input = interrupt(req.model_dump())
    reply = HumanReply.model_validate(user_input) if isinstance(user_input, dict) else HumanReply(request_id=req.request_id, text=str(user_input))

    return {
        "status": JobStatus.RUNNING,
    }


async def launch_browser_node(state: GovFlowState) -> dict[str, Any]:
    """Launch browser session and navigate to portal."""
    job_id = state.get("job_id") or new_id()
    service = _ensure_service(state)
    portal_url = str(service.portal_url or "http://localhost")
    settings = get_settings()
    store = get_store()
    mgr = get_session_manager()

    session = await mgr.get_or_create(job_id, start_url=portal_url, restore=True)
    await session.settle(timeout_ms=3000)

    return {
        "status": JobStatus.RUNNING,
        "storage_state_path": str(store.session_dir(job_id) / "storage_state.json"),
    }


async def run_agent_segment_node(state: GovFlowState) -> dict[str, Any]:
    """Run steps of the AgentLoop until human intervention or form completion."""
    job_id = state.get("job_id") or new_id()
    profile = _ensure_profile(state)
    service = _ensure_service(state)
    plan = state.get("plan")
    gap_report = state.get("gap_report")
    settings = get_settings()
    store = get_store()
    mgr = get_session_manager()

    session = mgr.get(job_id)
    if session is None:
        session = await mgr.get_or_create(job_id, start_url=str(service.portal_url or "http://localhost"), restore=True)

    llm = LLM(settings)
    agent = AgentLoop(
        session=session,
        profile=profile,
        service=service,
        plan=plan,
        gap_report=gap_report,
        llm=llm,
        store=store,
        settings=settings,
        job_id=job_id,
    )

    # Deliver any pending human reply from previous resume
    human_reply = state.get("human_reply")
    if human_reply:
        agent.resume_with_reply(human_reply)

    # Run up to 20 steps per segment before checking
    status = await agent.run_until_pause_or_done(max_consecutive_steps=20)

    # Accumulate steps
    existing_steps = list(state.get("steps", []))
    existing_steps.extend(agent.steps)

    if agent.status == JobStatus.COMPLETED or agent.receipt is not None:
        return {
            "status": JobStatus.COMPLETED,
            "receipt": agent.receipt,
            "steps": existing_steps,
            "needs_human": False,
            "agent_done": True,
        }

    if agent.pending_human_request is not None:
        return {
            "status": agent.status,
            "pending_human_request": agent.pending_human_request,
            "human_kind": agent.pending_human_request.kind.value,
            "steps": existing_steps,
            "needs_human": True,
            "agent_done": False,
        }

    return {
        "status": agent.status,
        "steps": existing_steps,
        "needs_human": False,
        "agent_done": agent.status == JobStatus.COMPLETED,
    }


def await_human_node(state: GovFlowState) -> dict[str, Any]:
    """Pause graph using interrupt() and receive user reply."""
    req = state.get("pending_human_request")
    req_dict = req.model_dump() if hasattr(req, "model_dump") else req

    user_input = interrupt(req_dict)
    reply = HumanReply.model_validate(user_input) if isinstance(user_input, dict) else HumanReply(request_id=req_dict.get("request_id", ""), text=str(user_input))

    return {
        "human_reply": reply,
        "pending_human_request": None,
        "needs_human": False,
        "status": JobStatus.RUNNING,
    }


def confirm_submit_node(state: GovFlowState) -> dict[str, Any]:
    """Pause graph before final irreversible submission."""
    req = state.get("pending_human_request")
    req_dict = req.model_dump() if hasattr(req, "model_dump") else req

    user_input = interrupt(req_dict)
    reply = HumanReply.model_validate(user_input) if isinstance(user_input, dict) else HumanReply(request_id=req_dict.get("request_id", ""), text=str(user_input))

    return {
        "human_reply": reply,
        "pending_human_request": None,
        "needs_human": False,
        "status": JobStatus.RUNNING,
    }


def finalize_receipt_node(state: GovFlowState) -> dict[str, Any]:
    """Format final receipt and mark done."""
    receipt = state.get("receipt")
    if receipt is None:
        receipt = Receipt(
            job_id=state.get("job_id", ""),
            reference_no="COMPLETED",
            portal=str(state.get("service", {}).get("portal_url", "")),
            summary="Application successfully submitted.",
        )

    return {
        "receipt": receipt,
        "status": JobStatus.COMPLETED,
    }


def failed_node(state: GovFlowState) -> dict[str, Any]:
    return {"status": JobStatus.FAILED}


def cancelled_node(state: GovFlowState) -> dict[str, Any]:
    return {"status": JobStatus.CANCELLED}
