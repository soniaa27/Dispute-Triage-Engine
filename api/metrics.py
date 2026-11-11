from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "path"]
)
DISPUTES_ACCEPTED = Counter("disputes_accepted_total", "Dispute requests accepted and enqueued")
DISPUTES_QUARANTINED = Counter("disputes_quarantined_total", "Dispute requests quarantined")

# Guardrails (requirement L4): what the input checks found in accepted disputes. The worker
# enforces them (agent.runner.run_agent); the API counts them for the dashboard.
GUARDRAIL_INPUT_FLAGS = Counter(
    "guardrail_input_flags_total",
    "Dispute submissions (cache hits included) flagged by an input guardrail", ["kind"],
)

# Caching and rate limiting (requirement D7, api/controls.py).
CACHE_REQUESTS = Counter(
    "dispute_cache_requests_total",
    "Duplicate-dispute cache lookups: hit (existing run reused), miss, stale (cached run "
    "failed or escalated transiently, rerun), error (Redis unavailable)",
    ["result"],
)
RATE_LIMITED = Counter("rate_limited_requests_total", "Requests refused with 429")
RATE_LIMIT_ERRORS = Counter(
    "rate_limiter_errors_total", "Rate-limit checks skipped because Redis was unavailable"
)
