"""LangGraph orchestration graph for GovFlow AI.

Connects intake, service resolution, read-only form planning, gap analysis,
profile confirmation, browser automation, human-in-the-loop interruptions,
and receipt generation into an end-to-end resumable workflow backed by SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..config import Settings, get_settings
from ..models import JobStatus
from .nodes import (
    await_human_node,
    cancelled_node,
    collect_missing_node,
    confirm_portal_node,
    confirm_profile_node,
    confirm_submit_node,
    failed_node,
    finalize_receipt_node,
    gap_analysis_node,
    intake_node,
    launch_browser_node,
    plan_form_node,
    resolve_service_node,
    run_agent_segment_node,
)
from .state import GovFlowState


def _route_after_resolve(state: GovFlowState) -> Literal["confirm_portal", "plan_form"]:
    if state.get("status") == JobStatus.AWAITING_PORTAL_CONFIRMATION:
        return "confirm_portal"
    return "plan_form"


def _route_after_gap(state: GovFlowState) -> Literal["collect_missing", "confirm_profile"]:
    gap = state.get("gap_report")
    if gap and gap.questions_for_user:
        return "collect_missing"
    return "confirm_profile"


def _route_agent_segment(
    state: GovFlowState,
) -> Literal["finalize_receipt", "confirm_submit", "await_human", "run_agent_segment", "failed"]:
    if state.get("status") == JobStatus.FAILED:
        return "failed"
    if state.get("agent_done") or state.get("receipt") is not None:
        return "finalize_receipt"
    if state.get("needs_human"):
        if state.get("human_kind") == "confirm_submit":
            return "confirm_submit"
        return "await_human"
    return "run_agent_segment"


def build_graph(checkpointer: Any | None = None) -> Any:
    workflow = StateGraph(GovFlowState)

    # Add all workflow nodes
    workflow.add_node("intake", intake_node)
    workflow.add_node("resolve_service", resolve_service_node)
    workflow.add_node("confirm_portal", confirm_portal_node)
    workflow.add_node("plan_form", plan_form_node)
    workflow.add_node("gap_analysis", gap_analysis_node)
    workflow.add_node("collect_missing", collect_missing_node)
    workflow.add_node("confirm_profile", confirm_profile_node)
    workflow.add_node("launch_browser", launch_browser_node)
    workflow.add_node("run_agent_segment", run_agent_segment_node)
    workflow.add_node("await_human", await_human_node)
    workflow.add_node("confirm_submit", confirm_submit_node)
    workflow.add_node("finalize_receipt", finalize_receipt_node)
    workflow.add_node("failed", failed_node)
    workflow.add_node("cancelled", cancelled_node)

    # Linear flow
    workflow.add_edge(START, "intake")
    workflow.add_edge("intake", "resolve_service")

    # Conditional branching
    workflow.add_conditional_edges(
        "resolve_service",
        _route_after_resolve,
        {"confirm_portal": "confirm_portal", "plan_form": "plan_form"},
    )
    workflow.add_edge("confirm_portal", "plan_form")
    workflow.add_edge("plan_form", "gap_analysis")

    workflow.add_conditional_edges(
        "gap_analysis",
        _route_after_gap,
        {"collect_missing": "collect_missing", "confirm_profile": "confirm_profile"},
    )
    workflow.add_edge("collect_missing", "confirm_profile")
    workflow.add_edge("confirm_profile", "launch_browser")
    workflow.add_edge("launch_browser", "run_agent_segment")

    workflow.add_conditional_edges(
        "run_agent_segment",
        _route_agent_segment,
        {
            "finalize_receipt": "finalize_receipt",
            "confirm_submit": "confirm_submit",
            "await_human": "await_human",
            "run_agent_segment": "run_agent_segment",
            "failed": "failed",
        },
    )

    # Resumption loops
    workflow.add_edge("await_human", "run_agent_segment")
    workflow.add_edge("confirm_submit", "run_agent_segment")

    # Terminal edges
    workflow.add_edge("finalize_receipt", END)
    workflow.add_edge("failed", END)
    workflow.add_edge("cancelled", END)

    return workflow.compile(checkpointer=checkpointer)


_DEFAULT_CHECKPOINTER: SqliteSaver | None = None


def get_sqlite_checkpointer(settings: Settings | None = None) -> SqliteSaver:
    global _DEFAULT_CHECKPOINTER
    if _DEFAULT_CHECKPOINTER is None:
        cfg = settings or get_settings()
        db_path = cfg.data_dir / "checkpoints.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _DEFAULT_CHECKPOINTER = SqliteSaver(conn)
    return _DEFAULT_CHECKPOINTER


def get_compiled_graph(settings: Settings | None = None) -> Any:
    checkpointer = get_sqlite_checkpointer(settings)
    return build_graph(checkpointer=checkpointer)

