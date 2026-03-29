"""
Hybrid vibe search: NL → feature ranges + vector similarity.
"""

import asyncio
import structlog
import asyncpg
import redis.asyncio as aioredis
from fastapi import HTTPException

from app.catalog.embedder import embed_texts
from app.core.circuit_breaker import CircuitBreaker
from app.core.logging import E, timer
from app.queue.vibe_extractor import extract_vibe_features, _fallback

logger = structlog.get_logger(__name__)


async def search_by_vibe(
    user_input: str,
    db: asyncpg.Connection,
    redis: aioredis.Redis,
    session_id: str,
    limit: int = 8,
) -> tuple[list[dict], dict]:
    """
    Returns (tracks, features) where tracks is a list of up to 5 dicts and
    features is the extracted vibe feature dict (for broadcasting keywords/confidence).
    """
    cb = CircuitBreaker(redis)

    # ── Step 1: extract features via Claude (circuit-broken) ─────────────────
    async def _vibe_fallback() -> dict:
        return _fallback(user_input)

    with timer() as t_vibe:
        features = await cb.call(
            "anthropic_vibe",
            lambda: extract_vibe_features(user_input),
            _vibe_fallback,
        )
    logger.debug(E.VIBE_EXTRACTED, vibe=user_input, latency_ms=t_vibe.ms)

    # ── Step 2: compose embedding query ──────────────────────────────────────
    keywords = features.get("mood_keywords", [user_input])
    query_text = f"Music that is {', '.join(keywords)}. {user_input}"

    # ── Step 3: embed query (circuit-broken, run sync fn in thread) ───────────
    async def _embed_fallback() -> None:
        raise HTTPException(status_code=503, detail="Search temporarily unavailable")

    with timer() as t_embed:
        vectors = await cb.call(
            "openai_embed",
            lambda: asyncio.to_thread(embed_texts, [query_text]),
            _embed_fallback,
        )
    query_embedding = vectors[0]
    logger.debug(E.VIBE_EMBED_DONE, vibe=user_input, latency_ms=t_embed.ms)

    # ── Step 4: hybrid search — vector + feature filters ─────────────────────
    energy_min  = features["energy"]["min"]
    energy_max  = features["energy"]["max"]
    valence_min = features["valence"]["min"]
    valence_max = features["valence"]["max"]
    tempo_min   = features["tempo"]["min"]
    tempo_max   = features["tempo"]["max"]

    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    rows = await db.fetch(
        """
        SELECT id, spotify_id, title, artist, features,
               1 - (embedding <=> $1::vector) AS similarity
        FROM songs
        WHERE
            spotify_id NOT IN (
                SELECT track_spotify_id FROM session_play_history
                WHERE session_id = $2
            )
            AND (features->>'energy')::float  BETWEEN $3 AND $4
            AND (features->>'valence')::float BETWEEN $5 AND $6
            AND (features->>'tempo')::float   BETWEEN $7 AND $8
        ORDER BY embedding <=> $1::vector
        LIMIT $9
        """,
        embedding_str, session_id,
        energy_min, energy_max,
        valence_min, valence_max,
        tempo_min, tempo_max,
        limit,
    )

    # ── Step 5: fallback to pure vector search if fewer than 3 results ───────
    fallback_used = False
    if len(rows) < 3:
        logger.info(E.VIBE_SEARCH_FALLBACK, vibe=user_input, hybrid_results=len(rows))
        fallback_used = True
        rows = await db.fetch(
            """
            SELECT id, spotify_id, title, artist, features,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM songs
            WHERE
                spotify_id NOT IN (
                    SELECT track_spotify_id FROM session_play_history
                    WHERE session_id = $2
                )
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            embedding_str, session_id, limit,
        )

    # ── Step 6: return top 5 ─────────────────────────────────────────────────
    tracks = [
        {
            "spotify_id":  row["spotify_id"],
            "title":       row["title"],
            "artist":      row["artist"],
            "similarity":  round(float(row["similarity"]), 3),
            "features":    row["features"],
        }
        for row in rows[:5]
    ]

    features["_fallback_used"] = fallback_used
    return tracks, features
