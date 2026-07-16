# LAM — Local Archives Manager

LAM provides deterministic local maintenance for a biomedical literature
library stored alongside the source repository. Version 0.5.1 implements
Workflow 1 (local reconciliation), Workflow 2 (network metadata lookup),
Workflow 3 (Inbox identification and registration), and Workflow 4 (filing by
the user-controlled `topic_folder`), including safe re-filing after a topic
change. Final topic content now lives exclusively below `Topics/`; an explicit
transactional migration upgrades legacy root-level topic directories. The CLI
also provides a machine-readable command registry and sanitized invocation
audit log for Agent calls. It also provides allowlisted maintenance cleanup.
It never accesses `summary.md`.

Workflow 2 queries PubMed, arXiv, and Unpaywall through synchronous,
provider-limited clients. Responses are versioned and cached under
`.library_state/metadata_cache`; exact identifiers take priority over titles,
and conflicting or ambiguous identities are never written silently.

```text
ResearchLibrary/
|-- Inbox/          # unidentified or incomplete
|-- Registered/     # registered, awaiting topic filing
|-- Topics/         # only namespace for final topic content
|-- LAM_tools/      # application source
|-- scripts/ build/ dist/
|-- catalogue.xlsx
`-- .library_state/ # derived state, reports, journals and invocation logs
```

`topic_folder` is relative to `Topics/` (for example `IBD/Epithelial`), while
`pdf_relative_path` records the observed full library-relative location (for
example `Topics/IBD/Epithelial/paper.pdf`).

## Install

From the repository root, using the `lam_agent` Conda environment:

```powershell
conda activate lam_agent
python -m pip install -e ".[dev]"
```

## Commands

The following table is generated from the same registry used by CLI help and
`lam commands --json`:

| Command | Category | Purpose | Dry run | Network |
|---|---|---|---:|---:|
| `lam check` | daily | Reconcile Catalogue and managed file state | yes | no |
| `lam register` | daily | Identify and register Inbox papers and supplementary documents | yes | yes |
| `lam search` | daily | Query providers and optionally update or download records | yes | yes |
| `lam file` | daily | File or refile registered Documents under Topics/ | yes | no |
| `lam cleanup` | maintenance | Apply allowlisted generated-file retention | yes | no |
| `lam normalize-records` | maintenance | Canonicalize existing records by exact identifiers | yes | yes |
| `lam repair-publication-types` | maintenance | Normalize publication types and Registered filenames | yes | no |
| `lam migrate-topics` | maintenance | Move legacy root topic directories into Topics/ | yes | no |
| `lam migrate-documents` | maintenance | Create Documents sheet and migrate legacy main PDFs | yes | no |
| `lam doctor` | maintenance | Check OCR and local runtime availability | no | no |
| `lam commands` | audit | List the public CLI command registry | no | no |

```powershell
lam check --root D:\ResearchLibrary --dry-run
lam check --root D:\ResearchLibrary --json
lam register --root D:\ResearchLibrary --dry-run
lam register --root D:\ResearchLibrary --max-files 5
lam register --root D:\ResearchLibrary --filename-only
lam register --root D:\ResearchLibrary --ocr auto
lam register --root D:\ResearchLibrary --ocr always --ocr-dpi 250 --dry-run
lam register --root D:\ResearchLibrary --ocr never
lam doctor --root D:\ResearchLibrary --json
lam repair-publication-types --root D:\ResearchLibrary --dry-run
lam repair-publication-types --root D:\ResearchLibrary
lam normalize-records --root D:\ResearchLibrary --dry-run
lam normalize-records --root D:\ResearchLibrary
lam search --root D:\ResearchLibrary --pmid 34265844 --dry-run
lam search --root D:\ResearchLibrary --doi 10.1038/s41586-021-03819-2 --dry-run
lam search --root D:\ResearchLibrary --arxiv-id 1706.03762 --dry-run
lam search --root D:\ResearchLibrary --row 25
lam search --root D:\ResearchLibrary --missing-metadata --max-records 25
lam search --root D:\ResearchLibrary --incomplete-records --max-records 25
lam search --root D:\ResearchLibrary --normalize-existing --max-records 25
lam search --root D:\ResearchLibrary --doi 10.1000/example --offline
lam search --root D:\ResearchLibrary --arxiv-id 1706.03762 --download
lam search --root D:\ResearchLibrary --doi 10.1000/example --download --download-source unpaywall
lam search --root D:\ResearchLibrary --doi 10.1000/example --download --dry-run
lam file --root D:\ResearchLibrary --dry-run
lam file --root D:\ResearchLibrary --json
lam migrate-topics --root D:\ResearchLibrary --dry-run
lam migrate-topics --root D:\ResearchLibrary --apply
lam migrate-documents --root D:\ResearchLibrary --dry-run
lam migrate-documents --root D:\ResearchLibrary --apply
lam cleanup --root D:\ResearchLibrary --dry-run
lam cleanup --root D:\ResearchLibrary --apply
lam commands --root D:\ResearchLibrary --json
lam check --root D:\ResearchLibrary --caller agent --json
```

`search --dry-run` performs real provider queries and may update the metadata
cache, but does not modify the catalogue, snapshots, files, change log, or
operation journal. Add `--no-cache-write` for a fully read-only query, or
`--offline` to use only valid cached responses.

PDF transfer is separately opt-in through `search --download`. Eligible links
are limited to official arXiv PDF links and explicit Unpaywall `url_for_pdf`
locations. Downloads are streamed to `.library_state/tmp`, validated as a
readable PDF with a matching DOI or arXiv identifier, and committed without
overwrite to `Inbox/`. Query parameters are removed from reports. A download
dry run selects and reports a plan but does not request the PDF or create a
temporary file. Use `--max-download-size MB`, `--download-timeout SECONDS`, or
`--download-source {auto,arxiv,unpaywall}` to apply narrower bounds.

Version 0.5.1 separates bibliographic identity from physical files.
`Catalogue` contains one row per paper and uses immutable `paper_uuid` values;
`Documents` contains one row per main or supplementary file, linked by that
UUID. Run `migrate-documents --dry-run` and then `--apply` once when upgrading
an existing 0.5.0 workbook. Legacy `id`, `record_uid`, and `pdf_*` columns are
retained read-only for one compatibility release; new file state is written
only to `Documents`.

After migration, Workflow 3 recognizes UUID-bound supplementary names such as
`<paper_uuid>__table01.xlsx` and exact same-stem groups such as
`paper.pdf` plus `paper_supp1.pdf`. Supplementary registration does not call
Workflow 2 and does not parse spreadsheet content. It checks SHA-256,
document ID, type/sequence, and target collisions, then writes a normalized
filename and a file-level uncertainty only in `Documents`. Workflow 4 moves
all Documents belonging to a paper together, including PDF, XLSX, XLS, and
CSV files.

Workflow 3 uses progressive identification and retries existing `LOCAL:` rows.
User-confirmed identity, catalogue PMID/DOI/arXiv identifiers, PDF identifiers,
catalogue title evidence, filename evidence, bounded `pypdf`, and first-page
EasyOCR are attempted in that order. Provider, catalogue, PDF, and confirmation
evidence are merged before the durable identity check, so a provider response
does not need to repeat fields already established locally. Conservative
first-page metadata extraction may fill blank title, author, year, journal,
DOI, PMID, and publication-type cells after provider failure; it never invents
abstracts or keywords and never overwrites populated fields. Common journal-name
variants are recorded as notes instead of identity conflicts.

Every processed unresolved PDF receives a stable `LOCAL:` provisional row,
stays in `Inbox/`, and carries at most one active paper-identity review blocker.
`USER_CONFIRMED`, `USER_CONFIRMED:`, and field-specific confirmation forms are
recognized; an empty confirmed value still clears the corresponding blocker.
Deleting a snapshotted machine blocker authorizes one retry with the same
evidence and does not immediately recreate it. Materially new evidence may
raise one new blocker. A later durable identity upgrades the same row rather
than creating another one. `--skip-pdf-text` and `--filename-only` disable
page-text extraction and OCR but still permit catalogue/filename matching and
provisional recording.

Every machine-maintained row now has an immutable UUID `record_uid`, while the
user-facing `id` is upgraded by priority (`PMID:`, `DOI:`, `ARXIV:`, then
`LOCAL:`). After an exact provider match, one canonicalization step normalizes
the identifier, provider fields, official journal title/abbreviation, and the
single primary `source` (`pubmed`, `arxiv`, `unpaywall`, or `local_pdf`). Field
provenance remains in reports and caches rather than accumulating in `source`.

Workflow 2 can revisit registered records without scanning or moving their
PDFs. `search --incomplete-records` selects identifier-backed rows missing
metadata; `search --normalize-existing` performs exact-identifier
canonicalization. `normalize-records` is the migration entry point: dry-run
previews Catalogue changes, while apply mode adds missing `record_uid` values,
upgrades canonical IDs and sources, and runs one final check. It never invokes
Workflow 4 or moves PDFs. Filename changes implied by canonical metadata are
reported as a separate plan only.

`lam doctor` checks pdf2image, Poppler, EasyOCR, Torch/CUDA, local model
availability, and temporary-directory access. EasyOCR model downloads are
disabled by default (`OCR_DOWNLOAD_ENABLED=false`), so Workflow 3 never starts
an implicit large download. If an explicit one-time initialization is desired,
temporarily enable that setting and run `lam doctor`, then disable it again.

`publication_type` stores one canonical special genre only. Ordinary research
articles and provider/index labels such as `Journal Article`,
`Research Support, ...`, and Unpaywall `journal-article` are omitted. Known
special genres such as `Review`, `Systematic Review`, `Meta-analysis`, and
`Erratum` remain available for standard filenames. Provider raw values stay in
metadata caches and field provenance rather than the catalogue cell.

`repair-publication-types --dry-run` reports old/new types and filenames,
title-truncation changes, and collision blockers without modifying the
catalogue, snapshots, operation journals, change log, or PDFs. Apply mode
backs up `catalogue.xlsx`, normalizes all type cells, safely renames only direct
children of `Registered/`, and runs one Workflow 1 final check.

Workflow 4 accepts registered PDFs from `Registered/` and already filed PDFs
below `Topics/`. It resolves `topic_folder` relative to `Topics/`, supports
limited safe nesting, trusts the Catalogue path and filename, does not inspect
PDF content or call Workflow 2, preserves the filename, blocks target
collisions, and runs one final check. After a successful topic-to-topic move it
removes only a truly empty old topic directory; a directory containing
`summary.md`, hidden files, or any other content is retained.

`migrate-topics --dry-run` classifies legacy root directories using Catalogue
references and reports unknown directories without moving them. Apply mode
moves each confirmed directory as one no-overwrite operation, carries
`summary.md` without opening it, updates Catalogue paths atomically, records an
operation journal, supports recovery when a directory move completed before a
Catalogue update, and runs one final check. See
[the 0.5.0 migration guide](MIGRATION_0.5.0.md).

Every top-level CLI invocation writes one sanitized record below
`.library_state/invocations/`. Agents pass `--caller agent`; nested workflows
share the same `RunContext` and do not create duplicate invocation entries.
Reports expose a common command/workflow/version/caller/status envelope.

`cleanup --dry-run` reports only strictly allowlisted machine-generated
artifacts and estimated recoverable bytes. `cleanup --apply` enforces retention
for Catalogue backups, reports, rotated logs, completed operation journals,
stale temporary files, expired metadata cache entries, and old snapshot
generations. It never selects PDFs, `catalogue.xlsx`, project instructions,
`summary.md`, ordinary topic folders, unfinished journals, or files outside the
explicit maintenance roots.

## Network configuration

Copy `LAM_tools/.env.example` to `LAM_tools/.env` and configure at least:

```text
NCBI_EMAIL=you@example.org
NCBI_TOOL=LAM
NCBI_API_KEY=
UNPAYWALL_EMAIL=you@example.org
HTTP_USER_AGENT=LAM/0.5.1
RESERVED_ROOT_DIRECTORIES=
DOWNLOAD_ENABLED=true
DOWNLOAD_MAX_BYTES=157286400
DOWNLOAD_TIMEOUT_SECONDS=120
OCR_ENABLED=true
OCR_LANGUAGES=en
OCR_DPI=250
OCR_GPU=auto
OCR_DOWNLOAD_ENABLED=false
POPPLER_PATH=
```

LAM never prints these values. PubMed uses a minimum interval of 0.36 seconds
without an API key and 0.11 seconds with a key; arXiv uses one synchronous
connection and at least 3.2 seconds between requests; Unpaywall uses a local
0.25-second interval and a persistent daily counter. Temporary failures use
bounded retries and `Retry-After` when supplied.

All commands also work as `python -m lam ...`. `register` processes direct
managed documents in `Inbox/`; main PDFs use progressive identification while
recognized supplementary PDF/XLSX/XLS/CSV files use deterministic UUID or
same-stem binding. Successful files move to `Registered/`, a recoverable
operation journal is written, and one final Workflow 1 check runs. Workflow 4
is never invoked automatically.

A dry run may write a report and
debug log under `.library_state`, but it does not modify the catalogue, managed
PDFs, official snapshots, operation journals, or `library_changes.md`.

Exit codes are `0` for success, `2` for completed work with review items, `3`
for no changes, `10` for configuration errors, `20` for catalogue errors,
`30` for file-operation failures, and `40` for provider/network failures.

## Test

```powershell
python -m pytest
python -m pytest -m live
python -m pytest -m live_download
python -m pytest -m ocr_live
```

Tests use temporary fixture libraries and do not touch the real catalogue or
PDF collection. Live provider and download tests are excluded by default and
make only bounded, exact public requests when explicitly selected.

## Repository layout

```text
ResearchLibrary/
├── pyproject.toml
├── README.md
├── LAM_tools/
│   ├── src/lam/
│   └── tests/
├── Inbox/          # local data; ignored by Git
├── Registered/     # local data; ignored by Git
└── catalogue.xlsx  # local data; ignored by Git
```

The root `.gitignore` excludes PDFs, catalogue files, snapshots, reports,
runtime journals, local configuration, and IDE/cache artifacts so that Git
updates contain project sources rather than private literature data.
