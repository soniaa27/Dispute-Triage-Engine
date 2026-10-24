from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "path"]
)
DISPUTES_ACCEPTED = Counter("disputes_accepted_total", "Dispute requests accepted and enqueued")
DISPUTES_QUARANTINED = Counter("disputes_quarantined_total", "Dispute requests quarantined")
