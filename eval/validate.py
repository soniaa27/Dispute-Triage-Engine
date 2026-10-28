"""Validate the evaluation set:  python -m eval.validate [--scenarios PATH]

Checks every scenario against the data contract, the working-day rule, the clause ID
conventions and the corpus text, then checks coverage. Exits non-zero on any failure.

When the chunk output exists (make ingest), also checks every supporting clause against it:
each must exist as a chunk, and the supporting_passage must be in the first one's text (for a
table row, whose chunk is relabelled by column, every number in the passage must be). Without
the chunk output these checks are skipped with a warning.
"""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from contracts import FraudType, Route
from eval.chunks import ChunkIndex, ChunksMissingError, load_chunk_index
from eval.corpus import (
    clause_id_valid,
    load_corpus_text,
    missing_terms,
    split_citation_id,
    text_in_document,
)
from eval.schema import DEFAULT_SCENARIOS, Category, Scenario, load_raw, reporting_delay

MIN_SCENARIOS = 20
# `unknown` is a fallback label, not a fraud pattern the set must cover.
REQUIRED_FRAUD_TYPES = [f for f in FraudType if f != FraudType.unknown]


@dataclass
class ValidationReport:
    errors: dict[str, list[str]] = field(default_factory=dict)
    coverage_errors: list[str] = field(default_factory=list)
    per_category: Counter = field(default_factory=Counter)
    per_route: Counter = field(default_factory=Counter)
    per_fraud_type: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    total: int = 0

    @property
    def ok(self) -> bool:
        return not self.coverage_errors and not any(self.errors.values())


def check_against_chunks(scenario: Scenario, chunks: ChunkIndex) -> list[str]:
    """The answer key against the retrievable text: what a correct citation can quote."""
    exp = scenario.expected
    errors = [f"supporting clause {cid} has no chunk (make ingest output)"
              for cid in exp.supporting_clauses if cid not in chunks]
    primary = exp.supporting_clauses[0]
    clause = chunks.get(primary)
    if clause is None or chunks.contains_span(primary, exp.supporting_passage):
        return errors
    if clause.kind == "table_row":
        missing = chunks.missing_key_values(primary, exp.supporting_passage)
        if missing:
            errors.append(f"supporting_passage values {missing} not in table row chunk {primary}")
    else:
        errors.append(f"supporting_passage not found in chunk {primary}")
    return errors


def check_scenario(scenario: Scenario, corpus, chunks: ChunkIndex | None = None) -> list[str]:
    """Content checks for one already-parsed scenario. Returns error messages."""
    errors = []
    exp = scenario.expected
    actual_delay = reporting_delay(scenario.request)
    if exp.working_days_delay != actual_delay:
        errors.append(
            f"working_days_delay is {exp.working_days_delay}, compute_working_days gives "
            f"{actual_delay}"
        )
    for cid in [*exp.supporting_clauses, *exp.must_not_cite]:
        if not clause_id_valid(cid):
            errors.append(f"clause id {cid!r} does not follow docs/interfaces.md conventions")
    try:
        primary_doc, _ = split_citation_id(exp.supporting_clauses[0])
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if not text_in_document(exp.supporting_passage, primary_doc, corpus):
            errors.append(
                f"supporting_passage not found verbatim in {primary_doc} "
                "(document of the first supporting clause)"
            )
    ungrounded = missing_terms(exp.supporting_passage, exp.basis_must_mention)
    if ungrounded:
        errors.append(f"basis_must_mention terms not in supporting_passage: {ungrounded}")
    overlap = set(exp.supporting_clauses) & set(exp.must_not_cite)
    if overlap:
        errors.append(f"clauses both supporting and must_not_cite: {sorted(overlap)}")
    if chunks is not None:
        errors += check_against_chunks(scenario, chunks)
    return errors


def validate(raw_scenarios: list[dict], corpus, chunks: ChunkIndex | None = None
             ) -> ValidationReport:
    """`chunks=None` skips the chunk checks (with a warning in the report)."""
    report = ValidationReport(total=len(raw_scenarios))
    if chunks is None:
        report.warnings.append("chunk output not found; chunk checks skipped (run make ingest)")
    seen: set[str] = set()
    for index, raw in enumerate(raw_scenarios, start=1):
        key = raw.get("id") or f"line {index}"
        try:
            scenario = Scenario.model_validate(raw)
        except ValidationError as exc:
            report.errors[key] = [
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            ]
            continue
        errors = check_scenario(scenario, corpus, chunks)
        if scenario.id in seen:
            errors.append("duplicate id")
        seen.add(scenario.id)
        report.errors[key] = errors
        report.per_category[scenario.category] += 1
        report.per_route[scenario.expected.route] += 1
        report.per_fraud_type[scenario.expected.fraud_type] += 1

    if report.total < MIN_SCENARIOS:
        report.coverage_errors.append(f"only {report.total} scenarios, need {MIN_SCENARIOS}")
    for label, required, counts in (
        ("route", list(Route), report.per_route),
        ("fraud type", REQUIRED_FRAUD_TYPES, report.per_fraud_type),
        ("category", list(Category), report.per_category),
    ):
        missing = [str(v) for v in required if counts[v] == 0]
        if missing:
            report.coverage_errors.append(f"no scenario for {label}: {', '.join(missing)}")
    return report


def print_report(report: ValidationReport) -> None:
    for key, errors in report.errors.items():
        status = "ok  " if not errors else "FAIL"
        print(f"[{status}] {key}")
        for err in errors:
            print(f"         - {err}")
    print(f"\n{report.total} scenarios")
    for title, counts in (
        ("category", report.per_category),
        ("route", report.per_route),
        ("fraud type", report.per_fraud_type),
    ):
        print(f"  per {title}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for err in report.coverage_errors:
        print(f"COVERAGE FAIL: {err}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    print("\nVALID" if report.ok else "\nINVALID")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.validate", description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    args = parser.parse_args(argv)
    try:
        chunks = load_chunk_index()
    except ChunksMissingError:
        chunks = None
    report = validate(load_raw(args.scenarios), load_corpus_text(), chunks)
    print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
