"""Batch evaluation:  python -m eval.run_eval [options]

Runs each scenario through agent.runner.run_agent (whatever implementation is current),
scores the results and writes eval/reports/<timestamp>_<label>.json and .md.

The four LLM-judged metrics need an LLMClient: pass --judge-client module:factory, where
factory() returns an object implementing contracts.llm.LLMClient. Without it they are
reported as not computed.
"""

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import runner
from contracts.llm import LLMClient
from eval.corpus import load_corpus_text
from eval.metrics import (
    ScenarioRun,
    basis_missing_terms,
    citation_ids,
    deterministic_metrics,
    judged_metrics,
)
from eval.schema import DEFAULT_SCENARIOS, Scenario, load_scenarios

DEFAULT_OUT = Path(__file__).parent / "reports"
DEFAULT_TIMEOUT_S = float(os.environ.get("RUN_TIMEOUT_SECONDS", "90"))


def run_one(scenario: Scenario, timeout_s: float) -> ScenarioRun:
    run_id = f"eval-{scenario.id}-{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(runner.run_agent, scenario.request, run_id)
    try:
        result = future.result(timeout=timeout_s)
        error = result.error
    except FutureTimeout:
        result, error = None, f"timed out after {timeout_s:g}s"
    except Exception as exc:  # the agent's failure is a data point, not a crash
        result, error = None, f"{exc.__class__.__name__}: {exc}"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return ScenarioRun(scenario=scenario, result=result, latency_ms=latency_ms, error=error)


def scenario_record(run: ScenarioRun) -> dict[str, Any]:
    exp, res = run.scenario.expected, run.result
    returned = set(citation_ids(res))
    return {
        "id": run.scenario.id,
        "title": run.scenario.title,
        "category": run.scenario.category,
        "expected_route": exp.route,
        "actual_route": res.route if res else None,
        "route_ok": res is not None and res.route == exp.route,
        "expected_fraud_type": exp.fraud_type,
        "actual_fraud_type": res.fraud_type if res else None,
        "must_escalate": exp.must_escalate,
        "status": res.status if res else "error",
        "supporting_clauses_found": sorted(set(exp.supporting_clauses) & returned),
        "supporting_clauses_missing": sorted(set(exp.supporting_clauses) - returned),
        "must_not_cite_hit": sorted(set(exp.must_not_cite) & returned),
        "basis_terms_missing": basis_missing_terms(run),
        "latency_ms": run.latency_ms,
        "error": run.error,
        "result": res.model_dump(mode="json") if res else None,
    }


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "unlabelled"


def write_reports(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = out_dir / f"{stamp}_{_slug(report['meta']['prompt_version'])}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    md_path.write_text(markdown_summary(report))
    return json_path, md_path


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return "n/a" if value is None else str(value)


def markdown_summary(report: dict[str, Any]) -> str:
    meta, metrics = report["meta"], report["metrics"]
    lines = [
        f"# Evaluation report: {meta['prompt_version']}",
        "",
        f"- Created: {meta['created_at']}",
        f"- Scenarios: {meta['scenario_count']} from `{meta['scenarios_file']}` "
        f"(sha256 {meta['scenarios_sha256'][:12]})",
        f"- Agent prompt versions seen: {', '.join(meta['agent_prompt_versions']) or 'none'}",
        f"- Mean latency: {_fmt(meta['latency_ms_mean'])} ms",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for name, value in metrics.items():
        if not isinstance(value, dict):
            lines.append(f"| {name} | {_fmt(value)} |")
    lines += [
        "",
        "## Per scenario",
        "",
        "| ID | Category | Expected | Actual | OK | Missing clauses | Error |",
        "|----|----------|----------|--------|----|-----------------|-------|",
    ]
    for s in report["scenarios"]:
        lines.append(
            f"| {s['id']} | {s['category']} | {s['expected_route']} | {_fmt(s['actual_route'])} "
            f"| {'yes' if s['route_ok'] else 'no'} | {', '.join(s['supporting_clauses_missing'])} "
            f"| {s['error'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def load_judge_client(spec: str | None) -> LLMClient | None:
    """'package.module:factory' -> factory(), which must return an LLMClient."""
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    client = getattr(importlib.import_module(module_name), attr)()
    if not isinstance(client, LLMClient):
        raise TypeError(f"{spec} did not return an LLMClient")
    return client


def run_eval(
    scenarios: list[Scenario],
    *,
    prompt_version: str,
    scenarios_file: Path,
    out_dir: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    judge_client: LLMClient | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    runs = []
    for scenario in scenarios:
        run = run_one(scenario, timeout_s)
        actual = run.result.route if run.result else run.error
        print(f"{scenario.id}: expected {scenario.expected.route}, got {actual} "
              f"({run.latency_ms:.0f} ms)")
        runs.append(run)

    corpus = load_corpus_text()
    metrics = {**deterministic_metrics(runs, corpus), **judged_metrics(runs, judge_client)}
    latencies = [r.latency_ms for r in runs]
    report = {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(),
            "prompt_version": prompt_version,
            "agent_prompt_versions": sorted({r.result.prompt_version for r in runs if r.result}),
            "scenarios_file": os.path.relpath(scenarios_file),
            "scenarios_sha256": hashlib.sha256(scenarios_file.read_bytes()).hexdigest(),
            "scenario_count": len(runs),
            "scenario_ids": [r.scenario.id for r in runs],
            "latency_ms_mean": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "judge": "configured" if judge_client else "none",
        },
        "metrics": metrics,
        "scenarios": [scenario_record(r) for r in runs],
    }
    json_path, md_path = write_reports(report, out_dir)
    return report, json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.run_eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--prompt-version", default="current",
                        help="label for this run, used in the report name (e.g. v1, v2)")
    parser.add_argument("--limit", type=int, help="run only the first N scenarios")
    parser.add_argument("--ids", help="comma-separated scenario ids, e.g. S01,S04")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="per-scenario wall-clock limit in seconds")
    parser.add_argument("--judge-client", help="module:factory returning an LLMClient")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",") if s.strip()}
        unknown = wanted - {s.id for s in scenarios}
        if unknown:
            parser.error(f"unknown scenario ids: {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s.id in wanted]
    if args.limit is not None:
        scenarios = scenarios[: args.limit]

    report, json_path, md_path = run_eval(
        scenarios,
        prompt_version=args.prompt_version,
        scenarios_file=args.scenarios,
        out_dir=args.out,
        timeout_s=args.timeout,
        judge_client=load_judge_client(args.judge_client),
    )
    print(f"\nroute_accuracy={_fmt(report['metrics']['route_accuracy'])} "
          f"citation_resolution_rate={_fmt(report['metrics']['citation_resolution_rate'])}")
    print(f"wrote {os.path.relpath(json_path)}\nwrote {os.path.relpath(md_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
