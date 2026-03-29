"""
HTTP request logging middleware
================================
Emits one ``http.request`` log event per HTTP request containing:
  method, path, status_code, latency_ms, client_id (if available).

/health is downgraded to DEBUG to avoid log spam in production.
WebSocket upgrade requests are skipped (they produce ws.* events instead).
"""

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.client_identity import CLIENT_ID_COOKIE, CLIENT_ID_HEADER
from app.core.logging import E

logger = structlog.get_logger(__name__)

_HEALTH_PATH = "/health"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip WebSocket upgrade — those produce ws.* events
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        client_id = (
            request.headers.get(CLIENT_ID_HEADER)
            or request.cookies.get(CLIENT_ID_COOKIE)
            or "unknown"
        )
        client_ip = request.client.host if request.client else "unknown"

        t0 = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            logger.error(
                E.HTTP_REQUEST,
                method=request.method,
                path=request.url.path,
                status_code=500,
                latency_ms=latency_ms,
                client_id=client_id,
                client_ip=client_ip,
                error=str(exc),
            )
            raise

        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        status = response.status_code
        path   = request.url.path

        log_fn = logger.debug if path == _HEALTH_PATH else logger.info

        log_fn(
            E.HTTP_REQUEST,
            method=request.method,
            path=path,
            status_code=status,
            latency_ms=latency_ms,
            client_id=client_id,
            client_ip=client_ip,
        )

        return response
