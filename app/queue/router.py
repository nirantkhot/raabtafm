"""
Queue management endpoints.

POST   /sessions/{session_id}/queue/vibe   — NL vibe search + enqueue
GET    /sessions/{session_id}/queue        — list ordered queue
DELETE /sessions/{session_id}/queue/{item_id} — remove item
"""

from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.client_identity import get_or_create_client_id
from app.core.logging import E, timer
from app.core.rate_limiter import check_rate_limit
from app.dependencies import get_db, get_redis
from app.queue.models import QueueItem, VibeRequest, VibeResponse
from app.queue.search import search_by_vibe
from app.session.websocket import manager

logger = structlog.get_logger(__name__)

router = APIRouter()


# ── POST /sessions/{session_id}/queue/vibe ────────────────────────────────────

@router.post("/{session_id}/queue/vibe", response_model=VibeResponse)
async def add_vibe_to_queue(
    session_id: str,
    body: VibeRequest,
    request: Request,
    response: Response,
    db: asyncpg.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    client_id = get_or_create_client_id(request, response)

    # Rate limits: 10/min per client, 50/hour per session
    await check_rate_limit(redis, "vibe_query", client_id)
    await check_rate_limit(redis, "vibe_query_session", session_id)

    # Validate session exists
    session_row = await db.fetchrow(
        "SELECT id FROM sessions WHERE id = $1 AND ended_at IS NULL",
        session_id,
    )
    if not session_row:
        raise HTTPException(status_code=404, detail="Session not found")

    # Search
    with timer() as t_search:
        tracks, features = await search_by_vibe(body.vibe, db, redis, session_id)

    if not tracks:
        raise HTTPException(status_code=404, detail="No tracks matched this vibe")

    fallback_used: bool = features.pop("_fallback_used", False)

    # Insert top 5 into session_queue
    inserted = []
    for track in tracks:
        position = await db.fetchval(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM session_queue WHERE session_id = $1",
            session_id,
        )
        await db.execute(
            """
            INSERT INTO session_queue
                (session_id, spotify_id, title, artist, position,
                 added_by, vibe_query, similarity_score)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            session_id,
            track["spotify_id"],
            track["title"],
            track["artist"],
            position,
            client_id,
            body.vibe,
            track["similarity"],
        )
        inserted.append({
            "spotify_id":       track["spotify_id"],
            "title":            track["title"],
            "artist":           track["artist"],
            "similarity_score": track["similarity"],
        })

    keywords: list[str] = features.get("mood_keywords", [body.vibe])
    confidence: float   = float(features.get("confidence", 0.1))

    # Broadcast queue_updated to all session members
    await manager.broadcast(
        session_id,
        {
            "event": "queue_updated",
            "session_id": session_id,
            "payload": {
                "added_tracks":        inserted,
                "added_by":            client_id,
                "added_by_ai":         True,
                "vibe_query":          body.vibe,
                "vibe_interpreted_as": keywords,
                "confidence":          confidence,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    logger.info(
        E.QUEUE_VIBE_ADDED,
        session_id=session_id,
        client_id=client_id,
        vibe=body.vibe,
        tracks_added=len(inserted),
        confidence=confidence,
        fallback_used=fallback_used,
        latency_ms=t_search.ms,
    )

    return VibeResponse(
        tracks=tracks,
        vibe_interpreted_as=keywords,
        confidence=confidence,
        fallback_used=fallback_used,
    )


# ── GET /sessions/{session_id}/queue ──────────────────────────────────────────

@router.get("/{session_id}/queue", response_model=list[QueueItem])
async def get_queue(
    session_id: str,
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await db.fetch(
        """
        SELECT id, session_id, spotify_id, title, artist, position,
               added_by, vibe_query, similarity_score, added_at
        FROM session_queue
        WHERE session_id = $1
        ORDER BY position ASC
        """,
        session_id,
    )
    return [QueueItem(**dict(row)) for row in rows]


# ── DELETE /sessions/{session_id}/queue/{item_id} ─────────────────────────────

@router.delete("/{session_id}/queue/{item_id}", status_code=204)
async def remove_queue_item(
    session_id: str,
    item_id: str,
    db: asyncpg.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    row = await db.fetchrow(
        "SELECT id FROM session_queue WHERE id = $1 AND session_id = $2",
        item_id, session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found in this session")

    await db.execute("DELETE FROM session_queue WHERE id = $1", item_id)

    await manager.broadcast(
        session_id,
        {
            "event": "queue_updated",
            "session_id": session_id,
            "payload": {"removed_item_id": item_id},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    logger.info(E.QUEUE_ITEM_REMOVED, session_id=session_id, item_id=item_id)
