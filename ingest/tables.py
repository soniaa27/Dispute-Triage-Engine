"""Tables, transcribed as specs and checked against the PDF.

pypdf cannot recover these tables reliably: merged cells (IDBI Table-I rows 03-07 share one
liability cell) come out interleaved or detached from their rows, and the RBI Table 1 page
is garbled in layout mode. So each table's cells are written out in ingest/documents.py and
`verify()` checks, at every build, that each cell appears verbatim in the plain text of the
table's pages. A wrong transcription or a changed PDF fails the build instead of producing
a citation that does not match the source.
"""

from dataclasses import dataclass, field

from contracts.agent import CITATION_SEPARATOR, DocId
from ingest.chunks import Chunk
from ingest.pdf import PageText, verbatim_key

Cell = str | tuple[str, ...]  # a tuple: fragments the PDF separates (another column between)


def _cell_text(cell: Cell) -> str:
    return cell if isinstance(cell, str) else " ".join(cell)


def _fragments(cell: Cell) -> tuple[str, ...]:
    return (cell,) if isinstance(cell, str) else cell


@dataclass(frozen=True)
class Row:
    cells: tuple[Cell, ...]
    row_id: str | None = None  # "03" -> chunk "Table-I.03"; None: not a separate chunk
    band: str | None = None  # banner the row sits under (reporting-delay band)

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(_cell_text(c) for c in self.cells)


@dataclass(frozen=True)
class TableSpec:
    doc_id: DocId
    clause_id: str  # "Table-I"
    pages: tuple[int, ...]
    title: str  # as printed: "Table - I"
    header: tuple[str, ...]
    rows: tuple[Row, ...]
    caption: str | None = None
    notes: tuple[str, ...] = ()  # footnotes printed under the table
    section: str | None = None
    related: tuple[str, ...] = ()  # clause_ids in the same document
    row_related: tuple[str, ...] = ()

    def texts(self) -> list[str]:
        """Every transcribed string, for verification."""
        out = [self.title, *self.header, *self.notes]
        if self.caption:
            out.append(self.caption)
        for row in self.rows:
            out += [f for cell in row.cells for f in _fragments(cell)]
            if row.band:
                out.append(row.band)
        return out


@dataclass
class VerificationError(Exception):
    clause_id: str
    missing: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.clause_id}: not found verbatim in the PDF: {self.missing}"


def verify(spec: TableSpec, pages: list[PageText]) -> None:
    """Raise VerificationError unless every cell is in the plain text of spec.pages."""
    source = verbatim_key("".join(p.plain for p in pages if p.page in spec.pages))
    missing = [text for text in spec.texts() if verbatim_key(text) not in source]
    if missing:
        raise VerificationError(spec.clause_id, missing)


def _cite(doc_id: DocId, clause_ids: tuple[str, ...]) -> list[str]:
    return [f"{doc_id}{CITATION_SEPARATOR}{c}" for c in clause_ids]


def table_chunks(spec: TableSpec) -> list[Chunk]:
    """The whole table as one chunk, plus one chunk per row that has a row_id."""
    lines = [spec.title, *([spec.caption] if spec.caption else []), " | ".join(spec.header)]
    band = None
    for row in spec.rows:
        if row.band and row.band != band:
            band = row.band
            lines.append(band)
        lines.append(" | ".join(row.texts))
    lines += spec.notes
    common = {"doc_id": spec.doc_id, "page": spec.pages[0], "section": spec.section}
    chunks = [Chunk(clause_id=spec.clause_id, text="\n".join(lines), kind="table",
                    related=_cite(spec.doc_id, spec.related), **common)]

    for row in spec.rows:
        if not row.row_id:
            continue
        starred = any("*" in cell for cell in row.texts)
        text = [f"{spec.title}, {spec.header[0]} {row.row_id}",
                *([spec.caption] if spec.caption else []),
                *([row.band] if row.band else []),
                *(f"{name}: {cell}" for name, cell in zip(spec.header[1:], row.texts[1:],
                                                         strict=True)),
                *(spec.notes if starred else ())]
        chunks.append(Chunk(clause_id=f"{spec.clause_id}.{row.row_id}", text="\n".join(text),
                            kind="table_row", parent=spec.clause_id,
                            related=_cite(spec.doc_id, spec.row_related), **common))
    return chunks
