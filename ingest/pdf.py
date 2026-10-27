"""Page text from the corpus PDFs.

pypdf has two extraction modes and each fails on part of this corpus:
- plain splits many words ("wor king", "compensat es"), mostly in IDBI_POLICY;
- layout splits far fewer and keeps table columns, but garbles some fonts
  (RBI_2017_CIRCULAR page 4 comes out as "Credit⁣慲摳").

So every page is extracted in layout mode and checked against plain mode: ignoring
whitespace, both must contain the same characters (layout may reorder them, e.g. page
numbers and table cells). A page that fails falls back to plain text. Split words are then
rejoined on every page. Only whitespace changes, so text taken from a page still matches
the PDF verbatim in the sense of eval/corpus.py.

The IDBI PDF is AES-encrypted with an empty password; pypdf needs `cryptography` for it.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pypdf import PdfReader

from contracts.agent import DocId

RAW_DIR = Path(__file__).resolve().parent.parent / "corpus" / "raw"

# Filenames as recorded in corpus/README.md (Provenance). Same as eval/corpus.py.
DOC_FILES: dict[DocId, str] = {
    DocId.RBI_2017_CIRCULAR: "RBI customer protection.pdf",
    DocId.IDBI_POLICY: "Unauthorized transactions policy.pdf",
    DocId.RBI_IOS_2026: "Ombudsman Scheme.pdf",
}

_WHITESPACE = re.compile(r"\s+")
_GAP = re.compile(r"( +)")
_SPACE_BEFORE_PUNCT = re.compile(r" +([,.;:)])")
_LETTERS = re.compile(r"[A-Za-z]+")
_TRAILING_LETTERS = re.compile(r"[A-Za-z]+$")
_LEADING_LETTERS = re.compile(r"^[A-Za-z]+")


@dataclass(frozen=True)
class PageText:
    doc_id: DocId
    page: int  # 1-based PDF page index, not the number printed on the page
    text: str  # layout text keeps column spacing; use squash_whitespace() for prose
    mode: Literal["layout", "plain"]


def _chars(text: str) -> Counter[str]:
    """Characters of `text` ignoring whitespace, for the layout-vs-plain check."""
    return Counter(_WHITESPACE.sub("", unicodedata.normalize("NFKC", text)))


def squash_whitespace(text: str) -> str:
    """One space between words, none before , . ; : ) ("Rs. 10 ,000" -> "Rs. 10,000")."""
    return _SPACE_BEFORE_PUNCT.sub(r"\1", _WHITESPACE.sub(" ", text)).strip()


def _is_split(left: str, right: str, vocab: Counter[str]) -> bool:
    # A split leaves at least one fragment that is rarer than the whole word: "wor king",
    # "h as". Two common words whose concatenation is also a word ("in to") stay apart, and
    # so does a capitalised right part, which starts a word or acronym ("the RE").
    if not right.islower():
        return False
    joined = vocab[(left + right).lower()]
    return joined > 0 and joined > min(vocab[left.lower()], vocab[right.lower()])


def rejoin_split_words(line: str, vocab: Counter[str]) -> str:
    """Rejoin words that pypdf split with spaces, judged by word counts in `vocab`.

    Any gap counts: layout mode spreads justified lines, so a split can be wide
    ("consid      ered"). Gaps that do not join are kept, preserving table columns.
    """
    parts = _GAP.split(line)  # tokens at even indexes, the gaps between them at odd
    out = [parts[0]]
    for gap, token in zip(parts[1::2], parts[2::2], strict=True):
        tail = _TRAILING_LETTERS.search(out[-1])
        head = _LEADING_LETTERS.match(token)
        if tail and head and _is_split(tail.group(), head.group(), vocab):
            out[-1] += token
        else:
            out += [gap, token]
    return "".join(out)


def _tidy(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines())


def _read(doc_id: DocId, raw_dir: Path) -> list[tuple[str, str]]:
    reader = PdfReader(raw_dir / DOC_FILES[doc_id])
    if reader.is_encrypted:
        reader.decrypt("")
    return [
        (page.extract_text() or "", page.extract_text(extraction_mode="layout") or "")
        for page in reader.pages
    ]


def load_corpus(raw_dir: Path = RAW_DIR) -> dict[DocId, list[PageText]]:
    """Text of every page of every corpus document, in page order."""
    raw = {doc_id: _read(doc_id, raw_dir) for doc_id in DOC_FILES}
    good = {
        (doc_id, n): _chars(plain) == _chars(layout)
        for doc_id, pages in raw.items()
        for n, (plain, layout) in enumerate(pages, start=1)
    }
    # Word counts from pages whose layout text passed the check, i.e. with intact words.
    vocab = Counter(
        word.lower()
        for doc_id, pages in raw.items()
        for n, (_, layout) in enumerate(pages, start=1)
        if good[doc_id, n]
        for word in _LETTERS.findall(layout)
    )

    corpus: dict[DocId, list[PageText]] = {}
    for doc_id, pages in raw.items():
        corpus[doc_id] = []
        for n, (plain, layout) in enumerate(pages, start=1):
            mode = "layout" if good[doc_id, n] else "plain"
            text = layout if good[doc_id, n] else plain
            lines = (rejoin_split_words(line, vocab) for line in _tidy(text).splitlines())
            corpus[doc_id].append(PageText(doc_id, n, "\n".join(lines), mode))
    return corpus
