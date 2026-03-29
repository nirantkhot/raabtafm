import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
import structlog
from fastapi import WebSocket, WebSocketDisconnect

import app.session.state as state
from app.config import settings
from app.core.logging import E, timer
from app.core.rate_limiter import check_rate_limit

logger = structlog.get_logger(__name__)


class ConnectionManager:
    def __init__(self):
        # session_id -> {client_id -> WebSocket}
        self.active: dict[str, dict[str, WebSocket]] = {}
        # session_id -> {client_id -> monotonic connect time}
        self.connect_times: dict[str, dict[str, float]] = {}
        # session_id -> {client_id -> last ping sent time}
        self.ping_timestamps: dict[str, dict[str, float]] = {}
        # session_id -> {client_id} — clients that have responded to latest ping
        self.pong_received: dict[str, set[str]] = {}

    async def connect(
        self,
        session_id: str,
        client_id: str,
        display_name: str,
        websocket: WebSocket,
        redis: aioredis.Redis,
        db,
    ) -> bool:
        client_ip = websocket.client.host if websocket.client else "unknown"

        # ── Rate limit check ─────────────────────────────────────────────────
        try:
            await check_rate_limit(redis, "ws_connection", client_ip)
        except Exception:
            logger.warning(
                E.WS_REJECTED,
                session_id=session_id,
                client_id=client_id,
                client_ip=client_ip,
                reason="rate_limit",
            )
            await websocket.accept()
            await websocket.close(code=1008, reason="Rate limit exceeded")
            return False

        # ── Member cap check ─────────────────────────────────────────────────
        members = await state.get_members(session_id, redis)
        if len(members) >= settings.max_session_members:
            logger.warning(
                E.WS_REJECTED,
                session_id=session_id,
                client_id=client_id,
                client_ip=client_ip,
                reason="session_full",
                member_count=len(members),
                max_members=settings.max_session_members,
            )
            await websocket.accept()
            await websocket.close(code=1008, reason="Session is full")
            return False

        await websocket.accept()

        # Register connection
        self.active.setdefault(session_id, {})[client_id] = websocket
        self.ping_timestamps.setdefault(session_id, {})
        self.connect_times.setdefault(session_id, {})[client_id] = time.monotonic()

        await state.add_member(session_id, client_id, display_name, redis)

        logger.info(
            E.WS_CONNECTED,
            session_id=session_id,
            client_id=client_id,
            display_name=display_name,
            client_ip=client_ip,
            member_count=len(members) + 1,
        )
        return True

    async def disconnect(
        self, session_id: str, client_id: str, redis: aioredis.Redis
    ) -> None:
        # Calculate how long this client was connected
        connect_time = self.connect_times.get(session_id, {}).pop(client_id, None)
        duration_ms  = round((time.monotonic() - connect_time) * 1000) if connect_time else None

        if session_id in self.active:
            self.active[session_id].pop(client_id, None)
            if not self.active[session_id]:
                del self.active[session_id]

        if session_id in self.ping_timestamps:
            self.ping_timestamps[session_id].pop(client_id, None)
        if session_id in self.pong_received:
            self.pong_received[session_id].discard(client_id)

        await state.remove_member(session_id, client_id, redis)

        logger.info(
            E.WS_DISCONNECTED,
            session_id=session_id,
            client_id=client_id,
            duration_ms=duration_ms,
        )

    async def broadcast(self, session_id: str, message: dict) -> None:
        if session_id not in self.active:
            return

        dead: list[str] = []
        event_type = message.get("event", "unknown")

        with timer() as t:
            for client_id, ws in list(self.active[session_id].items()):
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.append(client_id)

        recipient_count = len(self.active[session_id]) - len(dead)
        logger.info(
            E.WS_BROADCAST,
            session_id=session_id,
            event_type=event_type,
            recipient_count=recipient_count,
            dead_count=len(dead),
            latency_ms=t.ms,
        )

        for client_id in dead:
            logger.warning(
                E.WS_DEAD_CONN,
                session_id=session_id,
                client_id=client_id,
            )
            self.active[session_id].pop(client_id, None)

    async def send_to_one(
        self, session_id: str, client_id: str, message: dict
    ) -> None:
        ws = self.active.get(session_id, {}).get(client_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception:
                logger.warning(
                    E.WS_DEAD_CONN,
                    session_id=session_id,
                    client_id=client_id,
                )
                self.active[session_id].pop(client_id, None)

    async def send_ping(self, session_id: str, client_id: str) -> None:
        now = time.monotonic()
        self.ping_timestamps.setdefault(session_id, {})[client_id] = now

        logger.debug(
            E.WS_HEARTBEAT_SENT,
            session_id=session_id,
            client_id=client_id,
        )
        await self.send_to_one(
            session_id,
            client_id,
            {"event": "ping", "timestamp": datetime.now(timezone.utc).isoformat()},
        )

    async def handle_pong(
        self, session_id: str, client_id: str, redis: aioredis.Redis
    ) -> None:
        sent_time = self.ping_timestamps.get(session_id, {}).get(client_id)
        if sent_time is None:
            return

        rtt_ms = int((time.monotonic() - sent_time) * 1000)
        await state.record_rtt(session_id, client_id, rtt_ms, redis)

        # Signal to start_heartbeat that pong was received for this ping cycle
        self.pong_received.setdefault(session_id, set()).add(client_id)

        logger.info(
            E.WS_HEARTBEAT_PONG,
            session_id=session_id,
            client_id=client_id,
            rtt_ms=rtt_ms,
        )

        await self.send_to_one(
            session_id,
            client_id,
            {"event": "rtt_update", "rtt_ms": rtt_ms},
        )

    async def start_heartbeat(
        self,
        session_id: str,
        client_id: str,
        websocket: WebSocket,
        redis: aioredis.Redis,
    ) -> None:
        while True:
            await asyncio.sleep(30)

            # Bail early if client already disconnected
            if client_id not in self.active.get(session_id, {}):
                return

            # Clear any previous pong receipt before sending the new ping
            self.pong_received.setdefault(session_id, set()).discard(client_id)
            await self.send_ping(session_id, client_id)

            # Wait up to 10s for pong (handle_pong adds client to pong_received)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if client_id not in self.active.get(session_id, {}):
                    return  # Connection already cleaned up
                if client_id in self.pong_received.get(session_id, set()):
                    break   # Pong received — continue heartbeat loop

            if client_id not in self.pong_received.get(session_id, set()):
                logger.warning(
                    E.WS_HEARTBEAT_TIMEOUT,
                    session_id=session_id,
                    client_id=client_id,
                )
                try:
                    await websocket.close(code=1001, reason="Heartbeat timeout")
                except Exception:
                    pass
                return


manager = ConnectionManager()
