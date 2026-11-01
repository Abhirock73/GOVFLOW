"""GovFlow AI FastAPI Application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router as api_router
from .api.ws import router as ws_router
from .browser.session import get_session_manager
from .config import get_settings
from .security import get_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    settings.ensure_dirs()
    logger.info("GovFlow AI backend starting in %s mode (data_dir=%s)", settings.env, settings.data_dir)
    yield
    # Shutdown sessions cleanly
    session_mgr = get_session_manager()
    await session_mgr.shutdown()
    logger.info("GovFlow AI backend shut down cleanly.")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="GovFlow AI",
        description="Autonomous public-service and government form-filling web agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        *settings.cors_origins,
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(ws_router)

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "app": "GovFlow AI"}

    return app


app = create_app()
