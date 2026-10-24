"""Correlation ID propagation, request logging and HTTP metrics."""

import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from api.metrics import HTTP_LATENCY, HTTP_REQUESTS

CORRELATION_HEADER = "X-Correlation-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

log = structlog.get_logger("api.request")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(CORRELATION_HEADER, "")
        correlation_id = incoming if _VALID_ID.match(incoming) else str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id, run_id=None)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            # Use the route template, not the raw path, to keep label cardinality bounded.
            path = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            HTTP_LATENCY.labels(request.method, path).observe(elapsed)
            if path != "/metrics":
                log.info(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    duration_ms=round(elapsed * 1000, 1),
                    # Endpoint-bound contextvars do not propagate back here; use request.state.
                    run_id=getattr(request.state, "run_id", None),
                )
