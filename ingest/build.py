"""Build step: corpus/raw/*.pdf -> data/chunks.jsonl + data/index/ + data/manifest.json.

Idempotent: a build is skipped when the manifest records the same source SHA-256s, the
same pipeline hash (the ingest code and pypdf version) and the same embedding model, and
the chunks and index files are present. The manifest is the freshness record: when it was
built, from which bytes, how many chunks, which model, and how long each stage took.
"""

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pypdf
import structlog

from contracts.agent import DocId
from ingest.chunks import Chunk
from ingest.documents import chunk_corpus
from ingest.embed import Embedder, FastEmbedder, embed_model_name
from ingest.index import IDS_FILE, VECTORS_FILE, build_index
from ingest.pdf import DOC_FILES, RAW_DIR, load_corpus

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CHUNKS_FILE = "chunks.jsonl"
INDEX_DIR = "index"
MANIFEST_FILE = "manifest.json"
MANIFEST_VERSION = 2
PROVENANCE_README = ROOT / "corpus" / "README.md"

log = structlog.get_logger("ingest")

_PROVENANCE_ROW = re.compile(r"^\|\s*([A-Z0-9_]+)\s*\|[^|]*\|[^|]*\|\s*([0-9a-f]{64})\s*\|", re.M)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pipeline_hash() -> str:
    """Changes whenever the ingest code or the PDF library changes, forcing a rebuild."""
    digest = hashlib.sha256(f"pypdf=={pypdf.__version__}\n".encode())
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def recorded_hashes(readme: Path = PROVENANCE_README) -> dict[str, str]:
    """doc_id -> SHA-256 from the Provenance table in corpus/README.md ({} if absent)."""
    if not readme.is_file():
        return {}
    return dict(_PROVENANCE_ROW.findall(readme.read_text(encoding="utf-8")))


def source_hashes(raw_dir: Path) -> dict[str, str]:
    return {doc_id.value: sha256(raw_dir / name) for doc_id, name in DOC_FILES.items()}


def read_manifest(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / MANIFEST_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def stale_reasons(out_dir: Path, raw_dir: Path = RAW_DIR,
                  model_name: str | None = None) -> list[str]:
    """Why the built output does not match the current sources, code and embedding model
    (`model_name`, default EMBED_MODEL); [] if fresh."""
    manifest = read_manifest(out_dir)
    if manifest is None:
        return ["no manifest"]
    reasons = []
    for path in (out_dir / CHUNKS_FILE, out_dir / INDEX_DIR / VECTORS_FILE,
                 out_dir / INDEX_DIR / IDS_FILE):
        if not path.is_file():
            reasons.append(f"{path.relative_to(out_dir).as_posix()} missing")
    if manifest.get("index", {}).get("model") != (model_name or embed_model_name()):
        reasons.append("embedding model changed")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        reasons.append("manifest version changed")
    if manifest.get("pipeline_sha256") != pipeline_hash():
        reasons.append("ingest code or pypdf version changed")
    built = {doc: src["sha256"] for doc, src in manifest.get("sources", {}).items()}
    for doc, digest in source_hashes(raw_dir).items():
        if built.get(doc) != digest:
            reasons.append(f"{doc} source changed")
    return reasons


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_chunks(out_dir: Path = DATA_DIR) -> list[Chunk]:
    with (out_dir / CHUNKS_FILE).open(encoding="utf-8") as fh:
        return [Chunk.model_validate_json(line) for line in fh if line.strip()]


def build(raw_dir: Path = RAW_DIR, out_dir: Path = DATA_DIR, *, force: bool = False,
          readme: Path = PROVENANCE_README, embedder: Embedder | None = None) -> dict[str, Any]:
    """Run the pipeline unless the output is fresh. Returns the manifest in effect.

    `embedder` defaults to FastEmbedder(EMBED_MODEL), loaded only if a build is needed.
    """
    model_name = embedder.model_name if embedder else embed_model_name()
    if not force and not (reasons := stale_reasons(out_dir, raw_dir, model_name)):
        manifest = read_manifest(out_dir)
        log.info("ingest_up_to_date", built_at=manifest["built_at"],
                 chunks=manifest["chunks"]["total"])
        return {**manifest, "skipped": True}
    if not force:
        log.info("ingest_rebuilding", reasons=reasons)

    started = time.perf_counter()
    hashes = source_hashes(raw_dir)
    recorded = recorded_hashes(readme)
    for doc, digest in hashes.items():
        if doc in recorded and recorded[doc] != digest:
            log.warning("ingest_source_differs_from_provenance", doc_id=doc,
                        sha256=digest, recorded=recorded[doc])
    corpus = load_corpus(raw_dir)
    extracted = time.perf_counter()
    chunks = chunk_corpus(corpus)
    chunked = time.perf_counter()

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(out_dir / CHUNKS_FILE,
                  "".join(chunk.model_dump_json() + "\n" for chunk in chunks))
    index = build_index(chunks, embedder or FastEmbedder(model_name), out_dir / INDEX_DIR)
    indexed = time.perf_counter()
    pages = sum(len(p) for p in corpus.values())
    total_s = time.perf_counter() - started
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pipeline_sha256": pipeline_hash(),
        "sources": {
            doc_id.value: {
                "file": DOC_FILES[doc_id],
                "sha256": hashes[doc_id.value],
                "pages": len(corpus[doc_id]),
                "matches_provenance": (recorded[doc_id.value] == hashes[doc_id.value]
                                       if doc_id.value in recorded else None),
            }
            for doc_id in DocId
        },
        "chunks": {
            "total": len(chunks),
            "by_doc": {d.value: sum(c.doc_id == d for c in chunks) for d in DocId},
            "by_kind": {k: sum(c.kind == k for c in chunks)
                        for k in ("clause", "note", "table", "table_row")},
        },
        "index": index,
        "timings_s": {
            "extract": round(extracted - started, 3),
            "chunk": round(chunked - extracted, 3),
            "index": round(indexed - chunked, 3),
            "total": round(total_s, 3),
        },
        "throughput": {
            "pages_per_s": round(pages / total_s, 2),
            "chunks_per_s": round(len(chunks) / total_s, 2),
        },
    }
    _write_atomic(out_dir / MANIFEST_FILE, json.dumps(manifest, indent=2) + "\n")
    log.info("ingest_built", chunks=len(chunks), pages=pages, total_s=manifest["timings_s"]
             ["total"], out_dir=str(out_dir))
    return {**manifest, "skipped": False}
