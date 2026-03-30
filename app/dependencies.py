from typing import AsyncGenerator
import asyncpg
import redis.asyncio as aioredis

from app.db.postgres import get_pool
from app.db.redis import get_client


async def get_db() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


async def get_redis() -> aioredis.Redis:
    return get_client()
