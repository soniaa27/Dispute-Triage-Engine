# Incident log

Real failures met during development and operation. One row per incident; newest last.

| Date | Symptom | Root cause | Fix | Prevention |
|------|---------|------------|-----|------------|
| 2026-10-24 | Development halted: shell commands and Docker builds failed with `ENOSPC`. | Host disk full (Docker Desktop image/cache plus local files). | Freed ~25 GB on the host; resumed. | Check `docker system df` before builds; prune unused images/build cache regularly. |
| 2026-10-24 | Worker JSON logs had `run_id: null` on RQ's "Successfully completed" / "Job OK" lines. | `process_run` called `unbind_contextvars` in `finally`, removing the IDs the worker had bound around the job. | Use `structlog.contextvars.bound_contextvars` (restores the previous values) and bind IDs in a `Worker.execute_job` override. | Smoke check: `docker compose logs worker \| grep <run_id>` shows every job line. |
