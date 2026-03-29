import time
import uuid
import structlog
import redis.asyncio as aioredis
from fastapi import HTTPException

from app.config import settings
from app.core.logging import E

logger = structlog.get_logger(__name__)


class SlidingWindowRateLimiter:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def is_allowed(
        self, key: str, limit: int, window_ms: int
    ) -> tuple[bool, dict]:
        now_ms = int(time.time() * 1000)
        window_start = now_ms - window_ms
        request_id = str(uuid.uuid4())

        try:
            pipe = self.redis.pipeline()
            pipe.zremrangebyscore(key, 0, window_start)
            pipe.zcard(key)
            pipe.zadd(key, {request_id: now_ms})
            pipe.pexpire(key, window_ms + 1000)
            results = await pipe.execute()

            current_count = results[1]  # count before adding this request

            if current_count >= limit:
                # Remove the entry we just added
                await self.redis.zrem(key, request_id)
                reset_ms = now_ms + window_ms
                return False, {
                    "limit": limit,
                    "remaining": 0,
                    "reset_ms": reset_ms,
                    "window_ms": window_ms,
                }

            remaining = limit - current_count - 1
            reset_ms = now_ms + window_ms
            return True, {
                "limit": limit,
                "remaining": remaining,
                "reset_ms": reset_ms,
                "window_ms": window_ms,
            }
        except Exception as e:
            logger.error(E.RATE_ERROR, error=str(e), key=key)
            # Fail open
            return True, {
                "limit": limit,
                "remaining": limit,
                "reset_ms": int(time.time() * 1000) + window_ms,
                "window_ms": window_ms,
            }


async def check_rate_limit(
    redis: aioredis.Redis, limit_type: str, identifier: str
) -> dict:
    limiter = SlidingWindowRateLimiter(redis)

    if limit_type == "vibe_query":
        key = f"rl:vibe:{identifier}"
        limit = settings.rate_limit_vibe_per_minute
        window_ms = 60 * 1000
    elif limit_type == "vibe_query_session":
        key = f"rl:vibe:session:{identifier}"
        limit = 50
        window_ms = 60 * 60 * 1000
    elif limit_type == "ws_connection":
        key = f"rl:ws:{identifier}"
        limit = settings.rate_limit_ws_per_hour
        window_ms = 60 * 60 * 1000
    elif limit_type == "session_create":
        key = f"rl:session:{identifier}"
        limit = settings.rate_limit_sessions_per_hour
        window_ms = 60 * 60 * 1000
    else:
        raise ValueError(f"Unknown limit_type: {limit_type}")

    allowed, meta = await limiter.is_allowed(key, limit, window_ms)

    if not allowed:
        retry_after = int((meta["reset_ms"] - int(time.time() * 1000)) / 1000)
        logger.warning(
            E.RATE_REJECTED,
            limit_type=limit_type,
            identifier=identifier,
            key=key,
            limit=limit,
            retry_after_s=max(retry_after, 1),
        )
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(max(retry_after, 1))},
        )

    logger.debug(
        E.RATE_ALLOWED,
        limit_type=limit_type,
        identifier=identifier,
        remaining=meta["remaining"],
        reset_ms=meta["reset_ms"],
    )
    return meta
