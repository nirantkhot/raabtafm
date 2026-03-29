import redis.asyncio as aioredis
import structlog

from app.core.logging import E, timer

logger = structlog.get_logger(__name__)

_client: aioredis.Redis | None = None


async def create_client(url: str) -> aioredis.Redis:
    global _client
    _client = aioredis.from_url(url, decode_responses=True)
    # Verify connectivity and measure round-trip to Redis
    with timer() as t:
        await _client.ping()
    logger.info(E.DB_REDIS_CREATED, latency_ms=t.ms)
    return _client


def get_client() -> aioredis.Redis:
    if _client is None:
        raise RuntimeError("Redis client not initialized")
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
