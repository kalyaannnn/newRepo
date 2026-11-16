# WALLTOWN — Phase 1

Analyze a local GoldFynch Excel manifest and select a reproducible pilot. This
phase reads metadata only. It does not open case documents or use network APIs.

```bash
uv sync
uv run walltown analyze
uv run pytest
```

The default input is `data/raw/wallTownManifest.xlsx`. The workbook is opened with
pandas and openpyxl and its SHA-256 is checked before and after analysis. It is
never rewritten. Run from the project root. The existing `src/walltown/` package
and `walltown` entry point are retained. No API key is required for Phase 1.

Generated local files:

- `data/processed/corpus_summary.json`
- `data/processed/tag_distribution.csv`
- `data/processed/duplicate_report.csv`
- `data/pilot/pilot_manifest.csv`
- `data/pilot/pilot_selection_summary.json`

Console output contains aggregate counts and generated paths. Reports and pilot
files contain sensitive source metadata, remain under ignored `data/`, and are
written with owner-only permissions. `.env` and `.venv/` are also ignored.

## Schema and metadata

Headers are matched through explicit aliases. GoldFynch `GF ID`, `GF Folder`,
`Byte Count`, `MD5 Hash`, and `Primary Date` are supported, along with common
alternative headers. The one worksheet with an explicit document-ID column is
detected automatically; summary worksheets are skipped. Multiple matching
worksheets require `--sheet "Worksheet name"`.

`document_id` is required; IDs are never generated from filenames or row numbers.
Rows without IDs are counted and excluded from sampling. All optional missing
columns, missing values, invalid counts/hashes, repeated IDs, and conflicting
values are reported. Literal strings such as `NA` remain strings. Blank numeric
values stay blank, zero remains zero, and conflicting counts are not totaled.
An absent tags column is reported as unknown metadata rather than proof that
documents are untagged.
The statistics population is unique document IDs; repeated-ID rows are not
double counted. Numeric IDs are preserved as Excel supplies them; precision or
leading zeros already lost in Excel cannot be recovered.

Every source column and value for each selected document is retained in the
pilot's `original_metadata` JSON cell, including unmapped fields and all repeated
rows. Original tag spellings and raw tag cell values are retained separately.
CSV cells containing arrays or objects use JSON. Dates read as Excel dates use
ISO strings. Canonical fields are conveniences; original metadata is retained
when several source values conflict. MIME types and extensions are reported
only when explicitly supplied. Content identity is based on equal hashes within
the same reported algorithm; matching filenames do not establish duplicates.

For unknown headers, supply a local JSON mapping, for example
`{"document_id": "Source ID Header", "file_size_bytes": "Size In Bytes"}`, using
`--column-map path/to/mapping.json`. Only map byte counts with known byte units.
Use `uv run walltown analyze --help` for local input/output overrides.

## Sampling and future splits

The fixed seed is 42. Primary groups are drawn sequentially without replacement:
100 random corpus documents, 75 tagged IRRELEVANT, 50 with research tags, 40 tagged
REC CENTER DEVELOPMENT, then 35 untagged or difficult documents. Each document ID
appears once. Shortages are reported per group; only after all primary draws are
complete are they filled from the remaining corpus. Fill records are explicitly
marked `shortage_fill` and identify the exhausted group; they are never relabeled
as satisfying its tag criteria. A smaller corpus produces a smaller pilot.

Research tags include the requested five named tags. Additional observed tags
are matched by the transparent keyword pattern recorded in the selection summary
(landfill, lead, soil, DEQ, Duke, contamination, environmental, water, pollution,
toxic, remediation, heavy metals). `--research-tag` can add an exact tag. These
rules select sampling strata; existing tags are never ground-truth labels.
Difficulty indicators use only supplied metadata: missing counts/types/hashes,
at least 200 pages, at least 25 MiB, image/audio/video or container/email MIME
types, and explicit encryption metadata. No inference about a PDF's text layer
is made.

The full corpus is grouped by transitive same-algorithm valid hashes, explicit
family IDs, and parent relationships, including siblings of missing parents.
Every component touched by the uniform random group is reserved for evaluation;
related nonrandom selections are also assigned `split=evaluation`. Only the
original random group has `include_in_unbiased_evaluation=true`. Other components
are development. The random group is never redrawn to improve split sizes.
Large families can therefore leave few or no development documents; actual split
counts are reported rather than breaking family protection.

For future selections, consult the complete group membership in
`corpus_summary.json` and the reserved evaluation group IDs in the pilot summary.
Never split those components. If the corpus changes, recompute groups and audit
reservations before assigning new records: new metadata can join old components.
Missing hashes or relationships cannot prove uniqueness or independence. Content
duplicates may remain within a split; they never cross the planned split when
identifiable through the supplied metadata.

Privilege/confidentiality fields and keyword matches in tags, filenames, or
folders flag records for review. Unflagged records have status `not_assessed`;
they are not assumed safe. Every pilot record has
`external_processing_authorized=false`.

The tests use synthetic workbooks and prohibit network connections. Pytest is
configured to collect only `tests/`, excluding the separate paid `test_api.py`
diagnostic. Phase 1 stops at local reports and sampling.

## Phase 2: local five-PDF extraction

```bash
uv run walltown extract
uv run pytest
```

The default source is `~/Downloads/firstPilot/`. Every regular export file is
copied into `data/raw/first_pilot/` with its relative folder structure preserved.
Source and destination SHA-256 values are verified. Originals are never written,
conflicting existing copies are not overwritten, and symlink destinations are
rejected. Production/load files are preserved alongside PDFs; unexpected file
types, missing PDFs, copy failures, and unreadable files are reported. The default
expected PDF count is five. `--source-dir`, `--manifest`, and `--expected-pdfs`
support other explicitly chosen local inputs. `.env`/`data/` tracking is checked
before copying; the command stops if these paths are already tracked.

Outputs:

- `data/processed/first_pilot_documents.jsonl`
- `data/processed/first_pilot_extraction_report.json`

PyMuPDF extracts each page using `get_text("text", sort=False)`. Text is retained
exactly, including whitespace and trailing newlines; no cleanup, OCR, or model
processing occurs. Every declared page has a 1-based result. Failed pages have
null text/counts and a failure status instead of disappearing. Corrupt/unreadable
PDFs have their own failed document record; unknown page counts remain null.
Page text and counts are validated with Pydantic, including JSONL round trips.

Empty extracted text does not establish that a page is blank. A bounded local
grayscale rendering checks for visible marks on pages without extractable text.
Visually white pages are marked blank; nonblank pages with no text are flagged as
potential OCR candidates. Inspection failures remain explicit and uncertain.
Short text, replacement/control/private-use characters, parser repairs, password
protection, and all page-level failures receive warning codes. Blank and OCR
indicators are heuristics for later review; no document is classified here.

Email `Attachment:`/`Attachments:` headers produce separate reference records
with page numbers and exact offsets into unchanged page text. Names may span
multiple header lines. References with unparseable names remain explicit.
References are checked against other exported files and, when identity is
verified, their original filenames. Filename matches are availability candidates
only. Parent-child relationships are confirmed only by independently resolved
GoldFynch IDs and an explicit parent ID. PDF EmbeddedFiles and page file-attachment
annotations are listed separately; embedded payloads are not extracted or run.
Missing attachments are warnings and do not invalidate otherwise successful
text extraction.

Identity resolution prefers the named `Walltown_Master_Manifest.xlsx` and falls
back to the existing `wallTownManifest.xlsx`, recording the fallback. It also
reads the existing pilot manifest and supported accompanying production metadata.
Only exact, algorithm-specific native-file hashes or explicit export-relative
path-to-GoldFynch-ID mappings can resolve identity. Export order, filename
similarity, and matching original filenames never establish identity. Hashes
matching several IDs remain ambiguous unless independent mapping evidence
resolves them. Contradictory evidence leaves identity unresolved.

CSV, TSV, JSON record lists, and XLSX production metadata can provide explicit
`Export Filename`/`Export Path`/`PDF Path`/`Production Filename` fields alongside
`GF ID` or `GoldFynch ID`. Generic document IDs must already exist in the manifest
to be accepted as GoldFynch IDs. Paths must be relative to the export root or the
load file's directory. A native email/DOCX hash may differ from its rendered PDF;
only explicitly named `Export SHA256`, `PDF SHA256`, or analogous algorithm
fields assert the exported PDF's checksum. Opaque DAT/OPT/LFP/TXT/XML files are
preserved and reported as uninterpreted rather than guessed. Before expanding to
300 documents, verify the actual production mapping schema and review unresolved
identities, attachment availability, and OCR/garbled-text indicators.

An unresolved identity leaves document ID, original filename/path/tags, and
parent/family fields null, with `identity_unresolved`. Known metadata is retained
with its evidence; missing metadata is never invented. The versioned JSONL schema
separates media type, identity, page text, and attachment evidence so later formats
can be added without changing existing records. Outputs use owner-only
permissions. CLI output contains aggregates and output paths; parser diagnostics
and raw exception details are suppressed to avoid leaking confidential text.

Exit status is nonzero for wrong PDF counts, copy failures, partial/failed
extractions, or failed integrity/accounting checks. Missing identities and
attachments remain reported review issues rather than relevance/unreadability
decisions. The extraction report contains no full extracted text. The command
never reruns sampling, modifies GoldFynch, or calls any external API.
