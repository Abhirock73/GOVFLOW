"""WebSocket streaming and multiplexing for GovFlow AI.

Multiplexes live screencast frames, step timeline updates, human requests,
chat messages, and takeover mouse/keyboard interaction on a single WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..browser.screencast import Screencaster
from ..browser.session import get_session_manager
from ..browser.takeover import InputEvent, TakeoverController
from ..config import get_settings
from ..models import AgentStep, CitizenProfile, FieldSource, HumanReply, HumanRequest, JobStatus, ProfileField
from ..security import get_store

logger = logging.getLogger(__name__)

router = APIRouter()


class WebSocketHub:
    def __init__(self) -> None:
        self.active_connections: dict[str, set[WebSocket]] = {}
        self.screencasters: dict[str, Screencaster] = {}
        self.takeovers: dict[str, TakeoverController] = {}
        self._lock = asyncio.Lock()

    async def connect(self, job_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            if job_id not in self.active_connections:
                self.active_connections[job_id] = set()
            self.active_connections[job_id].add(ws)

    async def disconnect(self, job_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if job_id in self.active_connections:
                self.active_connections[job_id].discard(ws)
                if not self.active_connections[job_id]:
                    del self.active_connections[job_id]

    async def broadcast(self, job_id: str, message: dict[str, Any]) -> None:
        conns = list(self.active_connections.get(job_id, []))
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                pass


hub = WebSocketHub()


def get_hub() -> WebSocketHub:
    return hub


def _is_valid_portal_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    if u in ("http://localhost", "http://localhost/", "https://localhost", "https://localhost/"):
        return False
    return u.startswith("http://") or u.startswith("https://")


@router.websocket("/jobs/{job_id}/stream")
async def job_stream_endpoint(websocket: WebSocket, job_id: str) -> None:
    settings = get_settings()
    session_mgr = get_session_manager()

    await hub.connect(job_id, websocket)

    session = session_mgr.get(job_id)
    if session is None or session.closed:
        from .routes import _get_or_load_job
        job = _get_or_load_job(job_id)
        portal_url = str(job.service.portal_url) if (job and job.service and job.service.portal_url) else None
        if _is_valid_portal_url(portal_url):
            try:
                session = await session_mgr.get_or_create(job_id, start_url=portal_url, restore=True)
            except Exception as e:
                logger.warning("Could not auto-start browser on ws connect: %s", e)

    screencaster: Screencaster | None = None
    takeover: TakeoverController | None = None
    frame_queue: asyncio.Queue | None = None

    if session is not None:
        screencaster = Screencaster(session, settings)
        await screencaster.start()
        frame_queue = screencaster.subscribe()
        takeover = TakeoverController(session, settings)

    # Frame streaming task that dynamically attaches when a session is ready
    async def stream_frames():
        nonlocal screencaster, takeover, frame_queue
        last_attempt = 0.0
        try:
            while True:
                if frame_queue is None or screencaster is None:
                    curr_session = session_mgr.get(job_id)
                    if curr_session is not None and not curr_session.closed:
                        screencaster = Screencaster(curr_session, settings)
                        await screencaster.start()
                        frame_queue = screencaster.subscribe()
                        takeover = TakeoverController(curr_session, settings)
                    else:
                        now = time.monotonic()
                        if now - last_attempt > 4.0:
                            last_attempt = now
                            from .routes import _get_or_load_job
                            job = _get_or_load_job(job_id)
                            portal_url = str(job.service.portal_url) if (job and job.service and job.service.portal_url) else None
                            if _is_valid_portal_url(portal_url):
                                try:
                                    curr_session = await session_mgr.get_or_create(
                                        job_id,
                                        start_url=portal_url,
                                        restore=True,
                                    )
                                    screencaster = Screencaster(curr_session, settings)
                                    await screencaster.start()
                                    frame_queue = screencaster.subscribe()
                                    takeover = TakeoverController(curr_session, settings)
                                except Exception as e:
                                    logger.warning("Could not auto-start browser in stream loop: %s", e)
                        await asyncio.sleep(0.5)
                        continue

                frame = await frame_queue.get()
                await websocket.send_json({
                    "type": "screencast_frame",
                    "data_b64": frame.data_b64,
                    "width": frame.device_width,
                    "height": frame.device_height,
                    "timestamp": frame.timestamp,
                })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Screencast stream ended for job %s: %s", job_id, e)
        finally:
            if screencaster and frame_queue:
                screencaster.unsubscribe(frame_queue)

    frame_task = asyncio.create_task(stream_frames())

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "takeover_begin":
                if takeover is None:
                    curr_session = session_mgr.get(job_id)
                    if curr_session is not None:
                        takeover = TakeoverController(curr_session, settings)
                if takeover:
                    reason = data.get("reason", "Citizen user takeover")
                    await takeover.begin(reason)
                    await hub.broadcast(job_id, {"type": "takeover_status", "active": True})

            elif msg_type == "takeover_end":
                if takeover:
                    duration = await takeover.end()
                    await hub.broadcast(job_id, {"type": "takeover_status", "active": False, "duration": duration})

            elif msg_type == "refresh_stream":
                curr_session = session_mgr.get(job_id)
                if not curr_session or curr_session.closed:
                    from .routes import _get_or_load_job
                    job = _get_or_load_job(job_id)
                    portal_url = str(job.service.portal_url) if (job and job.service and job.service.portal_url) else None
                    if _is_valid_portal_url(portal_url):
                        try:
                            curr_session = await session_mgr.get_or_create(
                                job_id,
                                start_url=portal_url,
                                restore=True,
                            )
                        except Exception as e:
                            logger.warning("Failed to start session on refresh: %s", e)
                if curr_session and not curr_session.closed:
                    try:
                        shot = await curr_session.page.screenshot(type="jpeg", quality=settings.screencast_quality)
                        await websocket.send_json({
                            "type": "screencast_frame",
                            "data_b64": base64.b64encode(shot).decode("ascii"),
                            "width": settings.browser_viewport_width,
                            "height": settings.browser_viewport_height,
                            "timestamp": time.time(),
                        })
                    except Exception as e:
                        logger.debug("Failed manual stream refresh: %s", e)

            elif msg_type == "takeover_input":
                if takeover is None:
                    curr_session = session_mgr.get(job_id)
                    if curr_session is not None:
                        takeover = TakeoverController(curr_session, settings)
                if takeover and takeover.active:
                    ev_dict = data.get("event", {})
                    try:
                        ev = InputEvent.model_validate(ev_dict)
                        await takeover.dispatch(ev)
                    except Exception as e:
                        logger.debug("Failed to dispatch takeover event: %s", e)

            elif msg_type == "chat_message":
                text = str(data.get("text", "")).strip()
                await hub.broadcast(job_id, {
                    "type": "chat",
                    "sender": "user",
                    "text": text,
                })
                from .routes import _get_or_load_job, handle_human_reply
                job = _get_or_load_job(job_id)

                # Proactively parse email / phone / name from chat input
                store = get_store()
                profile = store.load_profile(job_id) or CitizenProfile()
                email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
                if email_match:
                    profile.email = ProfileField(value=email_match.group(0), confidence=1.0, source=FieldSource.USER_EDIT)
                    store.save_profile(job_id, profile)
                    await hub.broadcast(job_id, {
                        "type": "profile_updated",
                        "profile": profile.model_dump(),
                    })
                    await hub.broadcast(job_id, {
                        "type": "chat",
                        "sender": "agent",
                        "text": f"Saved your email: {email_match.group(0)} to profile.",
                    })

                req_id = job.pending_request.request_id if (job and job.pending_request) else "chat"
                reply_req = HumanReply(request_id=req_id, text=text)
                asyncio.create_task(handle_human_reply(job_id, reply_req))

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for job %s", job_id)
    except Exception as e:
        logger.error("WebSocket error for job %s: %s", job_id, e)
    finally:
        frame_task.cancel()
        if screencaster:
            await screencaster.stop()
        await hub.disconnect(job_id, websocket)
