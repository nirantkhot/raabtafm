import json
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
import structlog

from app.core.logging import E, timer
from app.config import settings
from app.session.models import PlaybackState, SessionState, SessionStatus

logger = structlog.get_logger(__name__)

# TTL in seconds
_TTL_DEV  = 7 * 24 * 60 * 60   # 7 days
_TTL_PROD = 24 * 60 * 60        # 24 hours


def _ttl() -> int:
    return _TTL_DEV if settings.app_env != "production" else _TTL_PROD


def _state_key(session_id: str) -> str:
    return f"session:{session_id}:state"


def _members_key(session_id: str) -> str:
    return f"session:{session_id}:members"


def _rtt_key(session_id: str) -> str:
    return f"session:{session_id}:rtt"


async def create_session(
    session_id: str,
    host_client_id: str,
    display_name: str,
    redis: aioredis.Redis,
) -> SessionState:
    now = datetime.now(timezone.utc).isoformat()
    playback = PlaybackState(
        status=SessionStatus.PAUSED,
        position_ms=0,
        track_spotify_id=None,
        updated_at=now,
        version=0,
    )

    ttl = _ttl()
    with timer() as t:
        pipe = redis.pipeline()
        pipe.set(_state_key(session_id), playback.model_dump_json(), ex=ttl)
        pipe.hset(_members_key(session_id), host_client_id, display_name)
        pipe.expire(_members_key(session_id), ttl)
        pipe.expire(_rtt_key(session_id), ttl)
        await pipe.execute()

    logger.info(
        E.SESSION_CREATED,
        session_id=session_id,
        host_client_id=host_client_id,
        ttl_seconds=ttl,
        latency_ms=t.ms,
    )

    return SessionState(
        session_id=session_id,
        code="",  # caller sets this
        playback=playback,
        members=[host_client_id],
        rtt_ms={},
    )


async def get_session_state(
    session_id: str, redis: aioredis.Redis
) -> Optional[SessionState]:
    with timer() as t:
        pipe = redis.pipeline()
        pipe.get(_state_key(session_id))
        pipe.hgetall(_members_key(session_id))
        pipe.hgetall(_rtt_key(session_id))
        results = await pipe.execute()

    state_json, members_raw, rtt_raw = results

    if state_json is None:
        return None

    playback = PlaybackState.model_validate_json(state_json)
    members  = list(members_raw.keys()) if members_raw else []
    rtt_ms   = {k: int(v) for k, v in rtt_raw.items()} if rtt_raw else {}

    logger.debug(
        "state.get_session",
        session_id=session_id,
        member_count=len(members),
        latency_ms=t.ms,
    )

    return SessionState(
        session_id=session_id,
        code="",  # caller enriches with code from DB
        playback=playback,
        members=members,
        rtt_ms=rtt_ms,
    )


async def update_playback(
    session_id: str,
    status: SessionStatus,
    position_ms: int,
    track_spotify_id: Optional[str],
    redis: aioredis.Redis,
) -> SessionState:
    key = _state_key(session_id)
    ttl = _ttl()

    for attempt in range(3):
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                with timer() as t_read:
                    state_json = await pipe.get(key)
                if state_json is None:
                    raise ValueError(f"Session {session_id} not found in Redis")

                current = PlaybackState.model_validate_json(state_json)
                now     = datetime.now(timezone.utc).isoformat()
                updated = PlaybackState(
                    status=status,
                    position_ms=position_ms,
                    track_spotify_id=track_spotify_id,
                    updated_at=now,
                    version=current.version + 1,
                )

                pipe.multi()
                pipe.set(key, updated.model_dump_json(), ex=ttl)
                with timer() as t_write:
                    await pipe.execute()

                logger.info(
                    E.PLAYBACK_UPDATED,
                    session_id=session_id,
                    status=status.value,
                    position_ms=position_ms,
                    track_spotify_id=track_spotify_id,
                    version=updated.version,
                    attempt=attempt,
                    read_ms=t_read.ms,
                    write_ms=t_write.ms,
                )

                # Fetch full state after successful update
                state = await get_session_state(session_id, redis)
                if state:
                    state.playback = updated
                return state

            except aioredis.WatchError:
                logger.warning(
                    E.PLAYBACK_CONFLICT,
                    session_id=session_id,
                    attempt=attempt,
                )
                continue

    logger.error(E.PLAYBACK_FAILED, session_id=session_id, retries=3)
    raise RuntimeError(f"Failed to update playback for {session_id} after 3 retries")


async def add_member(
    session_id: str,
    client_id: str,
    display_name: str,
    redis: aioredis.Redis,
) -> None:
    ttl = _ttl()
    with timer() as t:
        pipe = redis.pipeline()
        pipe.hset(_members_key(session_id), client_id, display_name)
        pipe.expire(_members_key(session_id), ttl)
        await pipe.execute()
    logger.info(
        E.STATE_MEMBER_ADDED,
        session_id=session_id,
        client_id=client_id,
        display_name=display_name,
        latency_ms=t.ms,
    )


async def remove_member(
    session_id: str, client_id: str, redis: aioredis.Redis
) -> None:
    with timer() as t:
        await redis.hdel(_members_key(session_id), client_id)
        await redis.hdel(_rtt_key(session_id), client_id)
    logger.info(
        E.STATE_MEMBER_REMOVED,
        session_id=session_id,
        client_id=client_id,
        latency_ms=t.ms,
    )


async def get_members(
    session_id: str, redis: aioredis.Redis
) -> dict[str, str]:
    result = await redis.hgetall(_members_key(session_id))
    return result or {}


async def record_rtt(
    session_id: str, client_id: str, rtt_ms: int, redis: aioredis.Redis
) -> None:
    ttl = _ttl()
    with timer() as t:
        pipe = redis.pipeline()
        pipe.hset(_rtt_key(session_id), client_id, rtt_ms)
        pipe.expire(_rtt_key(session_id), ttl)
        await pipe.execute()
    logger.info(
        E.STATE_RTT_RECORDED,
        session_id=session_id,
        client_id=client_id,
        rtt_ms=rtt_ms,
        redis_latency_ms=t.ms,
    )


async def get_rtt_stats(
    session_id: str, redis: aioredis.Redis
) -> dict:
    raw = await redis.hgetall(_rtt_key(session_id))
    if not raw:
        return {}

    rtt_values = {k: int(v) for k, v in raw.items()}
    values = list(rtt_values.values())

    return {
        **rtt_values,
        "avg": int(sum(values) / len(values)),
        "max": max(values),
        "min": min(values),
    }
