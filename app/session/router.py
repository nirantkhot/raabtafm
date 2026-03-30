import asyncio
import json
import random
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

import app.session.state as state
from app.config import settings
from app.core.client_identity import get_or_create_client_id
from app.core.logging import E, timer
from app.core.rate_limiter import check_rate_limit
from app.dependencies import get_db, get_redis
from app.session.models import (
    CreateSessionRequest,
    JoinSessionRequest,
    PlaybackCommand,
    SessionStatus,
)
from app.session.websocket import manager

logger = structlog.get_logger(__name__)

router = APIRouter()

_WORDS = [
    "JAZZ", "BLUE", "SOUL", "ROCK", "BASS", "BEAT", "ECHO",
    "FUNK", "GOLD", "HAZE", "INDO", "JIVE", "KEYS", "LOOP",
    "MUSE", "NOVA", "OPUS", "PURE", "RIFF", "SYNC", "TONE",
    "VIBE", "WAVE", "XTRA", "YARN", "ZONE",
]


def _generate_code() -> str:
    word = random.choice(_WORDS)
    digits = random.randint(1000, 9999)
    return f"{word}-{digits}"


@router.post("")
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    response: Response,
    db: asyncpg.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    client_id = get_or_create_client_id(request, response)
    await check_rate_limit(redis, "session_create", client_id)

    session_id = str(uuid.uuid4())
    code = _generate_code()

    # Ensure unique code — log any collision retries
    for attempt in range(5):
        existing = await db.fetchrow("SELECT id FROM sessions WHERE code = $1", code)
        if not existing:
            break
        logger.warning(E.SESSION_CODE_RETRY, code=code, attempt=attempt, session_id=session_id)
        code = _generate_code()

    with timer() as t_pg:
        await db.execute(
            "INSERT INTO sessions (id, code, host_client_id) VALUES ($1, $2, $3)",
            session_id, code, client_id,
        )
        await db.execute(
            "INSERT INTO session_members (session_id, client_id, display_name) VALUES ($1, $2, $3)",
            session_id, client_id, body.display_name,
        )

    session_state = await state.create_session(session_id, client_id, body.display_name, redis)
    session_state.code = code

    logger.info(
        E.SESSION_CREATED,
        session_id=session_id,
        code=code,
        client_id=client_id,
        display_name=body.display_name,
        pg_latency_ms=t_pg.ms,
    )

    return {
        "session_id": session_id,
        "code": code,
        "state": session_state.model_dump(),
        "client_id": client_id,
    }


@router.post("/{session_id}/join")
async def join_session(
    session_id: str,
    body: JoinSessionRequest,
    request: Request,
    response: Response,
    db: asyncpg.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    client_id = get_or_create_client_id(request, response)

    row = await db.fetchrow(
        "SELECT id, code FROM sessions WHERE id = $1 AND ended_at IS NULL",
        session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    members = await state.get_members(session_id, redis)
    if len(members) >= settings.max_session_members:
        logger.warning(
            E.SESSION_FULL,
            session_id=session_id,
            client_id=client_id,
            member_count=len(members),
            max_members=settings.max_session_members,
        )
        raise HTTPException(status_code=403, detail="Session is full")

    with timer() as t_pg:
        await db.execute(
            """
            INSERT INTO session_members (session_id, client_id, display_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (session_id, client_id) DO UPDATE SET display_name = $3
            """,
            session_id, client_id, body.display_name,
        )
    await state.add_member(session_id, client_id, body.display_name, redis)

    session_state = await state.get_session_state(session_id, redis)
    if session_state:
        session_state.code = row["code"]

    updated_members = await state.get_members(session_id, redis)

    logger.info(
        E.SESSION_JOINED,
        session_id=session_id,
        client_id=client_id,
        display_name=body.display_name,
        member_count=len(updated_members),
        pg_latency_ms=t_pg.ms,
    )

    await manager.broadcast(
        session_id,
        {
            "event": "member_joined",
            "session_id": session_id,
            "payload": {"client_id": client_id, "display_name": body.display_name},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    return {
        "session_id": session_id,
        "code": row["code"],
        "state": session_state.model_dump() if session_state else None,
        "members": list(updated_members.keys()),
    }


@router.post("/{session_id}/playback")
async def update_playback(
    session_id: str,
    body: PlaybackCommand,
    request: Request,
    response: Response,
    db: asyncpg.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    get_or_create_client_id(request, response)

    row = await db.fetchrow(
        "SELECT id FROM sessions WHERE id = $1 AND ended_at IS NULL", session_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    action_to_status = {
        "play": SessionStatus.PLAYING,
        "pause": SessionStatus.PAUSED,
        "seek": SessionStatus.PLAYING,
    }
    new_status = action_to_status.get(body.action, SessionStatus.PAUSED)

    updated = await state.update_playback(
        session_id,
        new_status,
        body.position_ms,
        body.track_spotify_id,
        redis,
    )

    await manager.broadcast(
        session_id,
        {
            "event": "playback_updated",
            "session_id": session_id,
            "payload": updated.model_dump(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    return {"state": updated.model_dump()}


@router.get("/by-code/{code}")
async def get_session_by_code(
    code: str,
    db: asyncpg.Connection = Depends(get_db),
):
    """Resolve a human-readable session code (e.g. JAZZ-4521) → session UUID."""
    row = await db.fetchrow(
        "SELECT id FROM sessions WHERE code = $1 AND ended_at IS NULL",
        code.upper(),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": str(row["id"])}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    response: Response,
    db: asyncpg.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    get_or_create_client_id(request, response)

    row = await db.fetchrow(
        "SELECT id, code FROM sessions WHERE id = $1", session_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    session_state = await state.get_session_state(session_id, redis)
    if not session_state:
        raise HTTPException(status_code=404, detail="Session state not found")

    session_state.code = row["code"]
    rtt_stats = await state.get_rtt_stats(session_id, redis)

    return {
        "session_id": session_id,
        "code": row["code"],
        "state": session_state.model_dump(),
        # members also lives inside state.members — exposed top-level for convenience
        "members": session_state.members,
        "rtt_stats": rtt_stats,
    }


@router.websocket("/{session_id}/ws")
async def websocket_endpoint(
    session_id: str,
    websocket: WebSocket,
    display_name: str = "Anonymous",
    client_id: Optional[str] = None,
):
    from app.db.redis import get_client as get_redis_client
    from app.db.postgres import get_pool

    redis = get_redis_client()
    db_pool = get_pool()

    # Get client_id from query param, header, cookie, or generate one
    client_id = (
        client_id
        or websocket.headers.get("X-Client-ID")
        or websocket.cookies.get("groove_client_id")
        or str(uuid.uuid4())
    )

    async with db_pool.acquire() as db:
        connected = await manager.connect(
            session_id, client_id, display_name, websocket, redis, db
        )

    if not connected:
        return

    # Send current state immediately
    session_state = await state.get_session_state(session_id, redis)
    if session_state:
        row = None
        async with db_pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT code FROM sessions WHERE id = $1", session_id
            )
        if row:
            session_state.code = row["code"]
        await websocket.send_json(
            {
                "event": "session_state",
                "session_id": session_id,
                "payload": session_state.model_dump(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    # Start heartbeat as background task
    heartbeat_task = asyncio.create_task(
        manager.start_heartbeat(session_id, client_id, websocket, redis)
    )

    try:
        while True:
            text = await websocket.receive_text()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                logger.debug(
                    E.WS_INVALID_JSON,
                    session_id=session_id,
                    client_id=client_id,
                    raw=text[:200],
                )
                continue

            event = msg.get("event", "")
            logger.debug(
                E.WS_MSG_RECEIVED,
                session_id=session_id,
                client_id=client_id,
                event_type=event,
            )

            if event == "playback":
                payload      = msg.get("payload", {})
                action       = payload.get("action", "pause")
                position_ms  = payload.get("position_ms", 0)
                track_spotify_id = payload.get("track_spotify_id")

                action_to_status = {
                    "play": SessionStatus.PLAYING,
                    "pause": SessionStatus.PAUSED,
                    "seek": SessionStatus.PLAYING,
                }
                new_status = action_to_status.get(action, SessionStatus.PAUSED)

                try:
                    with timer() as t:
                        updated = await state.update_playback(
                            session_id, new_status, position_ms, track_spotify_id, redis
                        )
                    await manager.broadcast(
                        session_id,
                        {
                            "event": "playback_updated",
                            "session_id": session_id,
                            "payload": updated.model_dump(),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception as e:
                    logger.error(
                        "ws.playback_error",
                        error=str(e),
                        session_id=session_id,
                        client_id=client_id,
                        action=action,
                    )

            elif event == "pong":
                await manager.handle_pong(session_id, client_id, redis)

            elif event == "ping":
                await websocket.send_json(
                    {
                        "event": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

            else:
                logger.debug(
                    E.WS_UNKNOWN_EVENT,
                    session_id=session_id,
                    client_id=client_id,
                    event_type=event,
                )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("ws_error", error=str(e), session_id=session_id, client_id=client_id)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        await manager.disconnect(session_id, client_id, redis)
        await manager.broadcast(
            session_id,
            {
                "event": "member_left",
                "session_id": session_id,
                "payload": {"client_id": client_id},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
