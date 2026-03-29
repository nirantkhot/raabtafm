# Raabta.fm

**Feel the room.**

Raabta (رابطہ) means "connection" in Urdu and Hindi. It's a real-time synchronized music session backend built for people who are physically apart but want to listen together — same track, same moment, same energy. The interesting technical problem is maintaining sub-100ms playback sync across clients with asymmetric network conditions while letting participants collaboratively shape the queue through natural language instead of track search.

## Demo

**Live:** https://your-railway-url.up.railway.app/demo/

Open the demo in a browser. Alice clicks "Create Session" — a session code appears (e.g. `JAZZ-4521`). Bob types that code into the Join input and clicks Join. Both panels are now in the same session: play/pause on either side syncs instantly, and either user can type a vibe into "Add to queue" to get AI-matched tracks.

## Architecture

The system has three distinct layers that compose to produce the synchronized listening experience.

**Session sync engine.** Sessions live entirely in Redis — a hash for member state, a JSON blob for playback state (status, position, track ID, optimistic-lock version), and a hash for per-client RTT measurements. The server never writes playback state to Postgres during a session; Postgres holds only the audit record (sessions, session_members, session_play_history) and the song catalog. WebSocket connections are managed by a singleton `ConnectionManager` that broadcasts playback and membership events to every socket in a session. Optimistic locking on the playback state uses Redis WATCH/pipeline with three retries, so concurrent play/pause commands from two clients don't corrupt state — the second write sees a version conflict and retries against the winner's state.

**Sync optimizer.** Every 30 seconds the server sends a ping to each WebSocket client and records the timestamp. When the client responds with a pong, RTT is calculated from the round trip and stored in `session:{id}:rtt`. The RTT values are broadcast as `rtt_update` events so clients know their relative latency. The current implementation measures and exposes RTT but does not yet apply per-client playback offset correction — that's the next step toward true clock sync (see Limitations).

**NL queue control.** When a user submits a vibe string (e.g. "late night drive"), it goes through a two-stage retrieval pipeline. First, Claude Haiku extracts structured audio feature ranges (energy, valence, tempo, danceability, acousticness) and mood keywords from the free-text description — a structured extraction task where Haiku's speed and cost profile is the right tradeoff. Second, the mood keywords are composed into an embedding query ("Music that is nocturnal, melancholic, atmospheric. late night drive") and sent to OpenAI text-embedding-3-small to get a 1536-dim vector. pgvector runs cosine similarity search against the song catalog filtered by the feature ranges from step one. If the hybrid search returns fewer than three results (usually because the catalog is sparse in a feature corner), the system falls back to pure vector search without feature filters. The top five matches are inserted into the session queue and broadcast as a `queue_updated` event. The entire AI path is wrapped in a Redis-backed circuit breaker that opens after three failures in a 60-second window; in OPEN state vibe queries fail fast with a 503 rather than stacking timeout load on the OpenAI/Anthropic APIs.

## Running locally

**Prerequisites:** Docker Desktop with at least 4 GB memory allocated; API keys for OpenAI, Anthropic, and Spotify.

```bash
git clone <repo-url>
cd raabtafm
cp .env.example .env
# Fill in OPENAI_API_KEY, ANTHROPIC_API_KEY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
docker compose up --build
# One-time: seed the song catalog from Spotify playlists
docker compose exec app python scripts/seed_catalog.py
# Open the demo
open http://localhost:8000/demo/index.html
```

The app starts with hot-reload enabled (`--reload`). Postgres migrations run automatically on startup via `001_init.sql`. The pgvector extension is installed by the `pgvector/pgvector:pg15` image — no manual setup needed.

## Key engineering decisions

**Redis for session state, not Postgres.** Session playback state changes on every play, pause, and seek — potentially several times per second across multiple clients. Writing each change to Postgres would generate write amplification with no benefit, since session state is ephemeral: a session that ends doesn't need its millisecond-by-millisecond playback history. Redis gives sub-millisecond reads and writes for the hot path, and a 7-day TTL (configurable to 24 hours in production) handles cleanup automatically. The tradeoff is that if Redis goes down mid-session, session state is lost — but sessions are cheap to recreate, and the audit record in Postgres (what played, who was there) survives.

**Optimistic locking on playback updates.** Multiple clients can send playback commands concurrently — Alice hits pause a half-second after Bob hits play. Without coordination, the last write wins and one client's intent is silently dropped. Rather than use a distributed lock (which adds latency and a failure mode), the playback state carries a `version` integer. Updates use Redis WATCH on the state key: if the key changes between WATCH and EXEC, the pipeline raises WatchError and the update retries against the new state. Three retries is sufficient for a session with up to 10 members; beyond that, a queue-based serialization approach would be more appropriate.

**Hybrid retrieval (LLM feature extraction + vector search) vs pure vector search.** A naive approach embeds the vibe query and retrieves nearest neighbors by cosine similarity. This works for vibes that map cleanly to song descriptions ("chill lo-fi study beats") but fails for vibes that describe emotional context rather than musical texture ("music for when you've just broken up"). Claude Haiku extracts audio feature ranges that act as a hard filter before vector ranking — so "breakup music" gets valence < 0.3, which prunes upbeat songs that might match the text embedding. The cost is one extra LLM call per vibe query; the benefit is precision at the top of the result set.

**pgvector vs Pinecone.** Pinecone is purpose-built for vector search at scale and has a managed cloud offering, but it adds an external dependency, a per-query network hop, and cost that scales with index size. For a catalog of tens of thousands of songs, pgvector with an IVFFlat index (lists=100) retrieves nearest neighbors in single-digit milliseconds from the same Postgres instance that holds the catalog metadata. The tradeoff is that pgvector's recall degrades at very high list counts and requires re-indexing if the catalog grows significantly — at that point, migrating to a dedicated vector store becomes worth the operational complexity.

**Per-client rate limiting vs per-session.** Rate limiting per session would let a single aggressive client hammer the AI endpoints as long as others in the session aren't. Per-client limits (10 vibe queries per minute via `X-Client-ID`) ensure one user can't degrade the experience for others or run up API costs. The session also has its own secondary limit (50 vibe queries per hour) as a ceiling on total AI spend per session. The tradeoff is that anonymous clients who clear cookies or rotate IPs can bypass per-client limits — acceptable for a demo, not for production without auth.

**Sliding window vs fixed window rate limiting.** A fixed window resets at clock boundaries, which means a burst of requests at 11:59 and another at 12:00 can together exceed the intended limit. The sliding window implementation uses a Redis sorted set of request timestamps and counts entries within the last N seconds — no cliff edge, consistent enforcement. The tradeoff is slightly higher Redis memory per client (one sorted set entry per request vs one counter) and a more complex implementation. For rate limits in the tens-per-minute range, the memory cost is negligible.

**Anonymous client_id tokens vs requiring auth.** Requiring login before joining a session adds friction that kills the "share a link and listen together" use case. Instead, the server issues a UUID client token via `X-Client-ID` header (API clients) or `groove_client_id` cookie (browsers) and treats it as a pseudonymous identity for rate limiting and attribution. The tradeoff is that tokens are non-transferable and non-persistent — a user who clears cookies loses their identity — and there's no way to enforce per-user limits across devices. JWT auth is on the roadmap (see Limitations).

**Circuit breaker on AI API calls (fail open vs fail closed).** When Claude or OpenAI is down or slow, vibe queries should fail fast rather than queue up and time out. The circuit breaker opens after three consecutive failures in a 60-second window and stays open for 5 minutes (HALF_OPEN for one probe attempt). The decision was to fail closed — return a 503 to the client — rather than degrade to a keyword-only fallback search. The reason: a silent fallback that returns low-relevance results is worse UX than a clear error; it trains users to distrust the feature. The tradeoff is that during an AI outage, vibe queuing is entirely unavailable rather than degraded.

**Claude Haiku vs Sonnet for vibe extraction.** Vibe extraction is a structured JSON extraction task with a well-constrained output schema (6 numeric fields, one keyword list). Haiku handles it accurately at roughly 10x lower cost and 3x lower latency than Sonnet. Sonnet was benchmarked on the same prompt set during development and produced marginally better keyword quality on ambiguous inputs ("elevator music in a haunted house") but not enough to justify the cost difference at query volume. The tradeoff: Haiku occasionally returns feature ranges that are too wide (low confidence), which the hybrid search handles gracefully by falling back to pure vector retrieval.

**text-embedding-3-small vs text-embedding-3-large.** The catalog uses 1536-dimensional embeddings from text-embedding-3-small. The large model (3072 dims) produces modestly better cosine similarity scores on music description retrieval — roughly 3-5% improvement in recall@5 on the test set — but costs 5x more per embedding and requires a larger IVFFlat index. Given that the retrieval pipeline already applies feature-range filters before the vector step, the marginal recall improvement from a larger embedding doesn't meaningfully change end-to-end result quality. The tradeoff is a ceiling on embedding quality that would matter more if the catalog were larger and the feature filters were removed.

## API reference

All endpoints are prefixed with the server base URL. Rate limits are enforced per `X-Client-ID` header (or `groove_client_id` cookie).

| Method | Path | Request body | Response | Rate limit |
|--------|------|-------------|----------|------------|
| `POST` | `/sessions` | `{display_name: str}` | `{session_id, code, state, client_id}` | 5/hour per client |
| `POST` | `/sessions/{id}/join` | `{display_name: str}` | `{session_id, code, state, members}` | — |
| `GET` | `/sessions/{id}` | — | `{session_id, code, state, members, rtt_stats}` | — |
| `POST` | `/sessions/{id}/playback` | `{action: "play"\|"pause"\|"seek", position_ms: int, track_spotify_id?: str}` | `{state}` | — |
| `WS` | `/sessions/{id}/ws?display_name=X&client_id=X` | — | event stream | 20 connections/hour per IP |
| `POST` | `/sessions/{id}/queue/vibe` | `{vibe: str, display_name: str}` | `{tracks, vibe_interpreted_as, confidence, fallback_used}` | 10/min per client |
| `GET` | `/sessions/{id}/queue` | — | `[{id, spotify_id, title, artist, position, added_by, vibe_query, similarity_score, added_at}]` | — |
| `DELETE` | `/sessions/{id}/queue/{item_id}` | — | `204 No Content` | — |
| `GET` | `/health` | — | `{status, redis, postgres, redis_latency_ms, postgres_latency_ms}` | — |
| `GET` | `/tracks/demo` | — | `[{spotify_id, title, artist}]` | — |

**WebSocket events — server → client:**

| Event | Payload | When |
|-------|---------|------|
| `session_state` | Full `SessionState` (playback, members list, rtt_ms) | On connect |
| `playback_updated` | `PlaybackState` (status, position_ms, track_spotify_id, version) | On any playback change |
| `queue_updated` | `{added_tracks, added_by, vibe_query, vibe_interpreted_as, confidence}` | When tracks are added or removed |
| `member_joined` | `{client_id, display_name}` | When a new client connects |
| `member_left` | `{client_id}` | When a client disconnects |
| `rtt_update` | `{rtt_ms: int}` | After each heartbeat pong |
| `ping` | `{timestamp}` | Every 30 seconds |

**WebSocket events — client → server:**

| Event | Payload | Effect |
|-------|---------|--------|
| `playback` | `{action, position_ms, track_spotify_id?}` | Updates state, broadcasts `playback_updated` |
| `pong` | `{}` | Records RTT, triggers `rtt_update` broadcast |
| `ping` | `{}` | Server responds with `pong` |

## Limitations and roadmap

**True per-client clock sync.** The current RTT measurement tells the server how long a round trip takes per client, but playback commands are applied at server time and broadcast simultaneously to all clients regardless of their latency. A client with 200ms RTT hears a "play" command 100ms later than a client with 0ms RTT. The fix is to have the server apply a per-client timestamp offset when broadcasting playback commands — essentially NTP-style clock correction — using the measured RTT to compute each client's `server_time - rtt/2` offset.

**Multi-user taste resolver.** Vibe queries are first-come-first-served — whoever submits a vibe gets their tracks in the queue. There's no mechanism to weight or merge preferences across members. The obvious approach is to collect vibe signals from all active members over a time window, embed each one, and find the centroid in embedding space as the "room vibe" before searching.

**Reaction signal feedback loop.** There's no skip, like, or reaction signal in the current protocol. These signals are the primary data source for improving future recommendations — a skipped track is a strong negative signal, a track played to completion is a strong positive. Adding skip/like events to the WebSocket protocol and storing them in `session_play_history` would enable a feedback loop for recommendation quality over time.

**JWT auth.** Client identity is currently a UUID token with no authentication. This means rate limits can be bypassed by rotating the `X-Client-ID` header and session host authority can be impersonated. The fix is standard: issue short-lived JWTs on login, validate them on every request, and derive `client_id` from the token subject claim.

**Horizontal WebSocket scaling.** The `ConnectionManager` is an in-process singleton. Deploying two app instances means WebSocket connections are split between them, and a broadcast from instance A won't reach clients connected to instance B. The standard fix is to use Redis pub/sub as the broadcast bus: each instance subscribes to `session:{id}:events` and publishes outbound messages there rather than directly iterating its local connection set.

## Tech stack

| Component | Technology | Why |
|-----------|------------|-----|
| API framework | FastAPI 0.111 | Native async, WebSocket support, Pydantic integration |
| ASGI server | Uvicorn | Production-grade async server for FastAPI |
| Session state | Redis 7 + redis.asyncio | Sub-ms reads/writes for hot playback state; built-in TTL for session cleanup |
| Persistent store | PostgreSQL 15 + pgvector | ACID audit records; pgvector enables in-database vector similarity without a separate service |
| Async Postgres driver | asyncpg | Fastest async Postgres driver for Python; native prepared statements |
| Vibe extraction | Claude Haiku (Anthropic) | Structured JSON extraction from freeform text; fast and cheap at query volume |
| Song embeddings | text-embedding-3-small (OpenAI) | 1536-dim vectors; good recall/cost tradeoff for catalog sizes under ~100k songs |
| Spotify integration | Spotipy | Catalog ingestion and track metadata |
| Settings | Pydantic Settings | Type-safe `.env` parsing with validation |
| Structured logging | structlog | Machine-readable JSON logs with event codes; queryable in Railway's log stream |
| Containerization | Docker Compose | Reproducible local dev with pinned service versions |
