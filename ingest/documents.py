"""Per-document segmentation: numbering, page furniture, special clauses and tables.

The corpus is three fixed PDFs (SHA-256 in corpus/README.md), so their structure is
described here explicitly. Clause ids follow docs/interfaces.md. A changed PDF fails
tests/test_ingest_chunks.py or table verification rather than silently mis-chunking.
"""

import re

from contracts.agent import CITATION_SEPARATOR, DocId
from ingest.chunks import Chunk, split_long
from ingest.pdf import PageText, squash_whitespace, verbatim_key
from ingest.segment import DocLayout, Level, Marker, Region, page_lines, segment
from ingest.tables import Row, TableSpec, VerificationError, table_chunks, verify

_ROMAN = r"(?P<n>i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)"


def _re(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _child(parent: str | None, label: str) -> str:
    return f"{parent}({label})"


# ---------- RBI_2017_CIRCULAR ----------

RBI_LAYOUT = DocLayout(
    doc_id=DocId.RBI_2017_CIRCULAR,
    levels=[
        Level(_re(r"(?P<n>\d{1,2})\.\s+(?P<rest>.*)"), "number", lambda _, n: n),
        Level(_re(rf"\({_ROMAN}\)\s+(?P<rest>.*)"), "roman", _child, parent=0),
    ],
    # The opening unnumbered paragraph is paragraph 1 (docs/interfaces.md).
    start=_re(r"Please refer to our circular"),
    noise=[
        _re(r"RBI,DBR,CO - \d+ - continuation sheet"),
        _re(r"_{5,}"),
        _re(r".*[ऀ-ॿ].*"),  # Hindi letterhead
        _re(r"Department of Banking Regulation,Central Office.*"),
        _re(r"Tel No:.*"),
    ],
    headings=[
        _re(r"Strengthening of systems and procedures"),
        _re(r"Reporting of unauthorised transactions by customers to banks"),
        _re(r"Limited Liability of a Customer"),
        _re(r"\([ab]\) (Zero|Limited) Liability of a Customer"),
        _re(r"Reversal Timeline for Zero Liability/ ?Limited Liability of customer"),
        _re(r"Board Approved Policy for Customer Protection"),
        _re(r"Burden of Proof"),
        _re(r"Reporting and Monitoring Requirements"),
    ],
    markers=[
        Marker(_re(r"Please refer to our circular"), "1", label=(0, "1")),
        Marker(_re(r"Further, if the delay in reporting is beyond seven working days"),
               "7(ii)-beyond-7", parent="7"),
        Marker(_re(r"The number of working days mentioned in Table 2"), "Table-2-note",
               kind="note", parent="Table-2"),
    ],
    regions=[
        Region(_re(r"Table 1$"), _re(r"Further, if the delay in reporting")),
        Region(_re(r"Table 2$"), _re(r"The number of working days mentioned in Table 2")),
        Region(_re(r"Yours faithfully,")),  # signature and Annex (parsed separately)
    ],
)

RBI_TABLES = [
    TableSpec(
        doc_id=DocId.RBI_2017_CIRCULAR, clause_id="Table-1", pages=(4,), title="Table 1",
        caption="Maximum Liability of a Customer under paragraph 7 (ii)",
        header=("Type of Account", "Maximum liability (₹)"),
        rows=(
            Row(("BSBD Accounts", "5,000")),
            Row(("All other SB accounts", "10,000")),
            Row(("Pre-paid Payment Instruments and Gift Cards", "10,000")),
            Row(("Current/ Cash Credit/ Overdraft Accounts of MSMEs", "10,000")),
            Row(("Current Accounts/ Cash Credit/ Overdraft Accounts of Individuals with "
                 "annual average balance (during 365 days preceding the incidence of fraud) "
                 "/ limit up to Rs.25 lakh", "10,000")),
            Row(("Credit cards with limit up to Rs.5 lakh", "10,000")),
            Row(("All other Current/ Cash Credit/ Overdraft Accounts", "25,000")),
            Row(("Credit cards with limit above Rs.5 lakh", "25,000")),
        ),
        section="(b) Limited Liability of a Customer",
        related=("7(ii)", "7(ii)-beyond-7"),
    ),
    TableSpec(
        doc_id=DocId.RBI_2017_CIRCULAR, clause_id="Table-2", pages=(5,), title="Table 2",
        caption="Summary of Customer’s Liability",
        header=("Time taken to report the fraudulent transaction from the date of receiving "
                "the communication", "Customer’s liability (₹)"),
        rows=(
            Row(("Within 3 working days", "Zero liability")),
            Row(("Within 4 to 7 working days",
                 "The transaction value or the amount mentioned in Table 1, whichever is lower")),
            Row(("Beyond 7 working days", "As per bank’s Board approved policy")),
        ),
        section="(b) Limited Liability of a Customer",
        related=("6(ii)", "7(ii)", "7(ii)-beyond-7", "Table-1", "Table-2-note", "8"),
    ),
]

_ANNEX_INTRO = _re(r"Instructions in our Master Circular")
_ANNEX_ROW = _re(r"\s*(\d)\s+(II?\.\S+)")
_ANNEX_TITLE = (
    "Annex: Instructions in our Master Circular on Credit Card, Debit Card and Rupee "
    "Denominated Co-branded Pre-paid Card Operations of Banks and Credit card issuing NBFCs "
    "(DBR.No.FSD.BC.18/24.01.009/2015-16 dated July 1, 2015) which stand revised in respect "
    "of Scheduled Commercial Banks"
)


def _columns(lines: list[str], min_gap: int = 3) -> list[tuple[int, int]]:
    """Column spans of a block of layout lines: runs of positions that are not blank in
    every line, merging runs separated by fewer than `min_gap` blanks."""
    width = max(map(len, lines))
    padded = [line.ljust(width) for line in lines]
    used = [any(line[x] != " " for line in padded) for x in range(width)]
    spans: list[list[int]] = []
    for x, filled in enumerate(used):
        if not filled:
            continue
        if spans and x - spans[-1][1] < min_gap:
            spans[-1][1] = x + 1
        else:
            spans.append([x, x + 1])
    return [(start, end) for start, end in spans]


def rbi_annex_chunks(pages: list[PageText]) -> list[Chunk]:
    """Annex rows "Annex-1" to "Annex-8": Sr. No | Para No. | Instructions | Revised para.

    Columns are found from the layout text; each column's text on each page is then checked
    verbatim against that page's plain text (VerificationError if not found).
    """
    rows: list[dict] = []
    for page in pages:
        lines = [line for line in page.text.splitlines()
                 if line.strip() and "continuation sheet" not in line]
        first = next((i for i, line in enumerate(lines) if _ANNEX_ROW.match(line)), None)
        if first is None:
            continue
        # On a continuation page the lines above the first new row finish the previous row.
        body = lines if rows else lines[first:]
        spans = _columns(body)
        if len(spans) != 4:
            raise ValueError(f"RBI Annex page {page.page}: expected 4 columns, got {spans}")
        for line in body:
            cells = [line[start:end].strip() for start, end in spans]
            if cells[0]:
                rows.append({"page": page.page, "cells": [[] for _ in spans]})
            for column, cell in zip(rows[-1]["cells"], cells, strict=True):
                if cell:
                    column.append((page.page, cell))

    plain = {page.page: verbatim_key(page.plain) for page in pages}
    missing = [
        f"Annex-{row['cells'][0][0][1]} page {n}"
        for row in rows
        for column in row["cells"]
        for n in {p for p, _ in column}
        if verbatim_key(" ".join(text for p, text in column if p == n)) not in plain[n]
    ]
    if missing:
        raise VerificationError("Annex", missing)

    chunks = []
    for row in rows:
        sr, para, instructions, revised = (
            squash_whitespace(" ".join(text for _, text in column)) for column in row["cells"]
        )
        chunks.append(Chunk(
            doc_id=DocId.RBI_2017_CIRCULAR, clause_id=f"Annex-{sr}", page=row["page"],
            kind="table_row", section=_ANNEX_TITLE,
            text=(f"Annex, Sr. No. {sr}\nPara No.: {para}\nInstructions: {instructions}\n"
                  f"Revised instructions in this circular (Para No.): {revised}"),
        ))
    return chunks


# ---------- IDBI_POLICY ----------

IDBI_LAYOUT = DocLayout(
    doc_id=DocId.IDBI_POLICY,
    levels=[
        Level(_re(r"(?P<n>\d{1,2})\.\s+(?P<rest>.*)"), "number", lambda _, n: n, titled=True),
        Level(_re(r"(?P<p>\d{1,2})\.(?P<n>\d{1,2})\s+(?P<rest>.*)"), "number",
              lambda p, n: f"{p}.{n}", parent=0),
        Level(_re(rf"{_ROMAN}\)\s+(?P<rest>.*)"), "roman", _child, parent=1),
        Level(_re(r"(?P<n>[a-z])\.\s+(?P<rest>.*)"), "letter", _child, parent=1),
    ],
    start=_re(r"1\. Introduction$"),  # skips the cover and contents pages
    noise=[_re(r"Unauthorized Electronic Banking Transactions Policy")],
    markers=[
        Marker(_re(r"In cases where the responsibility for the unauthorized electronic"),
               "Table-I-note", kind="note", parent="Table-I"),
        # The sentence after Table-II belongs to section 6, which has no other text.
        Marker(_re(r"Bank shall not be liable for any liability other than the above amount"),
               "6"),
    ],
    regions=[
        Region(_re(r"Table - I$"), _re(r"In cases where the responsibility")),
        Region(_re(r"Table-II$"), _re(r"Bank shall not be liable for any liability")),
        Region(_re(r"Explanations for the abbreviations used in the Policy")),
    ],
)

_IDBI_SECTION_6 = "6. Liability of Customers and Bank under the Policy"
_BAND_3 = ("Reporting by Customer: Within 3 working days from the date of receipt of "
           "communication from the bank by the customer, intimating the debit")
_BAND_4_7 = ("Reporting by Customer : between 4 and 7 working days from the date of receipt of "
             "communication from the bank by the customer, intimating the debit")
_BAND_60 = ("Reporting by Customer : Beyond 7 working days to 60 calendar days from the date of "
            "receipt of communication from the bank by the customer, intimating the debit")
_RS_10K = ("Rs. 10,000/-Per transaction. Liability of the customer shall be limited to "
           "Rs. 10,000/- or the amount of the transaction, whichever is lower")
_RS_25K = ("Rs. 25,000/- Per transaction. Liability of the customer shall be restricted to "
           "Rs. 25,000/- or the amount of the transaction, whichever is lower")

IDBI_TABLES = [
    TableSpec(
        doc_id=DocId.IDBI_POLICY, clause_id="Table-I", pages=(7, 8), title="Table - I",
        caption=("Third party breach where the deficiency lies neither with the bank nor with "
                 "the customer but lies elsewhere in the system,"),
        header=("S. No", "Type of Account", "Liability of Customer",
                "Shadow Credit to be given"),
        rows=(
            Row(("01", "All type of Accounts", "Zero", "Yes"), "01", _BAND_3),
            Row(("02", "BSBD Accounts",
                 "Rs. 5,000/- Per transaction. Liability of the customer shall be limited to "
                 "Rs. 5,000/- or the actual amount of the transaction, whichever is lower",
                 "Yes*"), "02", _BAND_4_7),
            Row(("03", "All other Savings Bank Accounts", _RS_10K, "Yes*"), "03", _BAND_4_7),
            Row(("04", "Prepaid Instruments and Gift Cards", _RS_10K, "Yes*"), "04", _BAND_4_7),
            Row(("05", "Current / Cash Credit / Overdraft Accounts of MSME", _RS_10K, "Yes*"),
                "05", _BAND_4_7),
            Row(("06", "Current / Cash Credit/ Overdraft Accounts of Individuals with annual "
                       "average balance (during 365 days preceding the incidence of fraud) / "
                       "limit up to Rs.25 lakh", _RS_10K, "Yes*"), "06", _BAND_4_7),
            Row(("07", "Credit cards with limit up to Rs.5 lakh", _RS_10K, "Yes*"),
                "07", _BAND_4_7),
            Row(("08", "All Other Current / Cash Credit / Overdraft Accounts", _RS_25K, "Yes*"),
                "08", _BAND_4_7),
            Row(("09", "Credit Cards with limit above Rs. 5 Lacs", _RS_25K, "Yes*"),
                "09", _BAND_4_7),
            Row(("10", "All the type of Accounts",
                 ("Full", "However, customer to be compensated up to a limit of Rs. 2,500/- "
                          "or the transactions value, whichever is lower."), "No"),
                "10", _BAND_60),
        ),
        notes=("*Shadow credit shall be restricted to the amount of Bank’s Liability",),
        section=_IDBI_SECTION_6,
        related=("5.13", "5.20", "Table-I-note"),
        row_related=("5.13", "5.20"),
    ),
    TableSpec(
        doc_id=DocId.IDBI_POLICY, clause_id="Table-II", pages=(9,), title="Table-II",
        header=("S. No", "Unauthorized Transaction occurs in the following events",
                "Liability of Customer", "Shadow Credit to be given"),
        rows=(
            Row(("01", "Contributory fraud/ negligence/ deficiency on the part of the bank "
                       "(irrespective of whether or not the transaction is reported by the "
                       "customer).", "Zero", "Yes"), "01"),
            Row(("02", "If transactions have happened after reporting of unauthorized "
                       "transaction by the customer through any channels of reporting to the "
                       "Bank.", "Zero", "Yes"), "02"),
            Row(("03", "Transactions happened due to negligence of customer (i.e. where the "
                       "customer has shared credentials or Account/Transaction details, which "
                       "may be Internet Banking user Id & PIN, Debit/Credit Card PIN/OTP or due "
                       "to improper protection on customer devices like mobile/laptop/desktop "
                       "leading to malware/Trojan or Phishing/Vishing attack, etc., This could "
                       "also be due to SIM deactivation done ) until the customer reports "
                       "unauthorized transaction to the Bank", "Full", "No"), "03"),
        ),
        section=_IDBI_SECTION_6,
        related=("5.12", "6"),
        row_related=("5.12",),
    ),
]

# ---------- RBI_IOS_2026 ----------

_IOS_LEVELS = [
    Level(_re(r"(?P<n>\d{1,2})\.\s+(?P<rest>.*)"), "number", lambda _, n: n, titled=True),
    Level(_re(r"\(\s*(?P<n>\d{1,2})\s*\)\s*(?P<rest>.*)"), "number", _child, parent=0),
    Level(_re(r"\(\s*(?P<n>[a-z])\s*\)\s*(?P<rest>.*)"), "letter", _child, parent=1),
    # (i), (ii) under a lettered item: deeper than the clause_id scheme, kept in its text.
    Level(_re(rf"\(\s*{_ROMAN}\s*\)\s*(?P<rest>.*)"), "roman", _child, parent=2, emit=False),
]

IOS_LAYOUT = DocLayout(
    doc_id=DocId.RBI_IOS_2026,
    levels=_IOS_LEVELS,
    start=_re(r"CHAPTER I$"),  # skips the title and preamble
    noise=[
        _re(r"\d \S+@rbi\.org\.in"),  # footnotes with the CRPC address
        _re(r"\d Centralised Receipt and Processing Centre, 4th Floor.*"),
        _re(r"Vista, Chandigarh.*"),
    ],
    headings=[
        _re(r"CHAPTER [IVX]+"), _re(r"[A-Z][A-Z0-9 ,/–-]+"), _re(r"Annex"),
        # Annex Part A subheadings (mixed case, so the all-caps pattern misses them).
        _re(r"Time limits for filing a complaint"),
        _re(r"Using the services of an advocate for filing of a complaint"),
        _re(r"Amount of compensation admissible under the Scheme"),
        _re(r"Documents needed for filing a complaint"),
        _re(r"Where / how to file a complaint"),
        _re(r"Where / how to get further information"),
    ],
    regions=[Region(_re(r"Part B: FORM OF COMPLAINT"))],  # the complaint form, not provisions
    zones=[(_re(r"Part A: Guidance for filing of a complaint"), [
        Level(_re(r"(?P<n>\d)\.\s+(?P<rest>.*)"), "number", lambda _, n: f"Annex-A.{n}"),
    ])],
)

# ---------- corpus ----------

LAYOUTS = {layout.doc_id: layout for layout in (RBI_LAYOUT, IDBI_LAYOUT, IOS_LAYOUT)}
TABLES = {DocId.RBI_2017_CIRCULAR: RBI_TABLES, DocId.IDBI_POLICY: IDBI_TABLES,
          DocId.RBI_IOS_2026: []}

# Definitions a clause depends on (docs/interfaces.md: link, do not merge).
RELATED = {
    DocId.RBI_2017_CIRCULAR: {
        "6(ii)": ("Table-2", "Table-2-note"),
        "7(ii)": ("Table-1", "Table-2", "Table-2-note"),
        "7(ii)-beyond-7": ("Table-2",),
        "Table-2-note": ("Table-2",),
    },
    DocId.IDBI_POLICY: {
        "Table-I-note": ("Table-I", "5.13", "5.20"),
        "5.12": ("Table-II",),
        "5.13": ("Table-I",),
        "5.20": ("Table-I",),
    },
    DocId.RBI_IOS_2026: {},
}


def _link(chunk: Chunk) -> Chunk:
    extra = RELATED[chunk.doc_id].get(chunk.clause_id, ())
    if not extra:
        return chunk
    cites = [f"{chunk.doc_id}{CITATION_SEPARATOR}{c}" for c in extra]
    return chunk.model_copy(update={"related": [*chunk.related, *cites]})


def chunk_document(doc_id: DocId, pages: list[PageText]) -> list[Chunk]:
    """All chunks of one document: prose clauses, tables and table rows."""
    chunks = segment(page_lines(pages), LAYOUTS[doc_id])
    for spec in TABLES[doc_id]:
        verify(spec, pages)
        chunks += table_chunks(spec)
    if doc_id == DocId.RBI_2017_CIRCULAR:
        chunks += rbi_annex_chunks(pages)
    return [_link(part) for chunk in chunks for part in split_long(chunk)]


def chunk_corpus(corpus: dict[DocId, list[PageText]]) -> list[Chunk]:
    return [chunk for doc_id, pages in corpus.items() for chunk in chunk_document(doc_id, pages)]
