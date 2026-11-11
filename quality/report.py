"""Plain-text table of quality scores per day, for `python -m quality report`."""

from typing import Any

from sqlalchemy.orm import sessionmaker

from common.quality import summarize


def daily_rows(session_factory: sessionmaker, days: int) -> list[dict[str, Any]]:
    return summarize(session_factory, days=days, bucket="day")["buckets"]


def _fmt(value: Any) -> str:
    return "-" if value is None else (f"{value:.3f}" if isinstance(value, float) else str(value))


def format_rows(rows: list[dict[str, Any]]) -> str:
    columns = ["runs_scored", "groundedness", "context_relevance", "faithfulness",
               "hallucination_rate", "judge_errors", "feedback", "satisfactory_rate"]
    lines = ["day         " + " ".join(f"{c[:12]:>12}" for c in columns)]
    for row in rows:
        lines.append(f"{row['start'][:10]}  " + " ".join(f"{_fmt(row[c]):>12}" for c in columns))
    return "\n".join(lines)
