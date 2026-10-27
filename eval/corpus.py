"""Corpus text for evaluation checks, extracted from the PDFs in corpus/raw/.

Text matching ignores whitespace entirely: pypdf inserts spurious spaces inside words
("wor king", "Rs. 10 ,000"), so a passage is "verbatim" if it matches after removing all
whitespace, applying NFKC and mapping typographic quotes and dashes to ASCII.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

from contracts.agent import CITATION_SEPARATOR, DocId

RAW_DIR = Path(__file__).resolve().parent.parent / "corpus" / "raw"

# Filenames as recorded in corpus/README.md (Provenance).
DOC_FILES: dict[DocId, str] = {
    DocId.RBI_2017_CIRCULAR: "RBI customer protection.pdf",
    DocId.IDBI_POLICY: "Unauthorized transactions policy.pdf",
    DocId.RBI_IOS_2026: "Ombudsman Scheme.pdf",
}

_PUNCT = str.maketrans({"‘": "'", "’": "'", "‟": '"', "“": '"', "”": '"', "–": "-", "—": "-"})
_WHITESPACE = re.compile(r"\s+")

# Clause ID conventions from docs/interfaces.md. An optional "#n" suffix marks a split chunk.
_ROMAN = r"\((?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\)"
CLAUSE_ID_PATTERNS: dict[DocId, re.Pattern[str]] = {
    DocId.RBI_2017_CIRCULAR: re.compile(
        rf"^(?:\d{{1,2}}(?:{_ROMAN})?|7\(ii\)-beyond-7|Table-[12]|Table-2-note|Annex-\d+)(?:#\d+)?$"
    ),
    DocId.IDBI_POLICY: re.compile(
        rf"^(?:\d{{1,2}}(?:\.\d{{1,2}})?(?:{_ROMAN}|\([a-z]\))?"
        r"|Table-I|Table-II|Table-I\.(?:0[1-9]|10)|Table-II\.0[1-3]|Table-I-note)(?:#\d+)?$"
    ),
    DocId.RBI_IOS_2026: re.compile(
        r"^(?:\d{1,2}(?:\(\d{1,2}\))?(?:\([a-z]\))?|Annex-A\.\d+)(?:#\d+)?$"
    ),
}


def normalise(text: str) -> str:
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", text).translate(_PUNCT))


@lru_cache(maxsize=4)
def load_corpus_text(raw_dir: Path = RAW_DIR) -> dict[DocId, str]:
    """Normalised full text per document. Raises FileNotFoundError if a PDF is missing."""
    texts = {}
    for doc_id, filename in DOC_FILES.items():
        reader = PdfReader(raw_dir / filename)
        texts[doc_id] = normalise("".join(page.extract_text() or "" for page in reader.pages))
    return texts


def corpus_available(raw_dir: Path = RAW_DIR) -> bool:
    return all((raw_dir / name).is_file() for name in DOC_FILES.values())


def split_citation_id(citation_id: str) -> tuple[DocId, str]:
    """'IDBI_POLICY:5.13' -> (DocId.IDBI_POLICY, '5.13'). Raises ValueError if malformed."""
    doc, sep, clause = citation_id.partition(CITATION_SEPARATOR)
    if not sep or not clause:
        raise ValueError(f"{citation_id!r} is not DOC_ID:clause_id")
    return DocId(doc), clause


def clause_id_valid(citation_id: str) -> bool:
    try:
        doc_id, clause_id = split_citation_id(citation_id)
    except ValueError:
        return False
    return bool(CLAUSE_ID_PATTERNS[doc_id].match(clause_id))


def base_citation_id(citation_id: str) -> str:
    """Drop a '#n' chunk suffix so parts of one clause compare equal."""
    return citation_id.split("#", 1)[0]


def _term_key(text: str) -> str:
    return normalise(text).replace(",", "").lower()


def missing_terms(text: str, terms: list[str]) -> list[str]:
    """Terms not found in `text`, ignoring case, commas and whitespace ("Rs. 2,500" ~ "2500")."""
    haystack = _term_key(text)
    return [term for term in terms if _term_key(term) not in haystack]


def text_in_document(text: str, doc_id: str, corpus: dict[DocId, str]) -> bool:
    """True if `text` appears verbatim (see module docstring) in document `doc_id`."""
    try:
        doc_text = corpus[DocId(doc_id)]
    except ValueError:
        return False
    needle = normalise(text)
    return bool(needle) and needle in doc_text
