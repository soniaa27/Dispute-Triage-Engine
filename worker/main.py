"""Worker entry point:  python -m worker.main

Scale with `docker compose up --scale worker=N`; each container registers a unique
RQ worker name prefixed with its hostname (used by the healthcheck).
"""

import os
import socket

import structlog
from redis import Redis
from rq import Queue, Worker

from common.logging import configure_logging
from worker.tasks import get_settings, mark_failed

log = structlog.get_logger("worker")


def _on_job_exception(job, exc_type, exc_value, _tb) -> bool:
    """Catches failures outside process_run's own handling, e.g. RQ's hard job timeout."""
    run_id = job.args[0] if job.args else job.id
    mark_failed(run_id, f"{exc_type.__name__}: {exc_value}")
    return True  # continue to RQ's default handler (moves job to the failed registry)


class ContextWorker(Worker):
    """Binds run_id / correlation_id so RQ's own job log lines carry them too."""

    def execute_job(self, job, queue):
        args = job.args or ()
        structlog.contextvars.bind_contextvars(
            run_id=job.id, correlation_id=args[1] if len(args) > 1 else None
        )
        try:
            return super().execute_job(job, queue)
        finally:
            structlog.contextvars.clear_contextvars()


def worker_name() -> str:
    return f"{socket.gethostname()}.{os.getpid()}"


def main() -> None:
    settings = get_settings()
    configure_logging("worker", settings.log_level)
    if settings.agent_mode in ("investigator", "graph"):
        # RQ runs each job in a forked child: load the retriever (index + embedding model,
        # ~5 s) and the LLM client here once, so every job inherits them.
        from agent.runner import warm_up

        warm_up()
        log.info("agent_warmed_up", agent_mode=settings.agent_mode)
    redis = Redis.from_url(settings.redis_url)
    queue = Queue(settings.queue_name, connection=redis)
    worker = ContextWorker(
        [queue],
        connection=redis,
        name=worker_name(),
        exception_handlers=[_on_job_exception],
    )
    log.info("worker_starting", name=worker.name, queue=settings.queue_name)
    worker.work(logging_level=settings.log_level.upper())


if __name__ == "__main__":
    main()
