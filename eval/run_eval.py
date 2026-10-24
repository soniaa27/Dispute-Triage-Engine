"""Batch evaluation CLI:  python -m eval.run_eval [--scenarios eval/scenarios.jsonl]

TODO(Person A): run each scenario through the agent and report groundedness, context
relevance, faithfulness and hallucination rate (TruLens or equivalent), per prompt version.
For now it only validates that every scenario's request satisfies the data contract.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from contracts import DisputeRequest, Route

DEFAULT_SCENARIOS = Path(__file__).parent / "scenarios.jsonl"
REQUIRED_KEYS = {"id", "request", "expected", "supporting_passages"}


def load_scenarios(path: Path) -> list[dict]:
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.run_eval")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    problems = 0
    for sc in scenarios:
        missing = REQUIRED_KEYS - sc.keys()
        try:
            DisputeRequest.model_validate(sc.get("request", {}))
            Route(sc["expected"]["route"])
        except (ValidationError, KeyError, ValueError) as exc:
            missing.add(f"invalid: {exc}")
        if missing:
            problems += 1
            print(f"[FAIL] {sc.get('id', '?')}: {sorted(missing)}")
        else:
            print(f"[ok]   {sc['id']} -> expected {sc['expected']['route']}")
    print(f"{len(scenarios)} scenarios, {problems} invalid. Metrics: not implemented yet.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
