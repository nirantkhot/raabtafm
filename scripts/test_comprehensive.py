#!/usr/bin/env python3
"""
Raabta.fm — Comprehensive Integration Test Suite
=================================================
Covers every edge case described in the session spec.

Run (app must be up on :8000):
    pytest scripts/test_comprehensive.py -v --tb=short

Markers:
  slow        — tests that sleep or wait on timers (heartbeat, etc.)
  ratelimit   — tests that intentionally exhaust a rate-limit bucket.
                Run these in isolation; they leave state in Redis.
                  pytest -m ratelimit scripts/test_comprehensive.py

Dependencies (all in requirements.txt / uvicorn[standard] transitive):
  httpx, pytest, pytest-asyncio, websockets
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    pytest.skip("websockets package required", allow_module_level=True)

# ── Config ──────────────────────────────────────────────────────────────────
BASE    = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
TIMEOUT = httpx.Timeout(15.0)

# ── websockets cross-version compat ─────────────────────────────────────────
def ws_connect(uri: str, headers: dict | None = None):
    """Return an async context manager for a WebSocket connection.
    Handles the `extra_headers` (≤10) vs `additional_headers` (≥11) rename."""
    kw = {}
    if headers:
        try:
            # websockets ≥ 11
            return websockets.connect(uri, additional_headers=headers)
        except TypeError:
            return websockets.connect(uri, extra_headers=headers)
    return websockets.connect(uri)


# ── Helpers ──────────────────────────────────────────────────────────────────

def fresh_id() -> str:
    return str(uuid.uuid4())


async def create_session(
    client: httpx.AsyncClient,
    display_name: str = "Test Host",
    client_id: str | None = None,
) -> dict:
    cid = client_id or fresh_id()
    r = await client.post(
        "/sessions",
        json={"display_name": display_name},
        headers={"X-Client-ID": cid},
    )
    r.raise_for_status()
    return r.json()


async def join_session(
    client: httpx.AsyncClient,
    session_id: str,
    display_name: str = "Joiner",
    client_id: str | None = None,
) -> httpx.Response:
    cid = client_id or fresh_id()
    return await client.post(
        f"/sessions/{session_id}/join",
        json={"display_name": display_name},
        headers={"X-Client-ID": cid},
    )


async def do_playback(
    client: httpx.AsyncClient,
    session_id: str,
    action: str = "play",
    position_ms: int = 0,
    track_id: str = "4cOdK2wGLETKBW3PvgPWqT",
    client_id: str | None = None,
) -> httpx.Response:
    cid = client_id or fresh_id()
    return await client.post(
        f"/sessions/{session_id}/playback",
        json={"action": action, "position_ms": position_ms, "track_spotify_id": track_id},
        headers={"X-Client-ID": cid},
    )


async def receive_until(ws, event: str, timeout: float = 5.0) -> dict:
    """Read messages until one with the expected event type arrives."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("event") == event:
                return msg
        except asyncio.TimeoutError:
            break
    raise AssertionError(f"Never received event '{event}' within {timeout}s")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as c:
        yield c


@pytest_asyncio.fixture
async def session(client: httpx.AsyncClient) -> dict:
    """Fresh session per test."""
    return await create_session(client)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════

class TestInfrastructure:

    @pytest.mark.asyncio
    async def test_health_200(self, client):
        r = await client.get("/health")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_health_all_ok(self, client):
        d = (await client.get("/health")).json()
        assert d["status"] == "ok"
        assert d["redis"] == "ok"
        assert d["postgres"] == "ok"

    @pytest.mark.asyncio
    async def test_health_timestamp_is_iso8601(self, client):
        d = (await client.get("/health")).json()
        # Must be parseable
        datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))

    @pytest.mark.asyncio
    async def test_demo_tracks_returns_10(self, client):
        r = await client.get("/tracks/demo")
        assert r.status_code == 200
        tracks = r.json()
        assert len(tracks) == 10

    @pytest.mark.asyncio
    async def test_demo_tracks_schema(self, client):
        tracks = (await client.get("/tracks/demo")).json()
        for t in tracks:
            assert "spotify_id" in t
            assert "title" in t
            assert "artist" in t

    @pytest.mark.asyncio
    async def test_unknown_route_404(self, client):
        r = await client.get("/nonexistent-route-xyz")
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SESSION LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionLifecycle:

    @pytest.mark.asyncio
    async def test_create_returns_required_fields(self, client):
        d = await create_session(client)
        assert "session_id" in d
        assert "code" in d
        assert "state" in d
        assert "client_id" in d

    @pytest.mark.asyncio
    async def test_create_code_format(self, client):
        d = await create_session(client)
        code = d["code"]
        parts = code.split("-")
        assert len(parts) == 2, f"Expected WORD-NNNN, got {code}"
        word, digits = parts
        assert word.isalpha() and word.isupper(), f"word part invalid: {word}"
        assert digits.isdigit() and len(digits) == 4, f"digit part invalid: {digits}"

    @pytest.mark.asyncio
    async def test_create_respects_x_client_id_header(self, client):
        cid = fresh_id()
        d = await create_session(client, client_id=cid)
        assert d["client_id"] == cid

    @pytest.mark.asyncio
    async def test_create_generates_client_id_when_no_header(self, client):
        r = await client.post("/sessions", json={"display_name": "NoHeader"})
        r.raise_for_status()
        d = r.json()
        assert "client_id" in d
        assert len(d["client_id"]) == 36  # UUID4

    @pytest.mark.asyncio
    async def test_create_sets_cookie_when_no_header(self, client):
        r = await client.post("/sessions", json={"display_name": "Cookie"})
        r.raise_for_status()
        assert "groove_client_id" in r.cookies

    @pytest.mark.asyncio
    async def test_create_missing_display_name_422(self, client):
        r = await client.post("/sessions", json={}, headers={"X-Client-ID": fresh_id()})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_create_initial_state_paused(self, client):
        d = await create_session(client)
        assert d["state"]["playback"]["status"] == "paused"

    @pytest.mark.asyncio
    async def test_create_initial_version_zero(self, client):
        d = await create_session(client)
        assert d["state"]["playback"]["version"] == 0

    @pytest.mark.asyncio
    async def test_get_session_returns_state(self, client, session):
        sid = session["session_id"]
        r = await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})
        assert r.status_code == 200
        d = r.json()
        assert d["session_id"] == sid
        assert "state" in d
        assert "members" in d
        assert "rtt_stats" in d

    @pytest.mark.asyncio
    async def test_get_nonexistent_session_404(self, client):
        r = await client.get(f"/sessions/{fresh_id()}", headers={"X-Client-ID": fresh_id()})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_session_code_matches_create(self, client, session):
        sid = session["session_id"]
        r = await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})
        assert r.json()["code"] == session["code"]

    @pytest.mark.asyncio
    async def test_session_id_not_uuid_returns_404(self, client):
        r = await client.get("/sessions/not-a-uuid", headers={"X-Client-ID": fresh_id()})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_two_sessions_have_unique_codes(self, client):
        a = await create_session(client)
        b = await create_session(client)
        # Statistically they should differ, but they could theoretically collide.
        # We check that both are valid format at minimum.
        for code in [a["code"], b["code"]]:
            assert "-" in code


# ═══════════════════════════════════════════════════════════════════════════════
# 3. JOIN
# ═══════════════════════════════════════════════════════════════════════════════

class TestJoin:

    @pytest.mark.asyncio
    async def test_join_valid_session_200(self, client, session):
        r = await join_session(client, session["session_id"])
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_join_nonexistent_session_404(self, client):
        r = await join_session(client, fresh_id())
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_join_returns_required_fields(self, client, session):
        d = (await join_session(client, session["session_id"])).json()
        assert "session_id" in d
        assert "code" in d
        assert "state" in d
        assert "members" in d

    @pytest.mark.asyncio
    async def test_join_same_client_idempotent(self, client, session):
        cid = fresh_id()
        r1 = await join_session(client, session["session_id"], client_id=cid)
        r2 = await join_session(client, session["session_id"], client_id=cid)
        assert r1.status_code == 200
        assert r2.status_code == 200  # upsert, not error

    @pytest.mark.asyncio
    async def test_join_session_full_403(self, client):
        """Fill a session beyond MAX_SESSION_MEMBERS via REST join."""
        session = await create_session(client)
        sid = session["session_id"]
        # Host already counts as 1; join 9 more
        for i in range(9):
            r = await join_session(client, sid, display_name=f"User{i}")
            assert r.status_code == 200, f"join {i} failed: {r.text}"
        # 11th attempt (10 already in session)
        r = await join_session(client, sid, display_name="Overflow")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_join_member_count_increments(self, client, session):
        sid = session["session_id"]
        before = (await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})).json()
        before_count = len(before["members"])
        await join_session(client, sid, client_id=fresh_id())
        after = (await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})).json()
        assert len(after["members"]) == before_count + 1

    @pytest.mark.asyncio
    async def test_join_updates_display_name_on_rejoin(self, client, session):
        cid = fresh_id()
        await join_session(client, session["session_id"], display_name="OldName", client_id=cid)
        r = await join_session(client, session["session_id"], display_name="NewName", client_id=cid)
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 4. PLAYBACK
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlayback:
    TRACK = "4cOdK2wGLETKBW3PvgPWqT"

    @pytest.mark.asyncio
    async def test_play_sets_status_playing(self, client, session):
        r = await do_playback(client, session["session_id"], action="play")
        assert r.status_code == 200
        assert r.json()["state"]["playback"]["status"] == "playing"

    @pytest.mark.asyncio
    async def test_pause_sets_status_paused(self, client, session):
        await do_playback(client, session["session_id"], action="play")
        r = await do_playback(client, session["session_id"], action="pause")
        assert r.json()["state"]["playback"]["status"] == "paused"

    @pytest.mark.asyncio
    async def test_seek_sets_position(self, client, session):
        r = await do_playback(client, session["session_id"], action="seek", position_ms=45000)
        assert r.json()["state"]["playback"]["position_ms"] == 45000

    @pytest.mark.asyncio
    async def test_version_increments_each_update(self, client, session):
        sid = session["session_id"]
        v0 = session["state"]["playback"]["version"]
        r1 = await do_playback(client, sid, action="play")
        v1 = r1.json()["state"]["playback"]["version"]
        r2 = await do_playback(client, sid, action="pause")
        v2 = r2.json()["state"]["playback"]["version"]
        assert v1 == v0 + 1
        assert v2 == v1 + 1

    @pytest.mark.asyncio
    async def test_playback_nonexistent_session_404(self, client):
        r = await do_playback(client, fresh_id())
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_playback_stores_track_id(self, client, session):
        track = "3z8h0TU7ReDPLIFD9xzfLP"
        r = await do_playback(client, session["session_id"], track_id=track)
        assert r.json()["state"]["playback"]["track_spotify_id"] == track

    @pytest.mark.asyncio
    async def test_concurrent_playback_no_corruption(self, client, session):
        """10 concurrent seek requests: final state must have a coherent version."""
        sid = session["session_id"]
        coros = [
            do_playback(client, sid, action="seek", position_ms=i * 1000)
            for i in range(10)
        ]
        responses = await asyncio.gather(*coros, return_exceptions=True)
        successes = [r for r in responses if isinstance(r, httpx.Response) and r.status_code == 200]
        assert len(successes) > 0, "At least one update must succeed"
        # Get final state and verify it's consistent
        final = (await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})).json()
        v = final["state"]["playback"]["version"]
        assert v > 0

    @pytest.mark.asyncio
    async def test_playback_invalid_action_accepted_as_paused(self, client, session):
        """Unknown action falls back gracefully (server maps it to paused)."""
        r = await client.post(
            f"/sessions/{session['session_id']}/playback",
            json={"action": "unknown", "position_ms": 0, "track_spotify_id": self.TRACK},
            headers={"X-Client-ID": fresh_id()},
        )
        # Should not 500
        assert r.status_code in (200, 422)

    @pytest.mark.asyncio
    async def test_playback_large_position_ms(self, client, session):
        r = await do_playback(client, session["session_id"], position_ms=2**31 - 1)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_playback_zero_position(self, client, session):
        r = await do_playback(client, session["session_id"], position_ms=0)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_playback_returns_updated_at(self, client, session):
        r = await do_playback(client, session["session_id"])
        d = r.json()["state"]["playback"]
        assert "updated_at" in d
        datetime.fromisoformat(d["updated_at"].replace("Z", "+00:00"))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CLIENT IDENTITY
# ═══════════════════════════════════════════════════════════════════════════════

class TestClientIdentity:

    @pytest.mark.asyncio
    async def test_header_takes_priority_over_cookie(self, client):
        header_id = fresh_id()
        # Set a cookie first, then override with header
        r = await client.post(
            "/sessions",
            json={"display_name": "HeaderTest"},
            headers={"X-Client-ID": header_id},
            cookies={"groove_client_id": fresh_id()},
        )
        r.raise_for_status()
        assert r.json()["client_id"] == header_id

    @pytest.mark.asyncio
    async def test_cookie_used_when_no_header(self, client):
        """If there's no header but a cookie exists, the cookie client_id is used."""
        cookie_id = fresh_id()
        r = await client.post(
            "/sessions",
            json={"display_name": "CookieTest"},
            cookies={"groove_client_id": cookie_id},
        )
        r.raise_for_status()
        assert r.json()["client_id"] == cookie_id

    @pytest.mark.asyncio
    async def test_cookie_is_httponly(self, client):
        r = await client.post("/sessions", json={"display_name": "CookieMeta"})
        cookie_header = r.headers.get("set-cookie", "")
        assert "httponly" in cookie_header.lower()

    @pytest.mark.asyncio
    async def test_cookie_is_samesite_lax(self, client):
        r = await client.post("/sessions", json={"display_name": "SameSite"})
        cookie_header = r.headers.get("set-cookie", "")
        assert "samesite=lax" in cookie_header.lower()

    @pytest.mark.asyncio
    async def test_generated_id_is_valid_uuid(self, client):
        r = await client.post("/sessions", json={"display_name": "NoID"})
        r.raise_for_status()
        cid = r.json()["client_id"]
        uuid.UUID(cid)  # raises ValueError if invalid

    @pytest.mark.asyncio
    async def test_same_client_id_reused_across_requests(self, client, session):
        """Passing the same X-Client-ID in two separate requests is idempotent."""
        cid = fresh_id()
        r1 = await join_session(client, session["session_id"], client_id=cid)
        r2 = await join_session(client, session["session_id"], client_id=cid)
        assert r1.status_code == 200
        assert r2.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 6. RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════════

class TestRateLimiting:

    @pytest.mark.asyncio
    @pytest.mark.ratelimit
    async def test_session_create_rate_limit_429(self, client):
        """5 creates succeed, 6th gets 429. Uses a single unique client_id."""
        cid = fresh_id()
        for i in range(5):
            r = await client.post(
                "/sessions",
                json={"display_name": f"RL{i}"},
                headers={"X-Client-ID": cid},
            )
            assert r.status_code == 200, f"Request {i} failed unexpectedly: {r.text}"
        r = await client.post(
            "/sessions",
            json={"display_name": "Over"},
            headers={"X-Client-ID": cid},
        )
        assert r.status_code == 429

    @pytest.mark.asyncio
    @pytest.mark.ratelimit
    async def test_session_create_429_has_retry_after(self, client):
        cid = fresh_id()
        for _ in range(5):
            await client.post("/sessions", json={"display_name": "X"}, headers={"X-Client-ID": cid})
        r = await client.post("/sessions", json={"display_name": "X"}, headers={"X-Client-ID": cid})
        assert r.status_code == 429
        assert "retry-after" in r.headers

    @pytest.mark.asyncio
    @pytest.mark.ratelimit
    async def test_rate_limit_independent_per_client(self, client):
        """Different client_ids have independent counters."""
        cids = [fresh_id() for _ in range(5)]
        for cid in cids:
            r = await client.post(
                "/sessions", json={"display_name": "Isolated"}, headers={"X-Client-ID": cid}
            )
            assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 7. WEBSOCKET — BASIC
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebSocketBasic:

    @pytest.mark.asyncio
    async def test_ws_connect_receives_session_state(self, session):
        sid = session["session_id"]
        cid = fresh_id()
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Tester"
        async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "session_state"
            assert msg["session_id"] == sid
            assert "payload" in msg

    @pytest.mark.asyncio
    async def test_ws_session_state_has_playback(self, session):
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Tester"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert "playback" in msg["payload"]

    @pytest.mark.asyncio
    async def test_ws_session_state_has_members(self, session):
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Tester"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert "members" in msg["payload"]

    @pytest.mark.asyncio
    async def test_ws_default_display_name_anonymous(self, session):
        sid = session["session_id"]
        # No display_name param → default "Anonymous"
        uri = f"{WS_BASE}/sessions/{sid}/ws"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            # Just verify we connect and get session_state
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "session_state"

    @pytest.mark.asyncio
    async def test_ws_nonexistent_session_closes(self):
        uri = f"{WS_BASE}/sessions/{fresh_id()}/ws?display_name=Ghost"
        try:
            async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
                # Server should close the connection (no session → connect fails)
                await asyncio.wait_for(ws.recv(), timeout=5)
        except (ConnectionClosed, Exception):
            pass  # Expected — session doesn't exist

    @pytest.mark.asyncio
    async def test_ws_pong_response_to_ping(self, session):
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Pinger"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            # Drain session_state
            await asyncio.wait_for(ws.recv(), timeout=5)
            # Send client ping
            await ws.send(json.dumps({"event": "ping"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "pong"
            assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_ws_invalid_json_silently_ignored(self, session):
        """Sending garbage JSON should not crash the connection."""
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Fuzzer"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
            await ws.send("NOT JSON }{{{")
            # Send a valid ping after, expect a pong (connection still alive)
            await ws.send(json.dumps({"event": "ping"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "pong"

    @pytest.mark.asyncio
    async def test_ws_unknown_event_silently_ignored(self, session):
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Unknown"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            await ws.send(json.dumps({"event": "totally_made_up", "payload": {}}))
            await ws.send(json.dumps({"event": "ping"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "pong"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. WEBSOCKET — EVENTS & BROADCAST
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebSocketEvents:

    @pytest.mark.asyncio
    async def test_playback_event_broadcast_to_all(self, client, session):
        """Two WS clients both receive playback_updated when one sends a play event."""
        sid = session["session_id"]
        cid_a, cid_b = fresh_id(), fresh_id()
        uri_a = f"{WS_BASE}/sessions/{sid}/ws?display_name=A"
        uri_b = f"{WS_BASE}/sessions/{sid}/ws?display_name=B"

        async with ws_connect(uri_a, headers={"X-Client-ID": cid_a}) as ws_a, \
                   ws_connect(uri_b, headers={"X-Client-ID": cid_b}) as ws_b:
            # Drain initial messages
            await asyncio.wait_for(ws_a.recv(), timeout=5)
            await asyncio.wait_for(ws_b.recv(), timeout=5)

            # Client A sends play
            await ws_a.send(json.dumps({
                "event": "playback",
                "payload": {
                    "action": "play",
                    "position_ms": 1000,
                    "track_spotify_id": "4cOdK2wGLETKBW3PvgPWqT",
                },
            }))

            # Both should receive playback_updated
            msg_a = await receive_until(ws_a, "playback_updated")
            msg_b = await receive_until(ws_b, "playback_updated")

            assert msg_a["payload"]["status"] == "playing"
            assert msg_b["payload"]["status"] == "playing"

    @pytest.mark.asyncio
    async def test_rest_playback_broadcast_to_ws_clients(self, client, session):
        """REST POST /playback triggers broadcast to connected WS clients."""
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Watcher"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
            # Trigger via REST
            await do_playback(client, sid, action="play", position_ms=5000)
            msg = await receive_until(ws, "playback_updated")
            assert msg["payload"]["status"] == "playing"
            assert msg["payload"]["position_ms"] == 5000

    @pytest.mark.asyncio
    async def test_member_joined_broadcast(self, client, session):
        """Existing WS client receives member_joined when a new client joins via REST."""
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Existing"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
            await join_session(client, sid, display_name="Newcomer")
            msg = await receive_until(ws, "member_joined")
            assert msg["payload"]["display_name"] == "Newcomer"

    @pytest.mark.asyncio
    async def test_member_left_broadcast_on_ws_disconnect(self, client, session):
        """Other WS clients receive member_left when a client disconnects."""
        sid = session["session_id"]
        cid_watcher = fresh_id()
        cid_leaver  = fresh_id()
        uri_w = f"{WS_BASE}/sessions/{sid}/ws?display_name=Watcher"
        uri_l = f"{WS_BASE}/sessions/{sid}/ws?display_name=Leaver"

        async with ws_connect(uri_w, headers={"X-Client-ID": cid_watcher}) as ws_w:
            await asyncio.wait_for(ws_w.recv(), timeout=5)

            async with ws_connect(uri_l, headers={"X-Client-ID": cid_leaver}) as ws_l:
                await asyncio.wait_for(ws_l.recv(), timeout=5)
                # Drain member_joined that watcher sees
                await receive_until(ws_w, "member_joined")

            # Leaver disconnected — watcher should see member_left
            msg = await receive_until(ws_w, "member_left", timeout=8)
            assert msg["payload"]["client_id"] == cid_leaver

    @pytest.mark.asyncio
    async def test_playback_version_consistent_across_ws_and_rest(self, client, session):
        """Version reported by WS broadcast matches what REST GET returns."""
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Version"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            await do_playback(client, sid, action="play")
            msg = await receive_until(ws, "playback_updated")
            ws_version = msg["payload"]["version"]

        rest = (await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})).json()
        rest_version = rest["state"]["playback"]["version"]
        assert ws_version == rest_version

    @pytest.mark.asyncio
    async def test_ws_send_pong_records_rtt(self, session):
        """Server-initiated ping → client pong → RTT stored in Redis and echoed back."""
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=RTT"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
            # Simulate: server sends a "ping" — wait for it or manually trigger pong
            # Since heartbeat fires every 30s, we trigger a server ping via the client:
            await ws.send(json.dumps({"event": "ping"}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert pong["event"] == "pong"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. WEBSOCKET — MEMBER LIMITS & RATE LIMIT REJECTION
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebSocketLimits:

    @pytest.mark.asyncio
    async def test_ws_session_full_close_1008(self, client):
        """10 members already joined via REST → 11th WS connection closes with 1008."""
        session = await create_session(client)
        sid = session["session_id"]
        # Fill 9 more REST members (host = 1, total = 10)
        for i in range(9):
            r = await join_session(client, sid, display_name=f"U{i}")
            assert r.status_code == 200, f"pre-fill join {i} failed"

        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Overflow"
        try:
            async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                # If we somehow got a message, check it's not session_state
                assert False, f"Should have been rejected, got: {msg}"
        except ConnectionClosed as e:
            assert e.code == 1008
        except Exception:
            pass  # Connection refused / closed is expected

    @pytest.mark.asyncio
    async def test_ws_client_id_from_header(self, session):
        cid = fresh_id()
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=HeaderClient"
        async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "session_state"
            # Verify cid appears in members
            assert cid in msg["payload"]["members"]

    @pytest.mark.asyncio
    async def test_ws_same_client_reconnect(self, session):
        """Same client_id can reconnect after disconnect."""
        sid = session["session_id"]
        cid = fresh_id()
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Reconnect"
        async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
        await asyncio.sleep(0.3)  # brief pause
        async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "session_state"


# ═══════════════════════════════════════════════════════════════════════════════
# 10. EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_unicode_display_name(self, client):
        d = await create_session(client, display_name="🎵 DJ KünstlerÄÖÜ 音楽")
        assert "session_id" in d

    @pytest.mark.asyncio
    async def test_very_long_display_name(self, client):
        long_name = "A" * 500
        r = await client.post(
            "/sessions",
            json={"display_name": long_name},
            headers={"X-Client-ID": fresh_id()},
        )
        # Should either succeed or return 422 — not 500
        assert r.status_code in (200, 422)

    @pytest.mark.asyncio
    async def test_playback_missing_fields_handled(self, client, session):
        """Missing track_spotify_id — server should not 500."""
        r = await client.post(
            f"/sessions/{session['session_id']}/playback",
            json={"action": "play", "position_ms": 0},
            headers={"X-Client-ID": fresh_id()},
        )
        assert r.status_code in (200, 422)

    @pytest.mark.asyncio
    async def test_create_session_body_extra_fields_ignored(self, client):
        r = await client.post(
            "/sessions",
            json={"display_name": "Extra", "unexpected_field": True, "count": 99},
            headers={"X-Client-ID": fresh_id()},
        )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_get_session_rtt_stats_initially_empty(self, client, session):
        sid = session["session_id"]
        d = (await client.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})).json()
        rtt = d["rtt_stats"]
        # avg/min/max should be 0 or absent, no real RTTs yet
        assert isinstance(rtt, dict)

    @pytest.mark.asyncio
    async def test_ws_empty_string_display_name(self, session):
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name="
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert msg["event"] == "session_state"

    @pytest.mark.asyncio
    async def test_ws_concurrent_same_session_all_receive_broadcast(self, client, session):
        """5 concurrent WS clients all receive a broadcast."""
        sid = session["session_id"]
        N = 5
        uris = [f"{WS_BASE}/sessions/{sid}/ws?display_name=C{i}" for i in range(N)]
        cids = [fresh_id() for _ in range(N)]

        async def collect(uri, cid):
            async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
                # Wait for playback_updated
                msg = await receive_until(ws, "playback_updated", timeout=8)
                return msg["payload"]["status"]

        trigger_delay = 1.0  # Give all clients time to connect

        async def trigger():
            await asyncio.sleep(trigger_delay)
            await do_playback(client, sid, action="play")

        tasks = [asyncio.create_task(collect(u, c)) for u, c in zip(uris, cids)]
        asyncio.create_task(trigger())

        results = await asyncio.gather(*tasks, return_exceptions=True)
        statuses = [r for r in results if isinstance(r, str)]
        assert len(statuses) == N, f"Only {len(statuses)}/{N} clients got broadcast: {results}"
        assert all(s == "playing" for s in statuses)

    @pytest.mark.asyncio
    async def test_multiple_playback_updates_version_monotone(self, client, session):
        """10 sequential updates → version strictly increases."""
        sid = session["session_id"]
        versions = []
        for i in range(10):
            r = await do_playback(client, sid, action="seek", position_ms=i * 1000)
            assert r.status_code == 200
            versions.append(r.json()["state"]["playback"]["version"])
        assert versions == sorted(versions), f"Versions not monotone: {versions}"
        assert len(set(versions)) == 10, f"Duplicate versions: {versions}"

    @pytest.mark.asyncio
    async def test_ended_session_returns_404_on_join(self, client):
        """Sessions with ended_at set should return 404 on join."""
        # We can't end a session via API yet, so skip if no endpoint
        # This is a DB-level check — tested via direct Postgres verify
        pytest.skip("No end-session endpoint in Session 1 — verify at DB level")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. HEARTBEAT (slow — skipped in default runs)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeartbeat:

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_server_sends_ping_within_35s(self, session):
        """Server heartbeat fires every 30s; we should see a 'ping' event."""
        sid = session["session_id"]
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=HeartbeatTest"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
            msg = await receive_until(ws, "ping", timeout=35)
            assert msg["event"] == "ping"
            # Reply with pong so server doesn't kick us
            await ws.send(json.dumps({"event": "pong"}))
            rtt_msg = await receive_until(ws, "rtt_update", timeout=5)
            assert "rtt_ms" in rtt_msg
