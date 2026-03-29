"""
Structured logging for Raabta.fm
=================================
Every log event is a flat dict that can be shipped to any sink
(stdout → Loki / Datadog / CloudWatch) and turned into metrics later.

Field conventions
-----------------
event       dot-separated name   e.g.  "session.created"
session_id  UUID string          always bind when in session context
client_id   string               always bind when known
latency_ms  float (2 dp)         present on every timed operation
error       str(exception)       present on error / warning events
"""

import logging
import time

import structlog
from structlog.types import EventDict, WrappedLogger

from app.config import settings


# ── Timer ─────────────────────────────────────────────────────────────────────

class _Timer:
    """
    Synchronous context manager for timing any block (sync or async).

    Usage::

        with timer() as t:
            result = await some_operation()
        logger.info("event.name", latency_ms=t.ms)
    """
    __slots__ = ("_start", "ms")

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        self.ms: float = 0.0
        return self

    def __exit__(self, *_: object) -> None:
        self.ms = round((time.perf_counter() - self._start) * 1000, 2)


def timer() -> _Timer:
    """Return a new :class:`_Timer` ready to use as a ``with`` block."""
    return _Timer()


# ── Event name catalogue ──────────────────────────────────────────────────────
# Using constants means grep / IDE finds every emission site instantly.

class E:
    """Canonical event names — import and use instead of bare strings."""

    # App lifecycle
    APP_STARTUP       = "app.startup"
    APP_STARTUP_READY = "app.startup_ready"
    APP_SHUTDOWN      = "app.shutdown"

    # Infrastructure / DB
    DB_PG_POOL_CREATED = "db.postgres.pool_created"
    DB_PG_MIGRATION    = "db.postgres.migration_applied"
    DB_REDIS_CREATED   = "db.redis.client_created"

    # Session REST
    SESSION_CREATED    = "session.created"
    SESSION_CODE_RETRY = "session.code_collision_retry"
    SESSION_JOINED     = "session.joined"
    SESSION_FULL       = "session.rejected_full"       # REST 403

    # Playback
    PLAYBACK_UPDATED  = "playback.updated"
    PLAYBACK_CONFLICT = "playback.optimistic_lock_retry"
    PLAYBACK_FAILED   = "playback.optimistic_lock_failed"

    # Redis state mutations
    STATE_MEMBER_ADDED   = "state.member_added"
    STATE_MEMBER_REMOVED = "state.member_removed"
    STATE_RTT_RECORDED   = "state.rtt_recorded"

    # WebSocket lifecycle
    WS_CONNECTED    = "ws.connected"
    WS_REJECTED     = "ws.rejected"
    WS_DISCONNECTED = "ws.disconnected"

    # WebSocket traffic
    WS_MSG_RECEIVED  = "ws.message_received"
    WS_INVALID_JSON  = "ws.invalid_json"
    WS_UNKNOWN_EVENT = "ws.unknown_event"
    WS_BROADCAST     = "ws.broadcast_sent"
    WS_DEAD_CONN     = "ws.dead_connection_removed"

    # Heartbeat
    WS_HEARTBEAT_SENT    = "ws.heartbeat_sent"
    WS_HEARTBEAT_PONG    = "ws.heartbeat_pong_received"
    WS_HEARTBEAT_TIMEOUT = "ws.heartbeat_timeout"

    # Rate limiting
    RATE_ALLOWED  = "rate_limit.allowed"   # emitted at DEBUG — high volume
    RATE_REJECTED = "rate_limit.rejected"
    RATE_ERROR    = "rate_limit.error"

    # Health
    HEALTH_CHECK      = "health.check"
    HEALTH_ERROR_REDIS = "health.redis_error"
    HEALTH_ERROR_PG    = "health.postgres_error"

    # HTTP request middleware
    HTTP_REQUEST = "http.request"

    # Queue / vibe search
    VIBE_EXTRACTED       = "vibe.extracted"
    VIBE_EXTRACTION_ERROR = "vibe.extraction_error"
    VIBE_EMBED_DONE      = "vibe.embed_done"
    VIBE_SEARCH_FALLBACK = "vibe.search_fallback"
    QUEUE_VIBE_ADDED     = "queue.vibe_added"
    QUEUE_ITEM_REMOVED   = "queue.item_removed"

    # Circuit breaker
    CB_FAILURE = "circuit_breaker.failure"
    CB_OPENED  = "circuit_breaker.opened"
    CB_OPEN    = "circuit_breaker.call_rejected"
    CB_CLOSED  = "circuit_breaker.closed"


# ── Internal processors ───────────────────────────────────────────────────────

def _drop_color_message(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Remove uvicorn's color-markup 'color_message' key from log records."""
    event_dict.pop("color_message", None)
    return event_dict


# ── Public configuration ──────────────────────────────────────────────────────

def configure_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _drop_color_message,
    ]

    if settings.app_env == "production":
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", level=log_level)

    # Quiet noisy third-party loggers
    for noisy in ("uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for *name*."""
    return structlog.get_logger(name)
