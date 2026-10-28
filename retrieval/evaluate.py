"""Retrieval metrics over the evaluation scenarios: does the retriever find the clauses each
scenario's answer rests on (expected.supporting_clauses)?

    python -m retrieval.evaluate [--k 1 3 5 10] [--modes hybrid dense keyword]

Queries come from a query source (QUERY_SOURCES). "complaint" is the scenario's
complaint_text as the customer wrote it: the floor, since it never states the account
type or the reporting delay that pick the right table row. The Investigator's query plan
is meant to be added as a further source. A source may give several queries per
scenario; their rankings are merged with the retriever's RRF. Each mode (hybrid, dense
only, keyword only) is scored separately.

Metrics, for each k (a retrieved "5#1" counts as clause "5"):
  recall@k          = #(supporting clauses in the top k) / #(supporting clauses)   (micro)
  hit@k             = #(scenarios with at least one supporting clause in the top k) / N
  primary_recall@k  = #(scenarios whose first (primary) supporting clause is in the top k) / N
  must_not_cite@k   = #(must_not_cite clauses in the top k), summed over scenarios
and once per mode:
  mrr               = mean over scenarios of 1 / rank of the first supporting clause
                      (0 if none in the top RANK_DEPTH)
  latency_ms        = p50 and p95 of one search call

Writes eval/reports/<timestamp>_retrieval-<source>.json (per-scenario ranks included) and .md.
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.agent import CITATION_SEPARATOR
from eval.schema import DEFAULT_SCENARIOS, Scenario, load_scenarios
from ingest.build import DATA_DIR, read_manifest
from ingest.chunks import PART_SEPARATOR
from retrieval.hybrid import MODES, RRF_K, HybridRetriever, Mode

DEFAULT_KS = (1, 3, 5, 10)
RANK_DEPTH = 50
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "eval" / "reports"

QuerySource = Callable[[Scenario], list[str]]
QUERY_SOURCES: dict[str, QuerySource] = {
    "complaint": lambda scenario: [scenario.request.complaint_text],
}


def _base(citation_id: str) -> str:
    doc, clause = citation_id.split(CITATION_SEPARATOR, 1)
    return f"{doc}{CITATION_SEPARATOR}{clause.split(PART_SEPARATOR, 1)[0]}"


def ranks(retriever: HybridRetriever, queries: list[str], mode: Mode) -> tuple[list, float]:
    """Retrieved base ids (best first, split parts merged) and seconds per search call."""
    fused: dict[str, float] = defaultdict(float)
    started = time.perf_counter()
    for query in queries:
        seen: set[str] = set()
        for passage in retriever.search(query, k=RANK_DEPTH, mode=mode):
            if (base := _base(passage.citation_id)) not in seen:
                seen.add(base)
                fused[base] += 1 / (RRF_K + len(seen))
    elapsed = (time.perf_counter() - started) / max(len(queries), 1)
    return sorted(fused, key=lambda base: -fused[base]), elapsed


def _rank(retrieved: list[str], clause: str) -> int | None:
    return retrieved.index(clause) + 1 if clause in retrieved else None


def score_mode(retriever: HybridRetriever, scenarios: list[Scenario], mode: Mode,
               ks: tuple[int, ...], queries: QuerySource) -> dict[str, Any]:
    per_scenario, latencies = [], []
    for scenario in scenarios:
        retrieved, elapsed = ranks(retriever, queries(scenario), mode)
        latencies.append(elapsed * 1000)
        per_scenario.append({
            "id": scenario.id,
            "supporting": {c: _rank(retrieved, c) for c in scenario.expected.supporting_clauses},
            "must_not_cite": {c: _rank(retrieved, c) for c in scenario.expected.must_not_cite},
            "top5": retrieved[:5],
        })

    n = len(per_scenario)
    total = sum(len(s["supporting"]) for s in per_scenario)
    metrics: dict[str, Any] = {}
    for k in ks:
        def within(rank: int | None, k: int = k) -> bool:
            return rank is not None and rank <= k

        metrics[f"recall@{k}"] = round(sum(
            within(r) for s in per_scenario for r in s["supporting"].values()) / total, 3)
        metrics[f"hit@{k}"] = round(sum(
            any(within(r) for r in s["supporting"].values()) for s in per_scenario) / n, 3)
        metrics[f"primary_recall@{k}"] = round(sum(
            within(next(iter(s["supporting"].values()))) for s in per_scenario) / n, 3)
        metrics[f"must_not_cite@{k}"] = sum(
            within(r) for s in per_scenario for r in s["must_not_cite"].values())
    firsts = [min((r for r in s["supporting"].values() if r), default=None)
              for s in per_scenario]
    metrics["mrr"] = round(sum(1 / r for r in firsts if r) / n, 3)
    latencies.sort()
    metrics["latency_ms"] = {"p50": round(statistics.median(latencies), 1),
                             "p95": round(latencies[int(0.95 * (n - 1))], 1)}
    return {"metrics": metrics, "scenarios": per_scenario}


def evaluate(retriever: HybridRetriever, scenarios: list[Scenario],
             modes: tuple[Mode, ...] = MODES, ks: tuple[int, ...] = DEFAULT_KS,
             model: str | None = None, source: str = "complaint") -> dict[str, Any]:
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "scenario_count": len(scenarios),
            "supporting_clause_count": sum(len(s.expected.supporting_clauses)
                                           for s in scenarios),
            "query_source": source,
            "embedding_model": model,
            "ks": list(ks),
        },
        "modes": {mode: score_mode(retriever, scenarios, mode, ks, QUERY_SOURCES[source])
                  for mode in modes},
    }


def markdown_summary(report: dict[str, Any]) -> str:
    meta, ks = report["meta"], report["meta"]["ks"]
    columns = ([f"recall@{k}" for k in ks] + [f"primary_recall@{k}" for k in ks]
               + ["mrr", f"must_not_cite@{ks[-1]}", "p50 ms"])
    lines = [
        "# Retrieval report",
        "",
        f"- Created: {meta['created_at']}",
        f"- Scenarios: {meta['scenario_count']} ({meta['supporting_clause_count']} "
        f"supporting clauses); queries: `{meta['query_source']}`",
        f"- Embedding model: {meta['embedding_model']}",
        "",
        "| mode | " + " | ".join(columns) + " |",
        "|---" * (len(columns) + 1) + "|",
    ]
    for mode, result in report["modes"].items():
        m = result["metrics"]
        values = [m[c] for c in columns[:-1]] + [m["latency_ms"]["p50"]]
        lines.append(f"| {mode} | " + " | ".join(str(v) for v in values) + " |")
    if "hybrid" in report["modes"]:
        lines += ["", f"Missed at k={ks[-1]} (hybrid):"]
    for s in report["modes"].get("hybrid", {}).get("scenarios", []):
        missed = [c for c, r in s["supporting"].items() if r is None or r > ks[-1]]
        if missed:
            lines.append(f"- {s['id']}: {', '.join(missed)}")
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    base = out_dir / f"{stamp}_retrieval-{report['meta']['query_source']}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_summary(report), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m retrieval.evaluate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=DATA_DIR, help="built corpus (ingest)")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_KS))
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--queries", choices=sorted(QUERY_SOURCES), default="complaint")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args(argv)

    manifest = read_manifest(args.data)
    retriever = HybridRetriever.load(args.data)
    report = evaluate(retriever, load_scenarios(args.scenarios), tuple(args.modes),
                      tuple(sorted(args.k)), manifest["index"]["model"] if manifest else None,
                      args.queries)
    sys.stdout.write(markdown_summary(report))
    if not args.no_write:
        json_path, md_path = write_reports(report, args.out)
        sys.stdout.write(f"\nwrote {json_path}\n      {md_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
