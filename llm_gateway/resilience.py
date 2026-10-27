"""Circuit breaker, rate limiter and backoff for provider calls (requirement A5).

Breaker and limiter state live in Redis, not in process memory, so a gateway restart does not
reset an open breaker (it would otherwise hammer a failing provider straight away) and several
gateway replicas would share one breaker and one request budget. See docs/decisions.md D-012.
"""

import random
import time
from collections.abc import Callable
from enum import StrEnum

from redis.asyncio import Redis


class BreakerState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


# Value of the llm_circuit_state gauge.
STATE_GAUGE = {BreakerState.closed: 0, BreakerState.half_open: 1, BreakerState.open: 2}


class BreakerOpenError(Exception):
    def __init__(self, state: BreakerState):
        super().__init__(f"circuit breaker is {state}")
        self.state = state


class CircuitBreaker:
    """closed -> open after `failure_threshold` consecutive failures -> half_open once
    `reset_seconds` have passed (one trial call allowed) -> closed if the trial succeeds,
    open again if it fails. While open, before_call() raises without calling the provider.

    "Failure" means the provider was unavailable: timeout, 429, 5xx, connection error, auth.
    A bad request or a safety block is the request's fault and counts as a success here.
    """

    KEY = "llm:breaker"
    TRIAL_KEY = "llm:breaker:trial"

    def __init__(self, redis: Redis, *, failure_threshold: int, reset_seconds: float,
                 trial_ttl_seconds: float, clock: Callable[[], float] = time.time):
        self.redis = redis
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.trial_ttl_seconds = max(1, int(trial_ttl_seconds))
        self.clock = clock

    async def _get(self) -> dict[str, str]:
        raw = await self.redis.hgetall(self.KEY)
        return {k.decode() if isinstance(k, bytes) else k:
                v.decode() if isinstance(v, bytes) else v for k, v in raw.items()}

    async def state(self) -> BreakerState:
        return BreakerState((await self._get()).get("state", BreakerState.closed))

    async def snapshot(self) -> dict:
        data = await self._get()
        state = BreakerState(data.get("state", BreakerState.closed))
        opened_at = float(data["opened_at"]) if "opened_at" in data else None
        retry_in = None
        if state == BreakerState.open and opened_at is not None:
            retry_in = round(max(0.0, opened_at + self.reset_seconds - self.clock()), 1)
        return {
            "state": state,
            "consecutive_failures": int(data.get("consecutive_failures", 0)),
            "failure_threshold": self.failure_threshold,
            "reset_seconds": self.reset_seconds,
            "opened_at": opened_at,
            "trial_allowed_in_seconds": retry_in,
            "times_opened": int(data.get("times_opened", 0)),
            "fast_failures": int(data.get("fast_failures", 0)),
            "successes": int(data.get("successes", 0)),
            "failures": int(data.get("failures", 0)),
        }

    async def before_call(self) -> BreakerState:
        """Return the state the call runs under (closed, or half_open for the trial).
        Raises BreakerOpenError to fail fast."""
        data = await self._get()
        state = BreakerState(data.get("state", BreakerState.closed))
        if state == BreakerState.closed:
            return state
        if state == BreakerState.open:
            opened_at = float(data.get("opened_at", 0))
            if self.clock() - opened_at < self.reset_seconds:
                await self.redis.hincrby(self.KEY, "fast_failures", 1)
                raise BreakerOpenError(state)
        # Reset window elapsed (or a half-open trial expired): exactly one caller gets the trial.
        if await self.redis.set(self.TRIAL_KEY, "1", nx=True, ex=self.trial_ttl_seconds):
            await self.redis.hset(self.KEY, "state", BreakerState.half_open)
            return BreakerState.half_open
        await self.redis.hincrby(self.KEY, "fast_failures", 1)
        raise BreakerOpenError(BreakerState.half_open)

    async def release_trial(self) -> None:
        """Give the trial back without a result (the call never reached the provider)."""
        await self.redis.delete(self.TRIAL_KEY)

    async def record_success(self) -> BreakerState:
        pipe = self.redis.pipeline()
        pipe.hset(self.KEY, mapping={"state": BreakerState.closed, "consecutive_failures": 0})
        pipe.hincrby(self.KEY, "successes", 1)
        pipe.delete(self.TRIAL_KEY)
        await pipe.execute()
        return BreakerState.closed

    async def record_failure(self) -> BreakerState:
        pipe = self.redis.pipeline()
        pipe.hincrby(self.KEY, "consecutive_failures", 1)
        pipe.hincrby(self.KEY, "failures", 1)
        pipe.hget(self.KEY, "state")
        consecutive, _, raw_state = await pipe.execute()
        state = BreakerState((raw_state.decode() if isinstance(raw_state, bytes) else raw_state)
                             or BreakerState.closed)
        if state == BreakerState.half_open or (
            state == BreakerState.closed and consecutive >= self.failure_threshold
        ):
            pipe = self.redis.pipeline()
            pipe.hset(self.KEY, mapping={"state": BreakerState.open, "opened_at": self.clock()})
            pipe.hincrby(self.KEY, "times_opened", 1)
            pipe.delete(self.TRIAL_KEY)
            await pipe.execute()
            return BreakerState.open
        return state


# Token bucket, atomic in Redis. Returns "0" if a token was taken, else the seconds until one
# is available (as a string: Lua numbers are truncated to integers on return).
_TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1]) or capacity
local ts = tonumber(data[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], 300)
return tostring(wait)
"""


class RateLimiter:
    """Shared client-side limit on requests to the provider (LLM_MAX_REQUESTS_PER_MINUTE).

    Bucket capacity is 1, so requests are spaced 60/rpm seconds apart rather than bursting:
    a burst followed by a full refill could exceed a per-minute provider quota.
    """

    KEY = "llm:ratelimit"

    def __init__(self, redis: Redis, *, requests_per_minute: float,
                 clock: Callable[[], float] = time.time):
        self.redis = redis
        self.rate = requests_per_minute / 60.0
        self.clock = clock
        self._script = redis.register_script(_TOKEN_BUCKET)

    async def try_acquire(self) -> float:
        """0 if a request may go now, else seconds to wait before trying again."""
        wait = await self._script(keys=[self.KEY], args=[1, self.rate, self.clock()])
        return float(wait.decode() if isinstance(wait, bytes) else wait)


def backoff_delay(attempt: int, *, base: float, maximum: float,
                  retry_after: float | None = None,
                  rng: Callable[[float, float], float] = random.uniform) -> float:
    """Delay before retry number `attempt` (1-based): exponential, capped, with equal jitter
    (half fixed, half random) so concurrent callers spread out. A provider Retry-After hint
    is a floor."""
    ceiling = min(maximum, base * 2 ** (attempt - 1))
    delay = ceiling / 2 + rng(0, ceiling / 2)
    return max(delay, retry_after) if retry_after else delay
