"""Sampling live runs and scoring them with the LLM judge.

Sampling
    A run is in the sample when the first 8 hex digits of sha256(run_id), as a fraction of
    2^32, are below QUALITY_SAMPLE_RATE. The decision depends only on the run id, so it is
    reproducible, needs no state, and a run never moves in or out of the sample between
    passes. Only finished runs with at least one citation are judged: without a citation
    there is nothing to ground (a "model unavailable" escalation, a blocked complaint).

Budget
    The judge makes 2 + (number of citations) model calls per run, through the same
    llm-gateway and the same rate limit as the agents (about 4 calls a minute on the Gemini
    free tier). QUALITY_MAX_PER_PASS (default 1) and QUALITY_INTERVAL_SECONDS (default 900)
    keep it to at most 4 runs an hour, so live disputes keep the quota. Judge calls appear in
    llm_calls under run id "judge-<run_id>" and steps judge_*, so their cost is reported.

Failures
    Model unavailable: the pass stops and writes nothing; the run is retried next pass.
    An unusable judge reply (not JSON): a row with the error is written, so one bad run is
    not retried forever, and it is excluded from the averages.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.db import QualityScore, Run
from contracts import DisputeRequest, RunResult, RunStatus
from contracts.llm import LLMClient, LLMUnavailableError
from eval.metrics import JUDGE_PROMPT_VERSION, LLMJudge

log = structlog.get_logger("quality")

SCORABLE = (RunStatus.completed, RunStatus.escalated)


class QualitySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    log_level: str = "INFO"
    # Fraction of finished runs to judge, 0 to 1.
    quality_sample_rate: float = Field(default=0.2, ge=0, le=1)
    quality_interval_seconds: float = Field(default=900, gt=0)
    quality_max_per_pass: int = Field(default=1, ge=1)
    # Runs older than this are never picked up (e.g. after the scorer was down a while).
    quality_lookback_hours: float = Field(default=24, gt=0)


def in_sample(run_id: str, rate: float) -> bool:
    bucket = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) / 2**32
    return bucket < rate


@dataclass
class PassReport:
    candidates: int = 0
    scored: int = 0
    errors: int = 0
    stopped: str | None = None  # why the pass stopped early, e.g. model unavailable


def _candidates(session, since: datetime, rate: float) -> list[Run]:
    scored = select(QualityScore.run_id)
    rows = session.scalars(
        select(Run)
        .where(Run.status.in_([s.value for s in SCORABLE]), Run.updated_at >= since,
               Run.result_json.is_not(None), Run.id.not_in(scored))
        .order_by(Run.updated_at.desc())
    ).all()
    return [r for r in rows
            if in_sample(r.id, rate) and (r.result_json or {}).get("citations")]


def _row(run: Run, result: RunResult, judge: LLMJudge, error: str | None = None,
         judged=None) -> QualityScore:
    row = QualityScore(
        run_id=run.id, run_status=str(run.status),
        route=str(result.route) if result.route else None,
        prompt_version=result.prompt_version, run_finished_at=run.updated_at,
        citations=len(result.citations), judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_model=", ".join(sorted(judge.models)) or None, error=error,
    )
    if judged is not None:
        row.claims, row.supported_claims = judged.claims, judged.supported
        row.unsupported_claims = judged.unsupported
        row.groundedness, row.hallucination_rate = judged.groundedness, \
            judged.hallucination_rate
        row.context_relevance = judged.context_relevance
        row.faithful = bool(judged.faithful)
    return row


def score_once(session_factory: sessionmaker, client: LLMClient, settings: QualitySettings,
               now: datetime | None = None) -> PassReport:
    """One pass: judge up to quality_max_per_pass sampled, unscored, finished runs."""
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=settings.quality_lookback_hours)
    report = PassReport()
    with session_factory() as session:
        runs = _candidates(session, since, settings.quality_sample_rate)
    report.candidates = len(runs)
    for run in runs[: settings.quality_max_per_pass]:
        judge = LLMJudge(client)
        result = RunResult.model_validate(run.result_json)
        try:
            request = DisputeRequest.model_validate(run.request_json)
            judged = judge.judge_run(request, result)
            row = _row(run, result, judge, judged=judged)
            report.scored += 1
        except LLMUnavailableError as exc:
            report.stopped = f"model unavailable: {exc}"
            log.warning("quality_pass_stopped", run_id=run.id, reason=report.stopped)
            break
        except Exception as exc:
            # A reply that is not the JSON asked for (ValueError), a field of the wrong type
            # (e.g. "relevance": "high" -> TypeError), a request the gateway rejected
            # (LLMGatewayError). Recorded once, or the run would be retried every pass.
            row = _row(run, result, judge, error=f"{type(exc).__name__}: {exc}"[:2000])
            report.errors += 1
            log.warning("quality_judge_failed", run_id=run.id, error=row.error)
        with session_factory.begin() as session:
            session.add(row)
        log.info("quality_scored", run_id=run.id, groundedness=row.groundedness,
                 faithful=row.faithful, error=row.error)
    return report
