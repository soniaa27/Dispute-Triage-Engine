"""Quality-over-time aggregation (requirement L6), shared by GET /quality and
`python -m quality report`. Reads quality_scores (written by quality/) and feedback.

Per time bucket (hour or day, UTC, by when the run finished), with the same definitions as
the offline evaluation (eval/metrics.py):
  groundedness       = sum(supported claims) / sum(claims)
  hallucination_rate = sum(unsupported claims) / sum(claims)
  context_relevance  = mean of the per-run mean passage relevance
  faithfulness       = share of runs whose route and position follow from the passages
plus the analysts' feedback in the same bucket (by when it was given):
  satisfactory_rate  = share of feedback marked satisfactory
Rows with a judge error are counted in `judge_errors` and left out of the averages.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.db import Feedback, QualityScore

Bucket = Literal["hour", "day"]


def _utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything is stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _floor(value: datetime, bucket: Bucket) -> datetime:
    value = _utc(value)
    if bucket == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return value.replace(minute=0, second=0, microsecond=0)


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _aggregate(rows: list[QualityScore]) -> dict[str, Any]:
    ok = [r for r in rows if r.error is None]
    claims = sum(r.claims for r in ok)
    return {
        "runs_scored": len(ok),
        "judge_errors": len(rows) - len(ok),
        "groundedness": _ratio(sum(r.supported_claims for r in ok), claims),
        "hallucination_rate": _ratio(sum(r.unsupported_claims for r in ok), claims),
        "context_relevance": _mean([r.context_relevance for r in ok
                                    if r.context_relevance is not None]),
        "faithfulness": _mean([float(r.faithful) for r in ok if r.faithful is not None]),
    }


def summarize(session_factory: sessionmaker, days: int = 7, bucket: Bucket = "day",
              now: datetime | None = None) -> dict[str, Any]:
    now = _utc(now or datetime.now(UTC))
    since = now - timedelta(days=days)
    with session_factory() as session:
        scores = session.scalars(select(QualityScore).where(
            QualityScore.run_finished_at >= since)).all()
        feedback = session.scalars(select(Feedback).where(Feedback.created_at >= since)).all()

    by_bucket: dict[datetime, list[QualityScore]] = defaultdict(list)
    for row in scores:
        by_bucket[_floor(row.run_finished_at, bucket)].append(row)
    fb_bucket: dict[datetime, list[Feedback]] = defaultdict(list)
    for row in feedback:
        fb_bucket[_floor(row.created_at, bucket)].append(row)

    step = timedelta(days=1) if bucket == "day" else timedelta(hours=1)
    start, buckets = _floor(since, bucket), []
    while start <= now:
        fb = fb_bucket.get(start, [])
        buckets.append({
            "start": start.isoformat(),
            **_aggregate(by_bucket.get(start, [])),
            "feedback": len(fb),
            "satisfactory_rate": _ratio(sum(f.satisfactory for f in fb), len(fb)),
        })
        start += step

    by_version: dict[str, list[QualityScore]] = defaultdict(list)
    for row in scores:
        by_version[row.prompt_version].append(row)
    recent = sorted(scores, key=lambda r: _utc(r.run_finished_at), reverse=True)[:20]
    return {
        "bucket": bucket,
        "days": days,
        "since": since.isoformat(),
        "overall": {**_aggregate(list(scores)), "feedback": len(feedback),
                    "satisfactory_rate": _ratio(sum(f.satisfactory for f in feedback),
                                                len(feedback))},
        "by_prompt_version": {v: _aggregate(rows) for v, rows in sorted(by_version.items())},
        "buckets": buckets,
        "recent": [
            {"run_id": r.run_id, "run_finished_at": _utc(r.run_finished_at).isoformat(),
             "status": r.run_status, "route": r.route, "prompt_version": r.prompt_version,
             "groundedness": r.groundedness, "context_relevance": r.context_relevance,
             "faithful": r.faithful, "hallucination_rate": r.hallucination_rate,
             "judge_model": r.judge_model, "error": r.error}
            for r in recent
        ],
    }
