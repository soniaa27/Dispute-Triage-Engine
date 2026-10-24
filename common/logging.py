"""Structured JSON logging for every service.

Each line carries service, correlation_id and run_id. Bind the last two with
`structlog.contextvars.bind_contextvars(...)`; stdlib loggers (uvicorn, rq) are
rendered through the same JSON pipeline.
"""

import logging
import sys

import structlog

_DEFAULT_CONTEXT = {"correlation_id": None, "run_id": None}


def _add_default_context(_logger, _method, event_dict):
    for key, value in _DEFAULT_CONTEXT.items():
        event_dict.setdefault(key, value)
    return event_dict


def _add_service(service: str):
    def processor(_logger, _method, event_dict):
        event_dict["service"] = service
        return event_dict

    return processor


def configure_logging(service: str, level: str = "INFO") -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_default_context,
        _add_service(service),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "rq.worker"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Access logs are replaced by the API's own request log line.
    logging.getLogger("uvicorn.access").disabled = True
