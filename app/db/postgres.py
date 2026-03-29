from pathlib import Path

import asyncpg
import structlog

from app.core.logging import E, timer

logger = structlog.get_logger(__name__)

_pool: asyncpg.Pool | None = None

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_POOL_MIN = 2
_POOL_MAX = 10


async def create_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    with timer() as t:
        _pool = await asyncpg.create_pool(
            dsn=dsn, min_size=_POOL_MIN, max_size=_POOL_MAX
        )
    logger.info(
        E.DB_PG_POOL_CREATED,
        latency_ms=t.ms,
        pool_min=_POOL_MIN,
        pool_max=_POOL_MAX,
    )
    await _run_migrations(_pool)
    return _pool


async def _run_migrations(pool: asyncpg.Pool) -> None:
    migration_file = MIGRATIONS_DIR / "001_init.sql"
    sql = migration_file.read_text()
    with timer() as t:
        async with pool.acquire() as conn:
            await conn.execute(sql)
    logger.info(
        E.DB_PG_MIGRATION,
        file="001_init.sql",
        latency_ms=t.ms,
    )


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Postgres pool not initialized")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
