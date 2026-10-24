# Corpus

Source documents for retrieval. Put the PDFs in `corpus/raw/` and commit them: they are
versioned inputs, and their SHA-256 is recorded below and (later) in the index metadata.
Build outputs (vector index, embedding cache) go to `data/`, which is git-ignored.

| # | Document | Publisher | Source URL |
|---|----------|-----------|------------|
| 1 | Customer Protection – Limiting Liability of Customers in Unauthorised Electronic Banking Transactions (6 July 2017) | Reserve Bank of India | https://www.rbi.org.in/scripts/FS_Notification.aspx?Id=11040 |
| 2 | Policy on Unauthorized Electronic Banking Transactions (27 Sept 2024) | IDBI Bank | https://www.idbibank.in/pdf/policy/Unauthorised-Electronic-Banking-Transactions-Policy.pdf |
| 3 | Reserve Bank – Integrated Ombudsman Scheme, 2026 | Reserve Bank of India | https://rbidocs.rbi.org.in/rdocs/content/pdfs/SCHEME16012026_A.pdf |

The RBI circular page (1) is HTML; download the PDF linked from that page.

## Provenance

Fill this in after downloading. Get the hashes with `python scripts/hash_corpus.py`.

| doc_id | Filename (in `corpus/raw/`) | Download date | SHA-256 | Text layer? (y/n, OCR needed?) |
|--------|-----------------------------|---------------|---------|--------------------------------|
| RBI_2017_CIRCULAR | | | | |
| IDBI_POLICY | | | | |
| RBI_IOS_2026 | | | | |

`doc_id` is the identifier used in `Citation.doc_id`.
