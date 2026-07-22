"""HTTP hardening middleware used by the public V1 API."""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.core.observability import metrics, request_scope, safe_request_id
from app.services.cache.redis import get_redis_client


logger = logging.getLogger(__name__)


class ReleaseHardeningMiddleware(BaseHTTPMiddleware):
    """Bound API control-plane requests and add traceable access telemetry."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = safe_request_id(request.headers.get("x-request-id")) or uuid4().hex
        request.state.request_id = request_id
        path = request.url.path
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > settings.MAX_API_REQUEST_BYTES:
            metrics.increment("raikou_http_rejected_total", {"reason": "body_too_large"})
            response = JSONResponse({"detail": "Request body exceeds the API limit."}, status_code=413)
            response.headers["X-Request-ID"] = request_id
            return response

        if path.startswith(settings.API_V1_STR) and not self._allow_request(request):
            metrics.increment("raikou_http_rejected_total", {"reason": "rate_limited"})
            response = JSONResponse({"detail": "Too many requests. Please retry shortly."}, status_code=429)
            response.headers.update({"Retry-After": "60", "X-Request-ID": request_id})
            return response

        started = time.perf_counter()
        status_code = 500
        with request_scope(request_id, method=request.method, path=path):
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers["X-Request-ID"] = request_id
                return response
            except Exception:
                metrics.increment("raikou_http_failures_total", {"path": _metric_path(path)})
                logger.exception("Unhandled API request failure")
                raise
            finally:
                elapsed = time.perf_counter() - started
                metrics.increment("raikou_http_requests_total", {"path": _metric_path(path), "status": str(status_code)})
                metrics.observe("raikou_http_request_duration_seconds", elapsed, {"path": _metric_path(path)})
                logger.info("HTTP request completed status=%s duration_ms=%d", status_code, round(elapsed * 1000))
        # The return in the try path is intentional; header decoration is below
        # via the response hook in FastAPI's outer middleware stack.

    @staticmethod
    def _client_key(request: Request) -> str:
        # Nginx overwrites X-Real-IP with $remote_addr; direct backend traffic
        # has no such header and is grouped by its socket peer.
        return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")

    def _allow_request(self, request: Request) -> bool:
        limit = (
            settings.UPLOAD_INITIATE_RATE_LIMIT_PER_MINUTE
            if request.url.path.endswith("/uploads/initiate")
            else settings.API_RATE_LIMIT_PER_MINUTE
        )
        bucket = int(time.time() // 60)
        key = f"{settings.REDIS_KEY_PREFIX}:ratelimit:{self._client_key(request)}:{bucket}"
        try:
            client = get_redis_client()
            with client.pipeline(transaction=True) as pipeline:
                pipeline.incr(key)
                pipeline.expire(key, 120)
                count, _ = pipeline.execute()
            return int(count) <= limit
        except Exception:
            # A readiness failure already reports Redis. Keep an outage from
            # converting every authenticated API operation into a 429.
            metrics.increment("raikou_rate_limit_errors_total")
            logger.warning("Rate-limit backend unavailable; allowing request")
            return True


class ResponseHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", getattr(request.state, "request_id", "generated"))
        response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api/") else "no-cache")
        return response


def _metric_path(path: str) -> str:
    """Keep labels low-cardinality; UUID-bearing paths must not become labels."""
    if path.startswith("/api/v1/uploads"):
        return "/api/v1/uploads"
    if path.startswith("/api/v1/jobs"):
        return "/api/v1/jobs"
    if path.startswith("/api/v1/scenes"):
        return "/api/v1/scenes"
    if path.startswith("/api/v1/projects"):
        return "/api/v1/projects"
    return path
