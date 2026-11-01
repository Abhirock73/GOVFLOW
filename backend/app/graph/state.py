"""State definition for GovFlow LangGraph workflow."""

from __future__ import annotations

from typing import Any, TypedDict

from ..models import (
    AgentStep,
    CitizenProfile,
    FormPlan,
    GapReport,
    HumanReply,
    HumanRequest,
    JobStatus,
    Receipt,
    ResolutionResult,
    ServiceRequest,
)


class GovFlowState(TypedDict, total=False):
    """The complete persistent state of a citizen application workflow."""

    job_id: str
    status: JobStatus
    service: ServiceRequest
    resolution: ResolutionResult | None
    profile: CitizenProfile
    plan: FormPlan | None
    gap_report: GapReport | None
    steps: list[AgentStep]
    pending_human_request: HumanRequest | None
    human_reply: HumanReply | None
    receipt: Receipt | None
    storage_state_path: str | None
    error: str | None
    current_step_no: int
    loop_active: bool

