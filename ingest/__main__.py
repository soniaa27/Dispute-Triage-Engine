"""CLI:  python -m ingest build [--force]   parse and chunk corpus/raw/ into data/
       python -m ingest status            is data/ up to date? (exit 1 if not)

Outputs: data/chunks.jsonl (one Chunk per line) and data/manifest.json (built_at, source
SHA-256s, chunk counts, stage timings and throughput). See ingest/build.py.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from common.logging import configure_logging
from ingest.build import DATA_DIR, build, read_manifest, stale_reasons
from ingest.pdf import RAW_DIR


def _build(args: argparse.Namespace) -> int:
    manifest = build(args.raw, args.out, force=args.force)
    if not manifest["skipped"]:
        print(json.dumps({k: manifest[k] for k in ("chunks", "timings_s", "throughput")},
                         indent=2))
    return 0


def _status(args: argparse.Namespace) -> int:
    reasons = stale_reasons(args.out, args.raw)
    manifest = read_manifest(args.out)
    print(json.dumps({
        "fresh": not reasons,
        "reasons": reasons,
        "built_at": manifest and manifest["built_at"],
        "chunks": manifest and manifest["chunks"]["total"],
    }, indent=2))
    return 1 if reasons else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging("ingest", os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(prog="python -m ingest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", type=Path, default=RAW_DIR, help="source PDFs directory")
    parser.add_argument("--out", type=Path, default=DATA_DIR, help="output directory")
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="parse and chunk corpus/raw/ (skips if fresh)")
    build_cmd.add_argument("--force", action="store_true", help="rebuild even if fresh")
    build_cmd.set_defaults(func=_build)
    sub.add_parser("status", help="report whether data/ matches the sources and code") \
        .set_defaults(func=_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
