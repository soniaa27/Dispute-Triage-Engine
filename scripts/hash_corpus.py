"""Print filename and SHA-256 for every file in corpus/raw/.

Usage:  python scripts/hash_corpus.py [directory]
"""

import hashlib
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "corpus" / "raw"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    files = sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        print(f"no files in {directory}")
        return 0
    for path in files:
        print(f"{path.name}\t{sha256(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
