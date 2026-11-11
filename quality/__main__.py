"""python -m quality {run,once,report}. See quality/__init__.py."""

import argparse
import sys
import time

import structlog

from common.db import make_engine, make_session_factory
from common.logging import configure_logging
from quality.report import daily_rows, format_rows
from quality.scorer import QualitySettings, score_once

log = structlog.get_logger("quality")


def _client():
    from agent.llm_client import HttpLLMClient

    return HttpLLMClient()  # LLM_GATEWAY_URL from the environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quality")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="score a sample every QUALITY_INTERVAL_SECONDS, forever")
    sub.add_parser("once", help="one scoring pass")
    report = sub.add_parser("report", help="scores per day")
    report.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)

    settings = QualitySettings()
    configure_logging("quality", settings.log_level)
    session_factory = make_session_factory(make_engine(settings.database_url))

    if args.command == "report":
        print(format_rows(daily_rows(session_factory, args.days)))
        return 0
    client = _client()
    if args.command == "once":
        print(score_once(session_factory, client, settings))
        return 0
    log.info("quality_scorer_started", sample_rate=settings.quality_sample_rate,
             interval_s=settings.quality_interval_seconds,
             max_per_pass=settings.quality_max_per_pass)
    while True:
        try:
            outcome = score_once(session_factory, client, settings)
            log.info("quality_pass_done", candidates=outcome.candidates, scored=outcome.scored,
                     errors=outcome.errors, stopped=outcome.stopped)
        except Exception:  # keep the loop alive (e.g. Postgres restarting)
            log.exception("quality_pass_failed")
        time.sleep(settings.quality_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
