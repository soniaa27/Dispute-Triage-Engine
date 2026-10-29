"""The job executed for each accepted dispute.

Status transitions: queued -> running -> completed | escalated | failed.
Anything that goes wrong ends in `failed` with an error message: an incomplete run is
never stored as a confident answer.
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import lru_cache

import structlog
from sqlalchemy.orm import sessionmaker

from agent.runner import run_agent
from common.db import Run, make_engine, make_session_factory
from contracts import DisputeRequest, RunResult, RunStatus
from worker.settings import WorkerSettings

log = structlog.get_logger("worker.tasks")

_ACCEPTED_FINAL = {RunStatus.completed, RunStatus.escalated}


@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    return WorkerSettings()


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker:
    return make_session_factory(make_engine(get_settings().database_url))


def _set_status(run_id: str, status: RunStatus, *, result=None, error=None) -> None:
    with get_session_factory().begin() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        run.status = status
        run.result_json = result
        run.error = error


def run_with_timeout(req: DisputeRequest, run_id: str, timeout_s: float,
                     mode: str = "fake") -> RunResult:
    """Call run_agent under a wall-clock limit. Raises TimeoutError if it is exceeded."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"run-{run_id[:8]}")
    future = executor.submit(run_agent, req, run_id, mode=mode)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout as exc:
        raise TimeoutError(f"run exceeded RUN_TIMEOUT_SECONDS={timeout_s:g}") from exc
    finally:
        # Do not block on a stuck agent thread; the work-horse process exits after the job.
        executor.shutdown(wait=False, cancel_futures=True)


def mark_failed(run_id: str, error: str) -> None:
    try:
        _set_status(run_id, RunStatus.failed, error=error)
    except Exception:
        log.exception("mark_failed_error")


def process_run(run_id: str, correlation_id: str) -> str:
    with structlog.contextvars.bound_contextvars(run_id=run_id, correlation_id=correlation_id):
        return _process_run(run_id)


def _process_run(run_id: str) -> str:
    try:
        with get_session_factory()() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            request_json = run.request_json
        _set_status(run_id, RunStatus.running)
        log.info("run_started")

        req = DisputeRequest.model_validate(request_json)
        settings = get_settings()
        result = run_with_timeout(req, run_id, settings.run_timeout_seconds, settings.agent_mode)
        if result.run_id != run_id:
            raise ValueError("agent returned a result for a different run_id")
        if result.status not in _ACCEPTED_FINAL:
            raise ValueError(f"agent returned non-final status {result.status!r}")

        _set_status(run_id, result.status, result=result.model_dump(mode="json"))
        log.info("run_finished", status=result.status, route=result.route)
        return result.status
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        log.exception("run_failed", error=error)
        mark_failed(run_id, error)
        return RunStatus.failed
