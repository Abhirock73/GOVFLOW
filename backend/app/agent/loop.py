"""Agent execution loop for GovFlow AI.

Drives the observe -> LLM -> guard check -> execute -> record step cycle.
Handles CAPTCHAs, OTPs, ambiguous choices, missing data, and final submit confirmations.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..browser.actions import Actions, ProfileValueResolver
from ..browser.observe import ElementInfo, Observation, observe
from ..browser.session import BrowserSession
from ..config import Settings, get_settings
from ..llm import LLM
from ..models import (
    ActionType,
    AgentStep,
    CitizenProfile,
    FormPlan,
    GapReport,
    HumanReply,
    HumanRequest,
    HumanRequestKind,
    JobStatus,
    Receipt,
    ServiceRequest,
    StepStatus,
    new_id,
)
from ..security import JobDataStore, get_store
from .guards import DomainGuard, GuardViolation, LoopGuard, PromptInjectionGuard, SubmitGuard
from .prompts import build_system_prompt, build_user_turn_prompt
from .tools import get_agent_tools

logger = logging.getLogger(__name__)


class AgentLoop:
    def __init__(
        self,
        session: BrowserSession,
        profile: CitizenProfile,
        service: ServiceRequest,
        *,
        plan: FormPlan | None = None,
        gap_report: GapReport | None = None,
        llm: LLM | None = None,
        store: JobDataStore | None = None,
        settings: Settings | None = None,
        job_id: str | None = None,
        on_step: Callable[[AgentStep], Awaitable[None]] | None = None,
        on_human_request: Callable[[HumanRequest], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.profile = profile
        self.service = service
        self.plan = plan
        self.gap_report = gap_report
        self.settings = settings or get_settings()
        self.job_id = job_id or (session.job_id if hasattr(session, "job_id") else new_id())
        self.store = store or get_store()
        self.llm = llm or LLM(self.settings)

        self.on_step = on_step
        self.on_human_request = on_human_request

        self.actions = Actions(
            session,
            resolver=ProfileValueResolver(profile, service.extra_params),
            profile=profile,
            store=self.store,
        )

        target_url = str(service.portal_url or (plan.portal_url if plan else "http://localhost"))
        self.domain_guard = DomainGuard(target_url, self.settings)
        self.loop_guard = LoopGuard(max_repeats=self.settings.agent_max_same_action_failures)
        self.injection_guard = PromptInjectionGuard()
        self.submit_guard = SubmitGuard()

        self.step_no: int = 0
        self.steps: list[AgentStep] = []
        self.status: JobStatus = JobStatus.RUNNING
        self.pending_human_request: HumanRequest | None = None
        self.pending_human_reply: HumanReply | None = None
        self.receipt: Receipt | None = None
        self.last_feedback: str | None = None
        self.consecutive_failures: int = 0
        self.captcha_solved_urls: set[str] = set()

    async def _emit_step(self, step: AgentStep) -> None:
        if self.on_step:
            res = self.on_step(step)
            if asyncio.iscoroutine(res):
                await res

    async def _emit_human_request(self, req: HumanRequest) -> None:
        if self.on_human_request:
            res = self.on_human_request(req)
            if asyncio.iscoroutine(res):
                await res

    def resume_with_reply(self, reply: HumanReply) -> None:
        """Deliver user reply to resume paused loop."""
        self.pending_human_reply = reply
        if reply.captcha_solved:
            current_url = self.session.page.url if self.session and self.session.page else ""
            if current_url:
                self.captcha_solved_urls.add(current_url)
        if self.pending_human_request and self.pending_human_request.kind == HumanRequestKind.CONFIRM_SUBMIT:
            if reply.approved or (reply.text and reply.text.lower() in ("yes", "y", "confirm", "proceed")):
                self.submit_guard.confirmed = True
        self.pending_human_request = None
        self.status = JobStatus.RUNNING

    async def step(self) -> StepStatus:
        """Run one single step in the agent cycle."""
        if self.pending_human_request is not None and self.pending_human_reply is None:
            return StepStatus.WAITING_HUMAN

        if self.step_no >= self.settings.agent_max_steps:
            self.status = JobStatus.FAILED
            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.WAIT,
                status=StepStatus.ERROR,
                error=f"Exceeded max steps limit ({self.settings.agent_max_steps})",
            )
            self.steps.append(step)
            await self._emit_step(step)
            return StepStatus.ERROR

        started = time.monotonic()
        await self.session.settle(timeout_ms=2500)
        obs = await observe(self.session, screenshot=True)

        # 1. Prompt Injection scanning
        self.injection_guard.check_observation(obs)

        # 2. Check for interactive obstacles that require immediate human intervention
        is_captcha_solved = (obs.url in self.captcha_solved_urls) or (self.pending_human_reply and self.pending_human_reply.captcha_solved)
        if obs.has_captcha and not is_captcha_solved:
            req = HumanRequest(
                kind=HumanRequestKind.CAPTCHA,
                question="A CAPTCHA is present on the page. Please solve it on the live browser preview and click 'Done, resume'.",
            )
            self.pending_human_request = req
            self.status = JobStatus.AWAITING_HUMAN

            thumb_b64 = base64.b64encode(obs.thumbnail).decode("ascii") if obs.thumbnail else None
            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.ASK_HUMAN,
                args={"kind": req.kind.value, "question": req.question},
                status=StepStatus.WAITING_HUMAN,
                url=obs.url,
                page_title=obs.title,
                summary="Detected CAPTCHA: requested citizen assistance",
                thumbnail_b64=thumb_b64,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            await self._emit_human_request(req)
            return StepStatus.WAITING_HUMAN

        # Prepare user turn prompt
        reply_text = None
        if self.pending_human_reply:
            reply_text = self.pending_human_reply.text
            if self.pending_human_reply.captcha_solved:
                reply_text = "Citizen solved the CAPTCHA."
            elif self.pending_human_reply.approved is not None:
                reply_text = "Approved by citizen." if self.pending_human_reply.approved else "Rejected by citizen."
            self.pending_human_reply = None  # consumed

        user_prompt = build_user_turn_prompt(
            step_no=self.step_no,
            observation=obs,
            service_request=self.service,
            plan=self.plan,
            gap_report=self.gap_report,
            available_profile_refs=self.profile.available_references(),
            last_action_feedback=self.last_feedback,
            pending_human_reply=reply_text,
            captcha_solved=is_captcha_solved,
        )

        sys_prompt = build_system_prompt()

        # Call LLM with tool declarations
        resp = await self.llm.create(
            purpose="agent_act",
            messages=[{"role": "user", "content": user_prompt}],
            system=sys_prompt,
            tools=get_agent_tools(),
            job_id=self.job_id,
        )

        tool_calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use" or (isinstance(b, dict) and b.get("type") == "tool_use")]
        rationale = resp.text.strip()

        thumb_b64 = base64.b64encode(obs.thumbnail).decode("ascii") if obs.thumbnail else None
        duration_ms = int((time.monotonic() - started) * 1000)

        if not tool_calls:
            # Model responded with rationale only; default wait to let animations settle
            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.WAIT,
                args={"seconds": 1.0},
                rationale=rationale,
                status=StepStatus.OK,
                url=obs.url,
                page_title=obs.title,
                summary="Observed page, waiting...",
                thumbnail_b64=thumb_b64,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cost_usd=resp.usage.cost_usd,
                duration_ms=duration_ms,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            await self.session.wait(1.0)
            return StepStatus.OK

        call = tool_calls[0]
        tool_name = getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else "")
        tool_input = getattr(call, "input", None) or (call.get("input") if isinstance(call, dict) else {})

        # Handle ask_human
        if tool_name == "ask_human":
            kind_str = tool_input.get("kind", "missing_data")
            try:
                kind = HumanRequestKind(kind_str)
            except ValueError:
                kind = HumanRequestKind.MISSING_DATA

            req = HumanRequest(
                kind=kind,
                question=tool_input.get("question", "Please provide input"),
                options=tool_input.get("options"),
                summary=tool_input.get("summary"),
            )
            self.pending_human_request = req
            self.status = (
                JobStatus.AWAITING_SUBMIT_CONFIRMATION
                if kind == HumanRequestKind.CONFIRM_SUBMIT
                else JobStatus.AWAITING_HUMAN
            )

            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.ASK_HUMAN,
                args={"kind": kind.value, "question": req.question},
                rationale=rationale,
                status=StepStatus.WAITING_HUMAN,
                url=obs.url,
                page_title=obs.title,
                summary=f"Asked citizen: {req.question[:60]}",
                thumbnail_b64=thumb_b64,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cost_usd=resp.usage.cost_usd,
                duration_ms=duration_ms,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            await self._emit_human_request(req)
            return StepStatus.WAITING_HUMAN

        # Handle finish
        if tool_name == "finish":
            ref_no = tool_input.get("reference_no", "SUBMITTED")
            summary = tool_input.get("summary", "Application successfully completed.")

            receipt_shot_path = None
            if obs.screenshot and self.store and hasattr(self.store, "save_screenshot"):
                try:
                    receipt_shot_path = self.store.save_screenshot(self.job_id, obs.screenshot)
                except Exception as e:
                    logger.warning("could not save receipt screenshot: %s", e)

            self.receipt = Receipt(
                job_id=self.job_id,
                reference_no=ref_no,
                portal=obs.url,
                service_name=self.service.service_name or self.service.raw_user_text,
                screenshot_path=receipt_shot_path,
                submitted_fields_masked=self.profile.masked_summary(),
                summary=summary,
            )
            self.status = JobStatus.COMPLETED

            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.FINISH,
                args={"reference_no": ref_no},
                rationale=rationale,
                status=StepStatus.OK,
                url=obs.url,
                page_title=obs.title,
                summary=f"Completed! Receipt: {ref_no}",
                thumbnail_b64=thumb_b64,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cost_usd=resp.usage.cost_usd,
                duration_ms=duration_ms,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            return StepStatus.OK

        # Handle save_progress
        if tool_name == "save_progress":
            note = tool_input.get("note", "Draft progress saved.")
            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.SAVE_PROGRESS,
                args={"note": note},
                rationale=rationale,
                status=StepStatus.OK,
                url=obs.url,
                page_title=obs.title,
                summary=f"Progress saved: {note}",
                thumbnail_b64=thumb_b64,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cost_usd=resp.usage.cost_usd,
                duration_ms=duration_ms,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            return StepStatus.OK

        # Execute browser action
        try:
            # Loop Guard Check
            self.loop_guard.check(obs, tool_name, tool_input)

            # Submit Guard Check
            if tool_name == "click":
                target_id = tool_input.get("element_id")
                target_el = next((e for e in obs.elements if e.id == target_id), None)
                self.submit_guard.check(target_el)

            # Run action
            action_res = await self.actions.run(tool_name, tool_input)
            logger.info("tool %s %s -> ok=%s url=%s msg=%s", tool_name, tool_input, action_res.ok, action_res.url_after, action_res.message)

            # Check navigation domain guard
            if action_res.url_after:
                self.domain_guard.check_navigation(action_res.url_after)

            if action_res.ok:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1

            self.last_feedback = action_res.for_model()

            try:
                act_type = ActionType(tool_name)
            except ValueError:
                act_type = ActionType.CLICK

            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=act_type,
                args=action_res.masked_args,
                rationale=rationale,
                status=StepStatus.OK if action_res.ok else StepStatus.ERROR,
                error=action_res.message if not action_res.ok else None,
                url=action_res.url_after or obs.url,
                page_title=obs.title,
                summary=action_res.message[:80],
                thumbnail_b64=thumb_b64,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cost_usd=resp.usage.cost_usd,
                duration_ms=duration_ms,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            return StepStatus.OK if action_res.ok else StepStatus.ERROR

        except GuardViolation as gv:
            logger.warning("Guard triggered: %s", gv)
            if gv.guard_name == "SubmitGuard":
                # Automatically convert submit violation to human confirmation request!
                req = HumanRequest(
                    kind=HumanRequestKind.CONFIRM_SUBMIT,
                    question=f"{gv.message} Would you like to proceed with final submission?",
                    summary=self.profile.masked_summary(),
                )
                self.pending_human_request = req
                self.status = JobStatus.AWAITING_SUBMIT_CONFIRMATION

                step = AgentStep(
                    job_id=self.job_id,
                    step_no=self.step_no,
                    action=ActionType.ASK_HUMAN,
                    args={"kind": req.kind.value, "question": req.question},
                    rationale=rationale,
                    status=StepStatus.WAITING_HUMAN,
                    url=obs.url,
                    page_title=obs.title,
                    summary="Reached final submission: awaiting citizen confirmation",
                    thumbnail_b64=thumb_b64,
                    duration_ms=duration_ms,
                )
                self.steps.append(step)
                self.step_no += 1
                await self._emit_step(step)
                await self._emit_human_request(req)
                return StepStatus.WAITING_HUMAN

            step = AgentStep(
                job_id=self.job_id,
                step_no=self.step_no,
                action=ActionType.CLICK,
                args=tool_input,
                rationale=rationale,
                status=StepStatus.BLOCKED,
                error=str(gv),
                url=obs.url,
                page_title=obs.title,
                summary=f"Blocked: {gv}",
                thumbnail_b64=thumb_b64,
                duration_ms=duration_ms,
            )
            self.steps.append(step)
            self.step_no += 1
            await self._emit_step(step)
            self.last_feedback = f"Action blocked by safety guard: {gv.message}"
            return StepStatus.BLOCKED
            self.last_feedback = f"Action blocked by safety guard: {gv.message}"
            return StepStatus.BLOCKED

    async def run_until_pause_or_done(self, max_consecutive_steps: int = 25) -> StepStatus:
        """Run loop until completion, error, or human input needed."""
        for _ in range(max_consecutive_steps):
            if self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                return StepStatus.OK if self.status == JobStatus.COMPLETED else StepStatus.ERROR
            if self.pending_human_request is not None and self.pending_human_reply is None:
                return StepStatus.WAITING_HUMAN

            res = await self.step()
            if res == StepStatus.WAITING_HUMAN:
                return StepStatus.WAITING_HUMAN
            if self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                return StepStatus.OK if self.status == JobStatus.COMPLETED else StepStatus.ERROR

        return StepStatus.OK
