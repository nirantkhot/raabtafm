#!/usr/bin/env python3
"""
Raabta.fm — Scale & Connection Load Test
==========================================
Tests connection throughput, persistence, broadcast latency,
concurrent update consistency, and pool exhaustion.

Usage:
    # Quick smoke test (10 sessions, 5 clients each, 15s hold):
    python scripts/load_test.py

    # Full scale test:
    python scripts/load_test.py --sessions 50 --clients 8 --duration 60

    # Specific scenario only:
    python scripts/load_test.py --scenario connections
    python scripts/load_test.py --scenario broadcast
    python scripts/load_test.py --scenario persistence
    python scripts/load_test.py --scenario concurrent_updates
    python scripts/load_test.py --scenario pool_exhaustion

Requires: httpx, websockets  (both present in requirements.txt / uvicorn[standard])
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    raise SystemExit("pip install websockets")

# ── Config ───────────────────────────────────────────────────────────────────
BASE    = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
MAX_SESSION_MEMBERS = 10


# ── Results ──────────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    name: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Utilities ────────────────────────────────────────────────────────────────

def fresh_id() -> str:
    return str(uuid.uuid4())


def ws_connect(uri: str, headers: dict | None = None):
    if headers:
        try:
            return websockets.connect(uri, additional_headers=headers, open_timeout=10)
        except TypeError:
            return websockets.connect(uri, extra_headers=headers, open_timeout=10)
    return websockets.connect(uri, open_timeout=10)


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    data = sorted(data)
    idx = (len(data) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (idx - lo)


async def create_session(client: httpx.AsyncClient, display_name: str = "LoadHost") -> dict:
    cid = fresh_id()
    r = await client.post(
        "/sessions",
        json={"display_name": display_name},
        headers={"X-Client-ID": cid},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


async def do_playback(
    client: httpx.AsyncClient,
    session_id: str,
    action: str = "seek",
    position_ms: int = 0,
    track_id: str = "4cOdK2wGLETKBW3PvgPWqT",
) -> httpx.Response:
    return await client.post(
        f"/sessions/{session_id}/playback",
        json={"action": action, "position_ms": position_ms, "track_spotify_id": track_id},
        headers={"X-Client-ID": fresh_id()},
        timeout=10,
    )


async def drain_until(ws, event: str, timeout: float = 5.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            msg = json.loads(raw)
            if msg.get("event") == event:
                return msg
        except (asyncio.TimeoutError, ConnectionClosed):
            break
    return None


def print_banner(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def print_result(result: ScenarioResult) -> None:
    status = "✅ PASS" if result.passed else "❌ FAIL"
    print(f"\n{status}  {result.name}")
    for k, v in result.metrics.items():
        if isinstance(v, float):
            print(f"     {k:35s} {v:.2f}")
        else:
            print(f"     {k:35s} {v}")
    for note in result.notes:
        print(f"     ℹ  {note}")
    for err in result.errors:
        print(f"     ⚠  {err}")


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — Connection Establishment Throughput
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_connections(
    num_sessions: int,
    clients_per_session: int,
) -> ScenarioResult:
    """
    Create `num_sessions` sessions, then establish `clients_per_session`
    WebSocket connections per session concurrently. Measure:
      - Total connections attempted vs established
      - p50 / p95 / p99 connect latency
      - Error breakdown
    """
    name = f"Connection Establishment ({num_sessions}s × {clients_per_session}c)"
    print_banner(name)

    clients_per_session = min(clients_per_session, MAX_SESSION_MEMBERS)
    connect_times: list[float] = []
    errors: list[str] = []
    attempted = 0
    established = 0

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        # Create sessions
        print(f"  Creating {num_sessions} sessions...")
        session_tasks = [create_session(http) for _ in range(num_sessions)]
        sessions = await asyncio.gather(*session_tasks, return_exceptions=True)
        ok_sessions = [s for s in sessions if isinstance(s, dict)]
        print(f"  Sessions created: {len(ok_sessions)}/{num_sessions}")
        if not ok_sessions:
            return ScenarioResult(name, False, errors=["No sessions could be created"])

        # Establish WS connections
        print(f"  Connecting {clients_per_session} WS clients per session...")
        t_start_all = time.monotonic()

        async def connect_one(sid: str) -> bool:
            nonlocal attempted, established
            attempted += 1
            cid = fresh_id()
            uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=LoadClient"
            t0 = time.monotonic()
            try:
                async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
                    connect_times.append((time.monotonic() - t0) * 1000)
                    established += 1
                    # Drain initial session_state
                    await asyncio.wait_for(ws.recv(), timeout=5)
                    return True
            except ConnectionClosed as e:
                errors.append(f"WS closed {e.code}: {e.reason}")
                return False
            except Exception as ex:
                errors.append(f"WS error: {type(ex).__name__}: {ex}")
                return False

        tasks = [
            connect_one(s["session_id"])
            for s in ok_sessions
            for _ in range(clients_per_session)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        total_time = (time.monotonic() - t_start_all) * 1000

    metrics = {
        "sessions_created":      len(ok_sessions),
        "connections_attempted": attempted,
        "connections_established": established,
        "success_rate_%":        round(100 * established / max(attempted, 1), 1),
        "total_time_ms":         round(total_time, 1),
        "connections_per_sec":   round(established / max(total_time / 1000, 0.001), 1),
    }
    if connect_times:
        metrics["connect_p50_ms"] = round(percentile(connect_times, 50), 1)
        metrics["connect_p95_ms"] = round(percentile(connect_times, 95), 1)
        metrics["connect_p99_ms"] = round(percentile(connect_times, 99), 1)
        metrics["connect_max_ms"] = round(max(connect_times), 1)

    error_summary = {}
    for e in errors:
        key = e[:60]
        error_summary[key] = error_summary.get(key, 0) + 1
    error_notes = [f"{v}× {k}" for k, v in error_summary.items()]

    passed = established > 0 and (established / max(attempted, 1)) >= 0.95
    return ScenarioResult(name, passed, metrics, error_notes)


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Connection Persistence
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_persistence(
    num_sessions: int,
    clients_per_session: int,
    hold_seconds: int,
) -> ScenarioResult:
    """
    Establish `clients_per_session` WS connections per session and hold them
    for `hold_seconds`. Measures how many survive the full duration.
    Also verifies connections survive through a server heartbeat tick if
    hold_seconds > 30.
    """
    name = f"Connection Persistence ({clients_per_session}c × {hold_seconds}s hold)"
    print_banner(name)

    clients_per_session = min(clients_per_session, MAX_SESSION_MEMBERS)
    alive_count = 0
    total_count = 0
    drop_reasons: list[str] = []
    alive_lock = asyncio.Lock()

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        sessions = await asyncio.gather(
            *[create_session(http) for _ in range(num_sessions)],
            return_exceptions=True,
        )
        ok_sessions = [s for s in sessions if isinstance(s, dict)]
        print(f"  Sessions ready: {len(ok_sessions)}")

        async def hold_connection(sid: str) -> bool:
            nonlocal total_count
            async with alive_lock:
                total_count += 1
            cid = fresh_id()
            uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=PersistClient"
            t0 = time.monotonic()
            try:
                async with ws_connect(uri, headers={"X-Client-ID": cid}) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
                    deadline = t0 + hold_seconds
                    while time.monotonic() < deadline:
                        remaining = deadline - time.monotonic()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2))
                            msg = json.loads(raw)
                            # Respond to server pings
                            if msg.get("event") == "ping":
                                await ws.send(json.dumps({"event": "pong"}))
                        except asyncio.TimeoutError:
                            continue
                        except ConnectionClosed as e:
                            drop_reasons.append(f"Dropped after {time.monotonic()-t0:.1f}s: {e.code}")
                            return False
                    return True
            except Exception as ex:
                drop_reasons.append(f"Failed to connect: {ex}")
                return False

        print(f"  Holding {len(ok_sessions) * clients_per_session} connections for {hold_seconds}s…")
        results = await asyncio.gather(
            *[hold_connection(s["session_id"]) for s in ok_sessions for _ in range(clients_per_session)],
            return_exceptions=True,
        )
        alive_count = sum(1 for r in results if r is True)

    survival_rate = 100 * alive_count / max(total_count, 1)
    metrics = {
        "total_connections":  total_count,
        "survived_full_hold": alive_count,
        "survival_rate_%":    round(survival_rate, 1),
        "hold_seconds":       hold_seconds,
    }
    notes = drop_reasons[:10]  # show first 10
    passed = survival_rate >= 98.0
    return ScenarioResult(name, passed, metrics, notes)


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — Broadcast Latency
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_broadcast_latency(
    num_receivers: int = 9,
    num_rounds: int = 20,
) -> ScenarioResult:
    """
    One sender + `num_receivers` listeners in the same session.
    Sender fires a REST playback update; we measure the time until
    ALL receivers get the `playback_updated` WS event.
    """
    num_receivers = min(num_receivers, MAX_SESSION_MEMBERS - 1)
    name = f"Broadcast Latency ({num_receivers} receivers, {num_rounds} rounds)"
    print_banner(name)

    round_latencies: list[float] = []
    errors: list[str] = []

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        session = await create_session(http, display_name="BroadcastHost")
        sid = session["session_id"]
        print(f"  Session: {session['code']}  ({sid[:8]}…)")

        receiver_cids = [fresh_id() for _ in range(num_receivers)]
        receiver_ws: list[Any] = []

        # Connect all receivers
        print(f"  Connecting {num_receivers} receivers…")
        for cid in receiver_cids:
            uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=Recv"
            try:
                ws = await ws_connect(uri, headers={"X-Client-ID": cid}).__aenter__()
                await asyncio.wait_for(ws.recv(), timeout=5)  # session_state
                receiver_ws.append(ws)
            except Exception as ex:
                errors.append(f"Receiver connect failed: {ex}")

        if not receiver_ws:
            return ScenarioResult(name, False, errors=["No receivers connected"])

        actual_receivers = len(receiver_ws)
        print(f"  {actual_receivers} receivers live. Running {num_rounds} broadcast rounds…")

        for rnd in range(num_rounds):
            t_send = time.monotonic()
            await do_playback(http, sid, action="seek", position_ms=rnd * 1000)

            # Wait for all receivers
            recv_tasks = [
                drain_until(ws, "playback_updated", timeout=5.0)
                for ws in receiver_ws
            ]
            results = await asyncio.gather(*recv_tasks, return_exceptions=True)
            t_last_recv = time.monotonic()

            got = sum(1 for r in results if isinstance(r, dict))
            if got < actual_receivers:
                errors.append(f"Round {rnd}: only {got}/{actual_receivers} received")
            else:
                round_latencies.append((t_last_recv - t_send) * 1000)

        # Cleanup
        for ws in receiver_ws:
            try:
                await ws.__aexit__(None, None, None)
            except Exception:
                pass

    metrics = {
        "receivers_connected": actual_receivers,
        "rounds_completed":    len(round_latencies),
        "rounds_with_errors":  num_rounds - len(round_latencies),
    }
    if round_latencies:
        metrics["broadcast_p50_ms"] = round(percentile(round_latencies, 50), 1)
        metrics["broadcast_p95_ms"] = round(percentile(round_latencies, 95), 1)
        metrics["broadcast_p99_ms"] = round(percentile(round_latencies, 99), 1)
        metrics["broadcast_max_ms"] = round(max(round_latencies), 1)
        metrics["broadcast_min_ms"] = round(min(round_latencies), 1)

    passed = (
        len(round_latencies) >= num_rounds * 0.9
        and (not round_latencies or percentile(round_latencies, 95) < 200)
    )
    return ScenarioResult(name, passed, metrics, errors[:5])


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Concurrent Playback Update Consistency
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_concurrent_updates(
    concurrency: int = 20,
    rounds: int = 5,
) -> ScenarioResult:
    """
    Fire `concurrency` simultaneous POST /playback requests to the same session.
    After each wave, verify:
      - No 500 errors
      - Final version equals exactly the number of successful updates
      - State is internally consistent (no partial writes)
    """
    name = f"Concurrent Playback Updates ({concurrency} concurrent × {rounds} rounds)"
    print_banner(name)

    errors: list[str] = []
    version_drifts: list[int] = []
    success_counts: list[int] = []

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        session = await create_session(http, "ConcurrencyHost")
        sid = session["session_id"]
        initial_version = session["state"]["playback"]["version"]
        print(f"  Session: {sid[:8]}…  initial version={initial_version}")

        cumulative_successes = 0
        for rnd in range(rounds):
            tasks = [
                do_playback(http, sid, action="seek", position_ms=(rnd * 1000 + i))
                for i in range(concurrency)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            ok = [r for r in responses if isinstance(r, httpx.Response) and r.status_code == 200]
            err_500 = [r for r in responses if isinstance(r, httpx.Response) and r.status_code >= 500]
            err_other = [r for r in responses if isinstance(r, httpx.Response) and r.status_code not in (200, 429, 503)]

            if err_500:
                errors.append(f"Round {rnd}: {len(err_500)} server errors (500+)")
            if err_other:
                errors.append(f"Round {rnd}: {len(err_other)} unexpected status codes")

            cumulative_successes += len(ok)
            success_counts.append(len(ok))

            # Check final state
            state_resp = await http.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})
            current_version = state_resp.json()["state"]["playback"]["version"]
            expected_version = initial_version + cumulative_successes
            drift = abs(current_version - expected_version)
            version_drifts.append(drift)
            if drift > 0:
                errors.append(
                    f"Round {rnd}: version drift={drift} "
                    f"(expected {expected_version}, got {current_version})"
                )
            print(f"  Round {rnd+1}/{rounds}: {len(ok)}/{concurrency} ok, version={current_version}, drift={drift}")

    metrics = {
        "total_requests":      rounds * concurrency,
        "total_successes":     sum(success_counts),
        "success_rate_%":      round(100 * sum(success_counts) / max(rounds * concurrency, 1), 1),
        "max_version_drift":   max(version_drifts) if version_drifts else 0,
        "avg_success_per_round": round(statistics.mean(success_counts), 1) if success_counts else 0,
    }
    passed = max(version_drifts, default=0) == 0 and not any("server errors" in e for e in errors)
    return ScenarioResult(name, passed, metrics, errors[:10])


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 — asyncpg Pool Exhaustion
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_pool_exhaustion() -> ScenarioResult:
    """
    Fire more concurrent REST requests than the asyncpg pool size (max=10).
    Requests beyond the pool size must queue and eventually succeed — NOT crash.
    We fire 25 concurrent GET /sessions/{id} requests and verify ≥ 95% succeed.
    """
    name = "asyncpg Pool Exhaustion (25 concurrent requests, pool max=10)"
    print_banner(name)
    CONCURRENCY = 25

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        session = await create_session(http)
        sid = session["session_id"]
        print(f"  Session: {sid[:8]}…")
        print(f"  Firing {CONCURRENCY} concurrent GET /sessions/{{id}}…")

        t0 = time.monotonic()
        tasks = [
            http.get(f"/sessions/{sid}", headers={"X-Client-ID": fresh_id()})
            for _ in range(CONCURRENCY)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = (time.monotonic() - t0) * 1000

    ok = [r for r in responses if isinstance(r, httpx.Response) and r.status_code == 200]
    errs = [r for r in responses if not isinstance(r, httpx.Response) or r.status_code != 200]
    error_msgs = [
        f"{type(e).__name__}: {e}" if isinstance(e, Exception) else f"HTTP {e.status_code}"
        for e in errs
    ]

    metrics = {
        "concurrency":       CONCURRENCY,
        "pool_max_size":     10,
        "succeeded":         len(ok),
        "failed":            len(errs),
        "success_rate_%":    round(100 * len(ok) / CONCURRENCY, 1),
        "total_elapsed_ms":  round(elapsed, 1),
        "rps":               round(CONCURRENCY / max(elapsed / 1000, 0.001), 1),
    }
    passed = len(ok) / CONCURRENCY >= 0.95
    return ScenarioResult(name, passed, metrics, error_msgs[:5])


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 6 — Session Cap Enforcement
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_session_cap() -> ScenarioResult:
    """
    Verify MAX_SESSION_MEMBERS=10 is enforced:
      - 10 members can join (including host)
      - 11th REST join → 403
      - 11th WS connect → close 1008
    """
    name = "Session Cap Enforcement (MAX=10)"
    print_banner(name)
    errors: list[str] = []

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        session = await create_session(http, "CapHost")
        sid = session["session_id"]
        print(f"  Session: {sid[:8]}…  Filling to {MAX_SESSION_MEMBERS}…")

        for i in range(MAX_SESSION_MEMBERS - 1):
            r = await http.post(
                f"/sessions/{sid}/join",
                json={"display_name": f"U{i}"},
                headers={"X-Client-ID": fresh_id()},
            )
            if r.status_code != 200:
                errors.append(f"Join {i} failed with {r.status_code}: {r.text[:80]}")

        rest_403 = await http.post(
            f"/sessions/{sid}/join",
            json={"display_name": "Overflow"},
            headers={"X-Client-ID": fresh_id()},
        )

    ws_1008 = False
    ws_reason = ""
    try:
        uri = f"{WS_BASE}/sessions/{sid}/ws?display_name=WsOverflow"
        async with ws_connect(uri, headers={"X-Client-ID": fresh_id()}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
    except ConnectionClosed as e:
        ws_1008 = (e.code == 1008)
        ws_reason = e.reason or ""
    except Exception as ex:
        errors.append(f"WS overflow unexpected exception: {ex}")

    metrics = {
        "rest_403_on_11th_join": rest_403.status_code == 403,
        "ws_1008_on_11th_conn":  ws_1008,
        "ws_close_reason":       ws_reason,
    }
    passed = (rest_403.status_code == 403) and ws_1008 and not errors
    if rest_403.status_code != 403:
        errors.append(f"REST returned {rest_403.status_code}, expected 403")
    if not ws_1008:
        errors.append("WS did not close with code 1008")
    return ScenarioResult(name, passed, metrics, errors)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

SCENARIOS = {
    "connections":       "Connection Establishment Throughput",
    "persistence":       "Connection Persistence",
    "broadcast":         "Broadcast Latency",
    "concurrent_updates":"Concurrent Playback Update Consistency",
    "pool_exhaustion":   "asyncpg Pool Exhaustion",
    "session_cap":       "Session Cap Enforcement",
}


async def run_all(args: argparse.Namespace) -> None:
    results: list[ScenarioResult] = []
    run = set(args.scenario) if args.scenario else set(SCENARIOS.keys())

    print(f"\n🎵  Raabta.fm Load Test  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"    Target: {BASE}")
    print(f"    Scenarios: {', '.join(run)}")

    # Health check first
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=5) as c:
            r = await c.get("/health")
            h = r.json()
            if h.get("status") != "ok":
                print(f"\n❌  Health check failed: {h}")
                return
            print(f"\n✅  Health OK (redis={h['redis']}, postgres={h['postgres']})")
    except Exception as ex:
        print(f"\n❌  Cannot reach {BASE}: {ex}")
        return

    if "connections" in run:
        r = await scenario_connections(
            num_sessions=args.sessions,
            clients_per_session=args.clients,
        )
        results.append(r)
        print_result(r)

    if "persistence" in run:
        r = await scenario_persistence(
            num_sessions=min(args.sessions, 5),
            clients_per_session=min(args.clients, 5),
            hold_seconds=args.duration,
        )
        results.append(r)
        print_result(r)

    if "broadcast" in run:
        r = await scenario_broadcast_latency(
            num_receivers=min(args.clients, MAX_SESSION_MEMBERS - 1),
            num_rounds=args.rounds,
        )
        results.append(r)
        print_result(r)

    if "concurrent_updates" in run:
        r = await scenario_concurrent_updates(
            concurrency=args.concurrency,
            rounds=args.rounds,
        )
        results.append(r)
        print_result(r)

    if "pool_exhaustion" in run:
        r = await scenario_pool_exhaustion()
        results.append(r)
        print_result(r)

    if "session_cap" in run:
        r = await scenario_session_cap()
        results.append(r)
        print_result(r)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  SUMMARY")
    print(f"{'═' * 60}")
    passed = sum(1 for r in results if r.passed)
    total  = len(results)
    for r in results:
        mark = "✅" if r.passed else "❌"
        print(f"  {mark}  {r.name}")
    print(f"\n  {passed}/{total} scenarios passed")

    if passed < total:
        raise SystemExit(1)


def main() -> None:
    global BASE, WS_BASE
    _default_base = BASE  # capture before any reassignment
    parser = argparse.ArgumentParser(description="Raabta.fm load test")
    parser.add_argument("--base", default=_default_base, help="Base HTTP URL")
    parser.add_argument("--sessions",    type=int, default=10,  help="Sessions to create")
    parser.add_argument("--clients",     type=int, default=5,   help="WS clients per session (max 10)")
    parser.add_argument("--duration",    type=int, default=15,  help="Hold time in seconds (persistence test)")
    parser.add_argument("--rounds",      type=int, default=10,  help="Rounds for broadcast/update tests")
    parser.add_argument("--concurrency", type=int, default=20,  help="Concurrent requests for update test")
    parser.add_argument(
        "--scenario", nargs="+",
        choices=list(SCENARIOS.keys()),
        help="Run specific scenarios only",
    )
    args = parser.parse_args()

    BASE    = args.base
    WS_BASE = args.base.replace("http://", "ws://").replace("https://", "wss://")

    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
