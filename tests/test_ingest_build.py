import json

import pytest

from eval.corpus import corpus_available
from ingest import __main__ as cli
from ingest.build import (
    CHUNKS_FILE,
    MANIFEST_FILE,
    build,
    load_chunks,
    recorded_hashes,
    stale_reasons,
)
from ingest.documents import chunk_corpus
from ingest.pdf import RAW_DIR, load_corpus

pytestmark = pytest.mark.skipif(
    not corpus_available(), reason="corpus PDFs not present in corpus/raw/"
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    return out, build(out_dir=out)


def test_build_writes_chunks_and_manifest(built):
    out, manifest = built
    assert not manifest["skipped"]
    assert (out / CHUNKS_FILE).is_file() and (out / MANIFEST_FILE).is_file()
    saved = json.loads((out / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert saved["chunks"]["total"] == len(load_chunks(out)) > 0
    assert {s["pages"] for s in saved["sources"].values()} == {9, 16, 24}
    assert all(s["matches_provenance"] for s in saved["sources"].values())
    assert saved["throughput"]["pages_per_s"] > 0


def test_chunks_round_trip(built):
    out, _ = built
    assert load_chunks(out) == chunk_corpus(load_corpus())


def test_second_build_is_skipped(built):
    out, manifest = built
    mtime = (out / CHUNKS_FILE).stat().st_mtime_ns
    again = build(out_dir=out)
    assert again["skipped"] and again["built_at"] == manifest["built_at"]
    assert (out / CHUNKS_FILE).stat().st_mtime_ns == mtime
    assert stale_reasons(out) == []


def test_changed_source_or_code_is_stale(built, tmp_path):
    out, _ = built
    for name in (CHUNKS_FILE, MANIFEST_FILE):
        (tmp_path / name).write_bytes((out / name).read_bytes())
    manifest = json.loads((tmp_path / MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest["sources"]["IDBI_POLICY"]["sha256"] = "0" * 64
    manifest["pipeline_sha256"] = "old"
    (tmp_path / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    assert stale_reasons(tmp_path) == ["ingest code or pypdf version changed",
                                       "IDBI_POLICY source changed"]
    assert not build(out_dir=tmp_path)["skipped"]
    assert stale_reasons(tmp_path) == []


def test_missing_output_is_stale(tmp_path):
    assert stale_reasons(tmp_path) == ["no manifest"]


def test_provenance_mismatch_is_recorded(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("| IDBI_POLICY | x.pdf | 2026-10-24 | " + "a" * 64 + " | y |\n",
                      encoding="utf-8")
    assert recorded_hashes(readme) == {"IDBI_POLICY": "a" * 64}
    manifest = build(out_dir=tmp_path / "out", readme=readme)
    assert manifest["sources"]["IDBI_POLICY"]["matches_provenance"] is False
    assert manifest["sources"]["RBI_2017_CIRCULAR"]["matches_provenance"] is None


def test_readme_provenance_table_is_parsed():
    assert set(recorded_hashes()) == {"RBI_2017_CIRCULAR", "IDBI_POLICY", "RBI_IOS_2026"}


def test_cli_status_exit_codes(built, tmp_path, capsys):
    out, _ = built
    assert cli.main(["--out", str(out), "status"]) == 0
    assert '"fresh": true' in capsys.readouterr().out
    assert cli.main(["--out", str(tmp_path), "--raw", str(RAW_DIR), "status"]) == 1
    assert '"no manifest"' in capsys.readouterr().out
