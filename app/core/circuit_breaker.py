"""
Redis-backed circuit breaker for external API calls.

States:
  CLOSED    — normal operation; requests pass through
  OPEN      — too many failures; fallback is called immediately
  HALF_OPEN — testing recovery; next request is tried live

State is stored in Redis so it is shared across all app instances.
"""

import structlog
import redis.asyncio as aioredis
from typing import Any, Callable, Awaitable

from app.core.logging import E

logger = structlog.get_logger(__name__)

_CLOSED    = "CLOSED"
_OPEN      = "OPEN"
_HALF_OPEN = "HALF_OPEN"

_STATE_TTL_S    = 300   # 5 minutes
_FAILURES_TTL_S = 60    # 1 minute window for failure counting
_FAILURE_THRESHOLD = 3


class CircuitBreaker:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def call(
        self,
        service_name: str,
        coro_func: Callable[[], Awaitable[Any]],
        fallback_func: Callable[[], Awaitable[Any]],
    ) -> Any:
        state_key    = f"cb:{service_name}:state"
        failures_key = f"cb:{service_name}:failures"

        state = await self.redis.get(state_key) or _CLOSED

        if state == _OPEN:
            logger.warning(
                E.CB_OPEN,
                service=service_name,
                action="fallback",
            )
            return await fallback_func()

        # CLOSED or HALF_OPEN — attempt the real call
        try:
            result = await coro_func()
            # Success: reset failure counter and ensure state is CLOSED
            await self.redis.delete(failures_key)
            await self.redis.set(state_key, _CLOSED, ex=_STATE_TTL_S)
            if state == _HALF_OPEN:
                logger.info(E.CB_CLOSED, service=service_name, reason="half_open_success")
            return result
        except Exception as exc:
            failures = await self.redis.incr(failures_key)
            await self.redis.expire(failures_key, _FAILURES_TTL_S)

            if failures >= _FAILURE_THRESHOLD:
                await self.redis.set(state_key, _OPEN, ex=_STATE_TTL_S)
                logger.error(
                    E.CB_OPENED,
                    service=service_name,
                    failures=failures,
                    error=str(exc),
                )
            else:
                logger.warning(
                    E.CB_FAILURE,
                    service=service_name,
                    failures=failures,
                    threshold=_FAILURE_THRESHOLD,
                    error=str(exc),
                )
            raise
