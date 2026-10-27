from collections import Counter

import pytest
from pypdf import PdfReader

from contracts.agent import DocId
from eval import corpus as eval_corpus
from eval.corpus import corpus_available, normalise
from ingest.pdf import (
    DOC_FILES,
    RAW_DIR,
    load_corpus,
    rejoin_split_words,
    squash_whitespace,
)

needs_corpus = pytest.mark.skipif(
    not corpus_available(), reason="corpus PDFs not present in corpus/raw/"
)

VOCAB = Counter({"working": 5, "king": 1, "has": 9, "h": 0, "as": 40, "lien": 2, "a": 50,
                 "in": 60, "to": 70, "into": 4, "considered": 3, "there": 3, "the": 90})


@pytest.mark.parametrize(("line", "expected"), [
    ("between 4 and 7 wor king days", "between 4 and 7 working days"),
    ("customer h as made", "customer has made"),
    ("with a l ien marked", "with a lien marked"),
    ("would be consid ered, as", "would be considered, as"),
    ("(wor king)", "(working)"),
    ("would be consid        ered", "would be considered"),
    ("Accounts      Zero      Yes", "Accounts      Zero      Yes"),
    ("put in to effect", "put in to effect"),
    ("against the RE", "against the RE"),
    ("Rs. 10 ,000", "Rs. 10 ,000"),
])
def test_rejoin_split_words(line, expected):
    assert rejoin_split_words(line, VOCAB) == expected


def test_squash_whitespace():
    assert squash_whitespace("Rs.   10 ,000/-  per\n  transaction ;  done )") == \
        "Rs. 10,000/- per transaction; done)"


def test_doc_files_match_eval():
    assert DOC_FILES == eval_corpus.DOC_FILES


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@needs_corpus
def test_every_page_loaded_in_order(corpus):
    assert {doc: len(pages) for doc, pages in corpus.items()} == {
        DocId.RBI_2017_CIRCULAR: 9, DocId.IDBI_POLICY: 16, DocId.RBI_IOS_2026: 24}
    for doc_id, pages in corpus.items():
        assert [p.page for p in pages] == list(range(1, len(pages) + 1))
        assert all(p.doc_id == doc_id and p.text.strip() for p in pages)


@needs_corpus
def test_only_whitespace_changes(corpus):
    # Text from any page must still match the PDF verbatim as eval/corpus.py checks it.
    for doc_id, pages in corpus.items():
        reader = PdfReader(RAW_DIR / DOC_FILES[doc_id])
        for page, pdf_page in zip(pages, reader.pages, strict=True):
            assert Counter(normalise(page.text)) == Counter(normalise(pdf_page.extract_text()))


@needs_corpus
def test_garbled_layout_page_falls_back_to_plain(corpus):
    page = corpus[DocId.RBI_2017_CIRCULAR][3]
    assert page.mode == "plain"
    assert "Credit cards with limit up to Rs.5 lakh" in squash_whitespace(page.text)
    other = [p for pages in corpus.values() for p in pages if p is not page]
    assert all(p.mode == "layout" for p in other)


@needs_corpus
def test_no_split_words(corpus):
    text = " ".join(squash_whitespace(p.text) for pages in corpus.values() for p in pages)
    for split in ("wor king", "consid ered", "compensat es", "matt ers", "l ien", "fo r "):
        assert split not in text


@needs_corpus
def test_encrypted_idbi_page_index_is_pdf_index(corpus):
    # IDBI prints page numbers 2 lower than the PDF index: "5.13 Number of Days" is on
    # printed page 4, PDF page 6.
    pages = corpus[DocId.IDBI_POLICY]
    assert "September 25, 2025" in pages[0].text
    assert "5.13 Number of Days" in squash_whitespace(pages[5].text)
