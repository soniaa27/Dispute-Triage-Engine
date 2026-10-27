# Corpus

Source documents for retrieval. Put the PDFs in `corpus/raw/` and commit them: they are
versioned inputs, and their SHA-256 is recorded below and (later) in the index metadata.
Build outputs (vector index, embedding cache) go to `data/`, which is git-ignored.

| # | Document | Publisher | Source URL |
|---|----------|-----------|------------|
| 1 | Customer Protection – Limiting Liability of Customers in Unauthorised Electronic Banking Transactions (6 July 2017) | Reserve Bank of India | https://www.rbi.org.in/scripts/FS_Notification.aspx?Id=11040 |
| 2 | Policy on Unauthorized Electronic Banking Transactions (25 September 2025) | IDBI Bank | https://www.idbibank.in/pdf/policy/Unauthorised-Electronic-Banking-Transactions-Policy.pdf |
| 3 | Reserve Bank – Integrated Ombudsman Scheme, 2026 | Reserve Bank of India | https://rbidocs.rbi.org.in/rdocs/content/pdfs/SCHEME16012026_A.pdf |

The RBI circular page (1) is HTML; download the PDF linked from that page.

The dates above are the dates printed on the files in `corpus/raw/` (the IDBI cover page reads
"September 25, 2025"). The IDBI URL serves whichever version is current, so check the cover
date and SHA-256 below before rebuilding the index from a fresh download. Facts used in
evaluation scenarios must be checked against these exact files.

## Provenance

| doc_id | Filename (in `corpus/raw/`) | Download date | SHA-256 | Text layer? (y/n, OCR needed?) |
|--------|-----------------------------|---------------|---------|--------------------------------|
| RBI_2017_CIRCULAR | RBI customer protection.pdf | 2026-10-24 | 630f68e6f25a8cdcfe942c1e20b73b0c807b1690b0536b84b1e8b61b23f64b0e | y, no OCR. Page 4 (Table 1) is garbled by pypdf layout mode; plain mode is used for it |
| IDBI_POLICY | Unauthorized transactions policy.pdf | 2026-10-24 | c76aa964ededcf8dede1ed9802297e737a5209442647ef237570226d113712cd | y, no OCR. AES-encrypted with an empty password (pypdf needs `cryptography`) |
| RBI_IOS_2026 | Ombudsman Scheme.pdf | 2026-10-24 | f119574982a073819d2286c3d016ef10e1fbeebed8e23864e40e885d950901b6 | y, no OCR |

`doc_id` is the identifier used in `Citation.doc_id`.

`python -m ingest build` checks each file's SHA-256 against this table and records the
result in `data/manifest.json` (`matches_provenance`); a mismatch is logged as a warning.
