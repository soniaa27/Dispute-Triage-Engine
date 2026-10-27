"""Compare two evaluation reports:  python -m eval.compare A.json B.json

Prints every metric side by side with the difference (B - A), then the scenarios whose
route correctness changed. Used for prompt variants (L2) and before/after changes (L3).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, str) and value.startswith("not computed"):
        return "not computed"
    return "n/a" if value is None else str(value)


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    lines = []
    label_a, label_b = a["meta"]["prompt_version"], b["meta"]["prompt_version"]
    if a["meta"].get("scenarios_sha256") != b["meta"].get("scenarios_sha256"):
        lines.append("WARNING: the reports used different scenario files; differences may "
                     "reflect the set, not the system.\n")
    elif a["meta"].get("scenario_ids") != b["meta"].get("scenario_ids"):
        lines.append("WARNING: the reports ran different scenarios (--ids/--limit); aggregate "
                     "metrics are not directly comparable.\n")
    width = max(len(k) for k in [*a["metrics"], *b["metrics"]])
    lines.append(f"{'metric':<{width}}  {label_a:>14}  {label_b:>14}  {'diff (B-A)':>11}")
    lines.append("-" * (width + 45))
    for name in dict.fromkeys([*a["metrics"], *b["metrics"]]):
        va, vb = a["metrics"].get(name), b["metrics"].get(name)
        if isinstance(va, dict) or isinstance(vb, dict):
            continue
        na, nb = _num(va), _num(vb)
        diff = f"{nb - na:+.3f}" if na is not None and nb is not None else ""
        lines.append(f"{name:<{width}}  {_cell(va)[:14]:>14}  {_cell(vb)[:14]:>14}  {diff:>11}")

    by_id_a = {s["id"]: s for s in a["scenarios"]}
    changed = [
        (s["id"], by_id_a[s["id"]], s)
        for s in b["scenarios"]
        if s["id"] in by_id_a and s["route_ok"] != by_id_a[s["id"]]["route_ok"]
    ]
    lines.append("")
    if changed:
        lines.append("Scenarios whose route correctness changed:")
        for sid, sa, sb in changed:
            arrow = "fixed" if sb["route_ok"] else "broken"
            lines.append(f"  {sid} {arrow}: {sa['actual_route']} -> {sb['actual_route']} "
                         f"(expected {sb['expected_route']})")
    else:
        lines.append("No scenario changed route correctness.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.compare", description=__doc__)
    parser.add_argument("report_a", type=Path)
    parser.add_argument("report_b", type=Path)
    args = parser.parse_args(argv)
    a, b = (json.loads(p.read_text()) for p in (args.report_a, args.report_b))
    print("\n".join(compare(a, b)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
