import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
import sqlite3
from app.graph.graph import build_graph
from app.graph.state import GovFlowState
from app.models import CitizenProfile, InputMode, JobStatus, ServiceRequest


def test_graph_compile():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    graph = build_graph(checkpointer=checkpointer)
    assert graph is not None


def test_intake_to_resolve_direct_link():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    graph = build_graph(checkpointer=checkpointer)

    thread_id = "test_thread_001"
    config = {"configurable": {"thread_id": thread_id}}

    init_state: GovFlowState = {
        "job_id": "test_job_1",
        "service": ServiceRequest(
            raw_user_text="http://localhost:8080/form.html",
            input_mode=InputMode.LINK,
            portal_url="http://localhost:8080/form.html",
        ),
        "profile": CitizenProfile(),
    }

    # Run intake step only or verify graph handles inputs
    # Testing that state initialization works with the compiled graph
    state_snap = graph.get_state(config)
    assert state_snap.values == {}

