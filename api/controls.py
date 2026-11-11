"""Per-client rate limiting and the duplicate-dispute cache, both in Redis (requirement D7).

Rate limiting
    A fixed one-minute window per client on POST endpoints (POST /disputes, feedback): INCR
    on "ratelimit:<client>:<window>", expiring with the window. Over the limit the API
    answers 429 with Retry-After, before the body is read, so a flood is neither validated
    nor quarantined nor enqueued. GET endpoints (status polling, health, metrics) are never
    limited. The client is the source IP. With RATE_LIMIT_TRUST_CLIENT_ID=true it is the
    X-Client-ID header when valid; that header is self-declared (a caller could rotate it),
    so it is off by default; behind a real gateway the id would come from the API key.
    If Redis is down the limiter lets the request through (fail open) and counts it: the
    enqueue fails anyway, and a broken limiter must not turn into an outage of its own.

Duplicate cache
    The same dispute submitted again (a retrying client, a double-clicked button) reuses
    the first run instead of starting a new one, which would cost the same model calls
    again. The key is a SHA-256 of the validated request in canonical JSON plus
    CACHE_NAMESPACE (set to the agent mode, so switching mode does not reuse runs).
    The first submission claims the key with SET NX, so two identical requests arriving
    together still start only one run.
    A cached run is not reused when it failed, or escalated because the model was
    unavailable or a limit was hit: those are transient, and the runbook tells analysts to
    resubmit them. The claim is then taken over by the new run.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass

import structlog
from redis import Redis
from redis.exceptions import RedisError
from starlette.requests import Request

from api.metrics import CACHE_REQUESTS, RATE_LIMIT_ERRORS, RATE_LIMITED
from contracts import DisputeRequest, RunStatus

log = structlog.get_logger("api.controls")

CLIENT_HEADER = "X-Client-ID"
_VALID_CLIENT = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
# Escalation reasons that mean "try again later", not "this is the answer" (agent/errors.py).
TRANSIENT_ESCALATIONS = ("model unavailable", "investigation limit reached")


def client_id(request: Request, trust_header: bool = False) -> str:
    """The rate-limit key: the source IP, or X-Client-ID when trust_header is set. The
    header is self-declared, so trusting it lets a caller pick a fresh id per request and
    escape the limit; only turn it on where a proxy sets it, or to tell apart simulated
    clients on one machine (load tests, scripts/measure_api_controls.py)."""
    declared = request.headers.get(CLIENT_HEADER, "")
    if trust_header and _VALID_CLIENT.match(declared):
        return f"id:{declared}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds until the window resets


class RateLimiter:
    def __init__(self, redis: Redis, per_minute: int, window_seconds: int = 60):
        self.redis = redis
        self.limit = per_minute
        self.window = window_seconds

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, client: str, now: float | None = None) -> RateDecision:
        now = time.time() if now is None else now
        window = int(now // self.window)
        retry_after = max(1, int((window + 1) * self.window - now))
        key = f"ratelimit:{client}:{window}"
        try:
            pipe = self.redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.window + 1)
            count = int(pipe.execute()[0])
        except RedisError as exc:
            RATE_LIMIT_ERRORS.inc()
            log.warning("rate_limiter_unavailable", error=type(exc).__name__)
            return RateDecision(True, self.limit, self.limit, retry_after)
        if count > self.limit:
            RATE_LIMITED.inc()
            return RateDecision(False, self.limit, 0, retry_after)
        return RateDecision(True, self.limit, self.limit - count, retry_after)


def dispute_key(dispute: DisputeRequest, namespace: str = "") -> str:
    canonical = json.dumps(dispute.model_dump(mode="json"), sort_keys=True,
                           separators=(",", ":"))
    digest = hashlib.sha256(f"{namespace}\n{canonical}".encode()).hexdigest()
    return f"dispute-cache:{digest}"


def reusable(status: str | None, result_json: dict | None) -> bool:
    """Whether a cached run may be returned for a new identical submission."""
    if status is None or status == RunStatus.failed:
        return False
    if status == RunStatus.escalated:
        reason = (result_json or {}).get("escalation_reason") or ""
        return not reason.startswith(TRANSIENT_ESCALATIONS)
    return True  # queued, running or completed: the run will give the same answer


class DisputeCache:
    def __init__(self, redis: Redis, ttl_seconds: int, namespace: str = ""):
        self.redis = redis
        self.ttl = ttl_seconds
        self.namespace = namespace

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def claim(self, key: str, run_id: str) -> tuple[bool, str | None]:
        """Claim the key for run_id: (True, None) if run_id now holds it, (False, other) if
        another run holds it, (False, None) if Redis is unavailable (no caching)."""
        try:
            if self.redis.set(key, run_id, nx=True, ex=self.ttl):
                return True, None
            existing = self.redis.get(key)
        except RedisError as exc:
            CACHE_REQUESTS.labels("error").inc()
            log.warning("cache_unavailable", error=type(exc).__name__)
            return False, None
        if existing is None:  # expired between SET and GET: take it
            return self.claim(key, run_id)
        return False, existing.decode() if isinstance(existing, bytes) else existing

    def take_over(self, key: str, run_id: str) -> None:
        try:
            self.redis.set(key, run_id, ex=self.ttl)
        except RedisError as exc:
            log.warning("cache_unavailable", error=type(exc).__name__)

    def release(self, key: str, run_id: str) -> None:
        """Drop the claim if run_id still holds it (e.g. its enqueue failed)."""
        try:
            held = self.redis.get(key)
            if held is not None and (held.decode() if isinstance(held, bytes) else held) \
                    == run_id:
                self.redis.delete(key)
        except RedisError:
            pass
