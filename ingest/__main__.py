"""CLI:  python -m ingest build

TODO(Person A): idempotent pipeline over corpus/raw/ writing the index to data/index/,
recording freshness (built_at, source SHA-256s, chunk count) and throughput as metrics.
"""

import argparse
import sys


def build(_args: argparse.Namespace) -> int:
    print("ingest build: not implemented")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="parse, chunk, embed and index corpus/raw/").set_defaults(
        func=build
    )
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
