import re
from dataclasses import replace

import pytest

from contracts.agent import DocId
from eval.corpus import clause_id_valid, corpus_available
from eval.schema import load_scenarios
from ingest.chunks import Chunk, split_long
from ingest.documents import IDBI_TABLES, LAYOUTS, chunk_corpus
from ingest.pdf import drop_glyphs, load_corpus, squash_whitespace, verbatim_key
from ingest.segment import DocLayout, Level, Line, is_noise, segment
from ingest.tables import Row, VerificationError, verify

needs_corpus = pytest.mark.skipif(
    not corpus_available(), reason="corpus PDFs not present in corpus/raw/"
)

# ---------- unit: segmentation rules ----------

_LETTERED = DocLayout(
    doc_id=DocId.RBI_IOS_2026,
    levels=[
        Level(re.compile(r"(?P<n>\d{1,2})\.\s+(?P<rest>.*)"), "number", lambda _, n: n,
              titled=True),
        Level(re.compile(r"\((?P<n>\d{1,2})\)\s*(?P<rest>.*)"), "number",
              lambda p, n: f"{p}({n})", parent=0),
        Level(re.compile(r"\((?P<n>[a-z])\)\s*(?P<rest>.*)"), "letter",
              lambda p, n: f"{p}({n})", parent=1),
        Level(re.compile(r"\((?P<n>i|ii|iii)\)\s*(?P<rest>.*)"), "roman",
              lambda p, n: f"{p}({n})", parent=2, emit=False),
    ],
)


def _segment(*texts: str) -> dict[str, str]:
    lines = [Line(1, t) for t in texts]
    return {c.clause_id: c.text for c in segment(lines, _LETTERED)}


def test_numbers_must_follow_previous_sibling():
    chunks = _segment("1. Definitions", "(1) In the Scheme:", "registration under sub-section",
                      "(5) of section 5 of the Act;", "(2) Words not defined.",
                      "3. Not clause 2, so text of 1(2).")
    assert list(chunks) == ["1(1)", "1(2)"]
    assert chunks["1(1)"] == "In the Scheme: registration under sub-section (5) of section 5 " \
                             "of the Act;"
    assert chunks["1(2)"].endswith("3. Not clause 2, so text of 1(2).")


def test_i_is_a_letter_after_h_and_a_roman_numeral_otherwise():
    letters = [f"({c}) item {c};" for c in "abcdefgh"]
    chunks = _segment("1. Definitions", "(1) Terms:", *letters, "(i) item i;")
    assert "1(1)(i)" in chunks
    chunks = _segment("1. Scope", "(1) Entities:", "(a) banks;", "(b) NBFCs which",
                      "(i) accept deposits; or", "(ii) have assets.", "(c) issuers.")
    assert list(chunks) == ["1(1)", "1(1)(a)", "1(1)(b)", "1(1)(c)"]
    assert chunks["1(1)(b)"] == "NBFCs which (i) accept deposits; or (ii) have assets."


def test_heading_only_clause_is_not_a_chunk_and_wrapped_heading_is_joined():
    chunks = segment([Line(1, "1. Staffing of the Offices and"), Line(1, "Processing Centre"),
                      Line(1, "The Reserve Bank shall staff them."),
                      Line(2, "2. Powers"), Line(2, "(1) The Ombudsman may.")], _LETTERED)
    assert [(c.clause_id, c.section) for c in chunks] == [
        ("1", "1. Staffing of the Offices and Processing Centre"), ("2(1)", "2. Powers")]
    assert chunks[1].parent == "2" and chunks[1].page == 2


def test_split_long_marks_parts():
    chunk = Chunk(doc_id=DocId.IDBI_POLICY, clause_id="9.1", text="One. Two. Three.", page=10,
                  kind="clause")
    parts = split_long(chunk, max_chars=6)
    assert [p.clause_id for p in parts] == ["9.1#1", "9.1#2", "9.1#3"]
    assert {p.base_clause_id for p in parts} == {"9.1"}


# ---------- corpus ----------

@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def chunks(corpus) -> dict[str, Chunk]:
    return {c.citation_id: c for c in chunk_corpus(corpus)}


def _clause(chunks: dict[str, Chunk], citation_id: str) -> str:
    """Text of a clause, joining "#n" parts."""
    return " ".join(c.text for cid, c in chunks.items()
                    if cid == citation_id or cid.startswith(f"{citation_id}#"))


@needs_corpus
def test_chunks_are_unique_and_follow_clause_id_conventions(corpus, chunks):
    assert len(chunks) == len(chunk_corpus(corpus))
    assert [cid for cid in chunks if not clause_id_valid(cid)] == []


@needs_corpus
def test_every_clause_named_in_interfaces_and_scenarios_exists(chunks):
    documented = [
        "RBI_2017_CIRCULAR:" + c for c in ("6", "6(ii)", "7(i)", "7(ii)", "Table-1",
                                           "7(ii)-beyond-7", "Table-2", "Table-2-note",
                                           "Annex-4")
    ] + [
        "IDBI_POLICY:" + c for c in ("5.12", "5.13", "5.20", "5.22", "Table-I", "Table-I-note",
                                     "Table-II", "3.1(i)", "8.1(v)",
                                     *(f"Table-I.{n:02}" for n in range(1, 11)),
                                     *(f"Table-II.{n:02}" for n in range(1, 4)))
    ] + [
        "RBI_IOS_2026:" + c for c in ("8(3)", "10(1)(g)", "10(2)", "3(1)(f)", "Annex-A.2")
    ]
    referenced = {c for s in load_scenarios()
                  for c in (*s.expected.supporting_clauses, *s.expected.must_not_cite)}
    bases = {cid.split("#", 1)[0] for cid in chunks}
    assert [c for c in sorted(set(documented) | referenced) if c not in bases] == []


@needs_corpus
def test_scenario_passages_sit_in_their_primary_clause(chunks):
    # Table rows are excluded: their chunks label each cell with its column name and resolve
    # merged cells, so they do not reproduce the PDF's row text in reading order.
    for scenario in load_scenarios():
        primary = scenario.expected.supporting_clauses[0]
        if chunks.get(primary) and chunks[primary].kind == "table_row":
            continue
        assert verbatim_key(scenario.expected.supporting_passage) in \
            verbatim_key(_clause(chunks, primary)), scenario.id


@needs_corpus
def test_notes_easily_lost_are_their_own_chunks(chunks):
    note = chunks["RBI_2017_CIRCULAR:Table-2-note"]
    assert note.kind == "note" and note.page == 5
    assert note.text.endswith("excluding the date of receiving the communication.")
    note = chunks["IDBI_POLICY:Table-I-note"]
    assert note.kind == "note" and note.page == 8
    assert "beyond 60 calendar days" in note.text
    assert "Further, if the delay" not in chunks["RBI_2017_CIRCULAR:7(ii)"].text


@needs_corpus
def test_table_rows_keep_band_and_merged_cells(chunks):
    rows = {n: chunks[f"IDBI_POLICY:Table-I.{n:02}"].text for n in range(1, 11)}
    assert "Within 3 working days" in rows[1]
    for n in range(2, 10):
        assert "between 4 and 7 working days" in rows[n]
    assert "Beyond 7 working days to 60 calendar days" in rows[10]
    for n in range(3, 8):  # one merged liability cell
        assert "limited to Rs. 10,000/-" in rows[n]
    for n in (8, 9):
        assert "restricted to Rs. 25,000/-" in rows[n]
    assert "Rs. 2,500/-" in rows[10] and "Shadow Credit to be given: No" in rows[10]
    assert "*Shadow credit shall be restricted" in rows[3]
    assert "*Shadow credit" not in rows[1]
    table = chunks["IDBI_POLICY:Table-I"]
    assert table.kind == "table" and all(f"\n{n:02} | " in table.text for n in range(1, 11))


@needs_corpus
def test_definitions_are_linked_not_merged(chunks):
    row = chunks["IDBI_POLICY:Table-I.03"]
    assert set(row.related) >= {"IDBI_POLICY:5.13", "IDBI_POLICY:5.20"}
    assert "Number of Days" not in row.text
    assert chunks["IDBI_POLICY:5.13"].text.startswith("Number of Days")


@needs_corpus
def test_pages_are_pdf_indexes(chunks):
    # IDBI prints page numbers 2 lower than the PDF index.
    assert chunks["IDBI_POLICY:5.13"].page == 6
    assert chunks["IDBI_POLICY:Table-II.03"].page == 9
    assert chunks["RBI_2017_CIRCULAR:Table-1"].page == 4
    assert chunks["RBI_IOS_2026:10(1)(g)"].page == 7


@needs_corpus
def test_prose_chunks_are_verbatim(corpus, chunks):
    # Against the plain text eval/corpus.py uses, minus page furniture (a clause running over
    # a page break has the running header in between in the PDF).
    for doc_id, pages in corpus.items():
        source = verbatim_key("".join(
            line for page in pages for line in page.plain.splitlines()
            if not is_noise(squash_whitespace(drop_glyphs(line)), LAYOUTS[doc_id])
        ))
        for chunk in chunks.values():
            if chunk.doc_id == doc_id and chunk.kind in ("clause", "note"):
                assert verbatim_key(chunk.text) in source, chunk.citation_id


@needs_corpus
def test_table_verification_rejects_a_wrong_cell(corpus):
    spec = IDBI_TABLES[0]
    verify(spec, corpus[DocId.IDBI_POLICY])
    wrong = replace(spec, rows=(Row(("01", "All type of Accounts", "Zero", "No"), "01"),))
    verify(wrong, corpus[DocId.IDBI_POLICY])  # "No" exists elsewhere in the table
    wrong = replace(spec, rows=(Row(("01", "All type of Accounts", "Rs. 15,000/-", "Yes"),
                                    "01"),))
    with pytest.raises(VerificationError, match="Rs. 15,000"):
        verify(wrong, corpus[DocId.IDBI_POLICY])


def test_layouts_cover_every_document():
    assert set(LAYOUTS) == set(DocId)
