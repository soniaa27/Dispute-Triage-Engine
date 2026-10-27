"""Rule-based clause segmentation of prose, configured per document (ingest/documents.py).

Lines are read in order. A line that starts a numbered item ("6.", "(ii)", "5.13",
"(g)") opens a new clause; other lines continue the open clause. A number only counts if it
follows the previous sibling ("(g)" after "(f)"), so a wrapped line that happens to start
with "(2)" stays text, and "(i)" is a letter after "(h)" but a roman numeral elsewhere.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from contracts.agent import DocId
from ingest.chunks import Chunk
from ingest.pdf import PageText, drop_glyphs, squash_whitespace

ROMANS = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii")
Labels = Literal["number", "roman", "letter"]

# Generic page furniture: page numbers ("7", "- 4 -") and separators ("*****").
_GENERIC_NOISE = (re.compile(r"\d{1,2}"), re.compile(r"-\s*\d{1,2}\s*-"), re.compile(r"\*{3,}"))
_BROKEN_WORD = re.compile(r"(?<=[A-Za-z])- (?=[a-z])")


@dataclass(frozen=True)
class Line:
    page: int
    text: str


@dataclass(frozen=True)
class Level:
    """One numbering level. `pattern` has named groups `n` (the label) and `rest` (text
    after it), and optionally `p`, which must equal the parent's label ("5" in "5.13")."""

    pattern: re.Pattern[str]
    labels: Labels
    make_id: Callable[[str | None, str], str]  # (parent clause_id, label) -> clause_id
    parent: int | None = None  # index of the parent level
    titled: bool = False  # the rest of the line is a heading ("8. Rights of Customers")
    emit: bool = True  # False: deeper than the clause_id scheme; stays in the parent's text


@dataclass(frozen=True)
class Marker:
    """A line that opens a clause with a fixed id ("Table-2-note") regardless of numbering."""

    pattern: re.Pattern[str]
    clause_id: str
    kind: Literal["clause", "note"] = "clause"
    parent: str | None = None
    label: tuple[int, str] | None = None  # (level, label) it stands for, e.g. paragraph 1


@dataclass(frozen=True)
class Region:
    """Lines from `start` up to (not including) `end` are left out of the prose: tables
    handled elsewhere, signatures, forms. `end=None` runs to the end of the document."""

    start: re.Pattern[str]
    end: re.Pattern[str] | None = None


@dataclass(frozen=True)
class DocLayout:
    doc_id: DocId
    levels: list[Level]
    start: re.Pattern[str] | None = None  # lines before the first match are front matter
    noise: list[re.Pattern[str]] = field(default_factory=list)  # running headers, footers
    headings: list[re.Pattern[str]] = field(default_factory=list)  # set the section
    markers: list[Marker] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    zones: list[tuple[re.Pattern[str], list[Level]]] = field(default_factory=list)


def is_noise(text: str, layout: DocLayout) -> bool:
    """Page furniture: page numbers, separators and the document's running headers."""
    return any(p.fullmatch(text) for p in (*_GENERIC_NOISE, *layout.noise))


def page_lines(pages: Iterable[PageText]) -> list[Line]:
    return [
        Line(page.page, squash_whitespace(drop_glyphs(raw)))
        for page in pages
        for raw in page.text.splitlines()
    ]


def _follows(previous: str | None, label: str, labels: Labels) -> bool:
    if labels == "number":
        return int(label) == (int(previous) + 1 if previous else 1)
    if labels == "roman":
        if label not in ROMANS:
            return False
        return ROMANS.index(label) == (ROMANS.index(previous) + 1 if previous else 0)
    if len(label) != 1:
        return False
    return ord(label) == (ord(previous) + 1 if previous else ord("a"))


_CONNECTORS = ("and", "of", "for", "the", "to", "in", "on", "under")


def _continues_title(title: str, line: str) -> bool:
    """A wrapped heading: "7. Staffing of ... Centralised Receipt and" + "Processing Centre"."""
    return title.rsplit(" ", 1)[-1] in _CONNECTORS or (
        len(line) < 40 and not line.endswith((".", ":", ";"))
    )


def _join(parts: list[str]) -> str:
    return _BROKEN_WORD.sub("-", squash_whitespace(" ".join(parts)))


@dataclass
class _Open:
    clause_id: str
    page: int
    kind: Literal["clause", "note"]
    section: str | None
    parent: str | None
    parts: list[str]
    titled: bool = False


def segment(lines: list[Line], layout: DocLayout) -> list[Chunk]:
    """Prose chunks of one document, in reading order."""
    chunks: list[Chunk] = []
    levels = layout.levels
    labels: list[str | None] = [None] * len(levels)
    ids: list[str | None] = [None] * len(levels)
    section: str | None = None
    current: _Open | None = None
    started = layout.start is None
    region_end: re.Pattern[str] | None | bool = False  # False: not inside a region

    def close() -> None:
        nonlocal current
        if current and current.parts:
            chunks.append(Chunk(doc_id=layout.doc_id, clause_id=current.clause_id,
                                text=_join(current.parts), page=current.page,
                                kind=current.kind, section=current.section,
                                parent=current.parent))
        current = None

    for line in lines:
        text = line.text
        if not text or is_noise(text, layout):
            continue
        if not started:
            if not layout.start.match(text):
                continue
            started = True
        if region_end is not False:
            if region_end is None or not region_end.match(text):
                continue
            region_end = False
        if region := next((r for r in layout.regions if r.start.match(text)), None):
            close()
            region_end = region.end
            continue
        if zone := next((z for z in layout.zones if z[0].match(text)), None):
            close()
            levels = zone[1]
            labels, ids = [None] * len(levels), [None] * len(levels)
            section = text
            continue
        if any(h.fullmatch(text) for h in layout.headings):
            close()
            section = text
            continue
        if marker := next((m for m in layout.markers if m.pattern.match(text)), None):
            close()
            if marker.label:
                k, label = marker.label
                labels[k], ids[k] = label, marker.clause_id
            current = _Open(marker.clause_id, line.page, marker.kind, section, marker.parent,
                            [text])
            continue

        for k, level in enumerate(levels):
            m = level.pattern.match(text)
            if not m:
                continue
            parent_id = ids[level.parent] if level.parent is not None else None
            if level.parent is not None and parent_id is None:
                continue
            if "p" in m.groupdict() and m["p"] != labels[level.parent]:
                continue
            if not _follows(labels[k], m["n"], level.labels):
                continue
            labels[k] = m["n"]
            for deeper in range(k + 1, len(levels)):
                labels[deeper] = ids[deeper] = None
            if not level.emit:
                if current:
                    current.parts.append(text)
                break
            close()
            ids[k] = level.make_id(parent_id, m["n"])
            if level.titled:
                section = f"{m['n']}. {m['rest']}"
                current = _Open(ids[k], line.page, "clause", section, parent_id, [],
                                titled=True)
            else:
                current = _Open(ids[k], line.page, "clause", section, parent_id,
                                [m["rest"]] if m["rest"] else [])
            break
        else:
            if current and current.titled and not current.parts and section \
                    and _continues_title(section, text):
                section = current.section = f"{section} {text}"
            elif current:
                current.parts.append(text)
    close()
    return chunks
