"""OpenTelemetry tracing shared by api, worker and llm-gateway (docs/observability.md).

setup_tracing(service) installs the process's TracerProvider. Spans are exported over
OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT (Jaeger in docker compose); without that variable
nothing is exported (unit tests, local tools), but spans and trace ids still exist.

One trace per run: the API's POST /disputes span is the root, its context travels in the RQ
job's meta (job_meta / context_from_meta), and the worker, the agents and llm-gateway add
child spans. Every span carries run_id, correlation_id and step: RunContextSpanProcessor
copies the first two from the structlog context each service already binds, and sets step
to the service name when the span's creator did not set one.
"""

import os
from collections.abc import Mapping
from typing import Any

import structlog
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

TRACER_NAME = "dispute-triage"
# Key of the trace context in an RQ job's meta.
JOB_META_KEY = "otel"
# Where people open Jaeger (printed by make trace); the services export to the OTLP endpoint.
DEFAULT_JAEGER_UI_URL = "http://localhost:16686"

_configured: dict[str, Any] = {}


class RunContextSpanProcessor(SpanProcessor):
    """Adds run_id, correlation_id and a default step to every span when it starts."""

    def __init__(self, service: str):
        self.service = service

    def on_start(self, span, parent_context=None) -> None:
        bound = structlog.contextvars.get_contextvars()
        for key in ("run_id", "correlation_id"):
            if bound.get(key) and not span.attributes.get(key):
                span.set_attribute(key, str(bound[key]))
        if "step" not in span.attributes:
            span.set_attribute("step", self.service)


def setup_tracing(service: str, *, exporter: SpanExporter | None = None) -> TracerProvider:
    """Install the global TracerProvider once per process; later calls return it unchanged.
    `exporter` is for tests (exported synchronously); otherwise OTLP/HTTP when
    OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    if "provider" in _configured:
        return _configured["provider"]
    provider = TracerProvider(resource=Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service)}))
    provider.add_span_processor(RunContextSpanProcessor(service))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured["provider"] = provider
    return provider


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def flush(timeout_ms: int = 5000) -> None:
    """Export pending spans now. The worker calls it at the end of each job: RQ's work horse
    exits with os._exit, which would drop spans still waiting in the batch."""
    provider = _configured.get("provider")
    if provider is not None:
        provider.force_flush(timeout_ms)


def set_attributes(span: trace.Span, **attributes: Any) -> None:
    """Set the attributes that have a value; enums and other objects become strings."""
    for key, value in attributes.items():
        if value is None:
            continue
        if not isinstance(value, bool | int | float | str):
            value = [str(v) for v in value] if isinstance(value, list | tuple) else str(value)
        span.set_attribute(key, value)


def job_meta() -> dict[str, dict[str, str]]:
    """RQ job meta carrying the current trace context (W3C traceparent)."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return {JOB_META_KEY: carrier}


def context_from_meta(meta: Mapping[str, Any] | None) -> otel_context.Context | None:
    carrier = (meta or {}).get(JOB_META_KEY)
    return extract(carrier) if carrier else None


def trace_id(span: trace.Span | None = None) -> str | None:
    """The 32-hex-digit trace id of `span` (default: the current span), or None."""
    ctx = (span or trace.get_current_span()).get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def jaeger_url(trace_id_hex: str, base: str | None = None) -> str:
    base = base or os.environ.get("JAEGER_UI_URL", DEFAULT_JAEGER_UI_URL)
    return f"{base.rstrip('/')}/trace/{trace_id_hex}"
