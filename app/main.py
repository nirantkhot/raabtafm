import os
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.logging import E, configure_logging
from app.core.middleware import RequestLoggingMiddleware
from app.db.postgres import create_pool, close_pool
from app.db.redis import create_client, close_client
from app.queue.router import router as queue_router
from app.routers.health import router as health_router
from app.session.router import router as session_router
from scripts.fixtures import router as fixtures_router

configure_logging()
logger = structlog.get_logger(__name__)

_startup_begin = time.perf_counter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info(E.APP_STARTUP, env=settings.app_env, log_level=settings.log_level)
    await create_pool(settings.database_url)
    await create_client(settings.redis_url)

    total_startup_ms = round((time.perf_counter() - _startup_begin) * 1000, 2)
    logger.info(
        E.APP_STARTUP_READY,
        message="Raabta.fm ready",
        env=settings.app_env,
        startup_ms=total_startup_ms,
        max_session_members=settings.max_session_members,
    )

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info(E.APP_SHUTDOWN, message="Raabta.fm shutting down")
    await close_pool()
    await close_client()


app = FastAPI(title="Raabta.fm API", lifespan=lifespan)

# Middleware order matters: RequestLoggingMiddleware must be added AFTER CORS
# so that CORS headers are already set when we log the response.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(session_router, prefix="/sessions")
app.include_router(queue_router, prefix="/sessions")
app.include_router(health_router)
app.include_router(fixtures_router)

# Mount static demo UI if present
if os.path.isdir("demo"):
    app.mount("/demo", StaticFiles(directory="demo", html=True), name="demo")
