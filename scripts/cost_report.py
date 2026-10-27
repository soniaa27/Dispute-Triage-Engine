"""Cost and token report from llm_calls (requirement L5).

  python scripts/cost_report.py [--database-url URL] [--since-hours N] [--top N]
  make llm-cost-report        (runs inside the llm-gateway container, which can reach Postgres)

Prints totals per step and per run, cost and tokens per 100 runs, and the largest single cost
driver: the step with the highest total cost (by tokens when every price is 0, e.g. on a
free tier).
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from common.db import LlmCall, make_engine, make_session_factory  # noqa: E402

_TOKENS = LlmCall.prompt_tokens + LlmCall.completion_tokens


def _aggregate(session, group, since):
    stmt = select(
        group,
        func.count(),
        func.sum(LlmCall.prompt_tokens),
        func.sum(LlmCall.completion_tokens),
        func.sum(LlmCall.cost_usd),
        func.avg(LlmCall.latency_ms),
    ).group_by(group)
    if since is not None:
        stmt = stmt.where(LlmCall.created_at >= since)
    return [
        {"key": key, "calls": calls, "prompt_tokens": int(p or 0),
         "completion_tokens": int(c or 0), "cost_usd": float(cost or 0),
         "mean_latency_ms": float(lat or 0)}
        for key, calls, p, c, cost, lat in session.execute(stmt)
    ]


def build_report(database_url: str, since_hours: float | None = None) -> dict:
    since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
    with make_session_factory(make_engine(database_url))() as session:
        by_step = _aggregate(session, LlmCall.step, since)
        by_run = _aggregate(session, LlmCall.run_id, since)
        by_outcome = _aggregate(session, LlmCall.outcome, since)
    runs = len(by_run)
    total_cost = sum(r["cost_usd"] for r in by_step)
    total_tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in by_step)
    driver = None
    if by_step:
        by_cost = total_cost > 0
        top = max(by_step, key=lambda r: r["cost_usd"] if by_cost
                  else r["prompt_tokens"] + r["completion_tokens"])
        driver = {"step": top["key"], "measure": "cost_usd" if by_cost else "tokens",
                  "value": top["cost_usd"] if by_cost
                  else top["prompt_tokens"] + top["completion_tokens"]}
    return {
        "runs": runs,
        "calls": sum(r["calls"] for r in by_step),
        "total_cost_usd": total_cost,
        "total_tokens": total_tokens,
        "cost_per_100_runs_usd": total_cost / runs * 100 if runs else None,
        "tokens_per_100_runs": total_tokens / runs * 100 if runs else None,
        "largest_cost_driver": driver,
        "by_step": sorted(by_step, key=lambda r: -r["cost_usd"]),
        "by_run": sorted(by_run, key=lambda r: (-r["cost_usd"], -r["prompt_tokens"])),
        "by_outcome": by_outcome,
    }


def _table(rows: list[dict], label: str, top: int | None = None) -> list[str]:
    lines = [f"{label:<40} {'calls':>6} {'prompt_tok':>11} {'compl_tok':>10} "
             f"{'cost_usd':>12} {'mean_ms':>9}"]
    for r in rows[:top] if top else rows:
        lines.append(f"{str(r['key'])[:40]:<40} {r['calls']:>6} {r['prompt_tokens']:>11} "
                     f"{r['completion_tokens']:>10} {r['cost_usd']:>12.6f} "
                     f"{r['mean_latency_ms']:>9.0f}")
    return lines


def format_report(report: dict, top: int = 10) -> str:
    if not report["calls"]:
        return "No rows in llm_calls."
    per100 = report["cost_per_100_runs_usd"]
    tok100 = report["tokens_per_100_runs"]
    driver = report["largest_cost_driver"]
    lines = [
        f"Runs: {report['runs']}   calls: {report['calls']}   "
        f"tokens: {report['total_tokens']}   cost: ${report['total_cost_usd']:.6f}",
        f"Per 100 runs: ${per100:.4f}, {tok100:.0f} tokens",
        f"Largest cost driver: step '{driver['step']}' ({driver['measure']} = "
        f"{driver['value']:.6g})",
        "",
        "Per step",
        *_table(report["by_step"], "step"),
        "",
        f"Per run (top {top} by cost)",
        *_table(report["by_run"], "run_id", top),
        "",
        "Per outcome",
        *_table(report["by_outcome"], "outcome"),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--since-hours", type=float)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("set DATABASE_URL or pass --database-url")
    print(format_report(build_report(args.database_url, args.since_hours), args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
