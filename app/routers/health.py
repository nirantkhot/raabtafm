from datetime import datetime, timezone

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.logging import E, timer
from app.db.postgres import get_pool
from app.db.redis import get_client

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    redis_status    = "ok"
    postgres_status = "ok"
    redis_ms        = None
    postgres_ms     = None

    try:
        redis = get_client()
        with timer() as t:
            await redis.set("health:check", "ok", ex=5)
        redis_ms = t.ms
    except Exception as e:
        logger.error(E.HEALTH_ERROR_REDIS, error=str(e))
        redis_status = "error"

    try:
        pool = get_pool()
        with timer() as t:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
        postgres_ms = t.ms
    except Exception as e:
        logger.error(E.HEALTH_ERROR_PG, error=str(e))
        postgres_status = "error"

    overall = "ok" if redis_status == "ok" and postgres_status == "ok" else "error"

    log_fn = logger.debug if overall == "ok" else logger.error
    log_fn(
        E.HEALTH_CHECK,
        status=overall,
        redis=redis_status,
        postgres=postgres_status,
        redis_latency_ms=redis_ms,
        postgres_latency_ms=postgres_ms,
    )

    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={
            "status": overall,
            "redis": redis_status,
            "postgres": postgres_status,
            "redis_latency_ms": redis_ms,
            "postgres_latency_ms": postgres_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
