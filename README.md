# LAM — Local Archives Manager

LAM provides deterministic local maintenance for a biomedical literature
library stored alongside the source repository. Version 0.5.4 implements
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
`Documents.relative_path` records the observed full library-relative location
(for example `Topics/IBD/Epithelial/paper.pdf`).

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
| `lam migrate-identifiers` | maintenance | Adopt paper_uuid and remove legacy Catalogue identity/file columns | yes | no |
| `lam doctor` | diagnostic | Check OCR and local runtime availability | no | yes |
| `lam commands` | audit | List the public CLI command registry | no | no |

```powershell
lam --root D:\ResearchLibrary check --dry-run
lam --root D:\ResearchLibrary --json check
lam --root D:\ResearchLibrary register --dry-run --offline --no-cache-write
lam --root D:\ResearchLibrary register --max-files 5
lam --root D:\ResearchLibrary register --filename-only
lam --root D:\ResearchLibrary register --ocr auto
lam --root D:\ResearchLibrary register --ocr always --ocr-dpi 250 --dry-run
lam --root D:\ResearchLibrary doctor
lam --root D:\ResearchLibrary doctor --initialize-ocr-models
lam --root D:\ResearchLibrary repair-publication-types --dry-run
lam --root D:\ResearchLibrary repair-publication-types --apply
lam --root D:\ResearchLibrary normalize-records --dry-run --offline
lam --root D:\ResearchLibrary normalize-records --apply
lam --root D:\ResearchLibrary search --pmid 34265844 --dry-run
lam --root D:\ResearchLibrary search --paper-uuid 12345678-1234-4234-9234-123456789abc
lam --root D:\ResearchLibrary search --normalize-existing --max-records 25
lam --root D:\ResearchLibrary search --doi 10.1000/example --offline --no-cache-write
lam --root D:\ResearchLibrary search --arxiv-id 1706.03762 --download
lam --root D:\ResearchLibrary file --dry-run
lam --root D:\ResearchLibrary --json file
lam --root D:\ResearchLibrary migrate-topics --dry-run
lam --root D:\ResearchLibrary migrate-topics --apply
lam --root D:\ResearchLibrary migrate-documents --dry-run
lam --root D:\ResearchLibrary migrate-documents --apply
lam --root D:\ResearchLibrary migrate-identifiers --dry-run
lam --root D:\ResearchLibrary migrate-identifiers --apply
lam --root D:\ResearchLibrary cleanup --dry-run
lam --root D:\ResearchLibrary cleanup --apply
lam --root D:\ResearchLibrary --json commands
lam --root D:\ResearchLibrary --caller agent --json check
```

Provider-capable commands (`register`, `search`, and `normalize-records`)
support `--offline`, `--refresh`, and `--no-cache-write`. `--offline` performs
no provider requests. `--no-cache-write` suppresses metadata-cache entries and
persistent quota counters. A provider dry run may still use the network unless
`--offline` is selected; OCR cache writes remain a separate policy and are
disabled by registration dry runs.

PDF transfer is separately opt-in through `search --download`. Eligible links
are limited to official arXiv PDF links and explicit Unpaywall `url_for_pdf`
locations. Downloads are streamed to `.library_state/tmp`, validated as a
readable PDF with a matching DOI or arXiv identifier, and committed without
overwrite to `Inbox/`. Query parameters are removed from reports. A download
dry run selects and reports a plan but does not request the PDF or create a
temporary file. Use `--max-download-size MB`, `--download-timeout SECONDS`, or
`--download-source {auto,arxiv,unpaywall}` to apply narrower bounds.

Version 0.5.2 uses immutable, non-empty `paper_uuid` values as the only internal
paper key. PMID, DOI, and arXiv ID remain ordinary external-identifier columns.
`Documents` is the only file-state table; Catalogue no longer contains `id`,
`record_uid`, `pdf_status`, `pdf_filename`, or `pdf_relative_path`. Upgrade an
older workbook with `migrate-identifiers --dry-run` and then `--apply`. The
apply command validates legacy identities and Documents foreign keys, creates
a backup, atomically rewrites both schemas, and runs one final check.

After migration, Workflow 3 recognizes UUID-bound supplementary names such as
`<paper_uuid>__table01.xlsx` and exact same-stem groups such as
`paper.pdf` plus `paper_supp1.pdf`. Supplementary registration does not call
Workflow 2 and does not parse spreadsheet content. It checks SHA-256,
document ID, type/sequence, and target collisions, then writes a normalized
filename and a file-level uncertainty only in `Documents`. Workflow 4 moves
all Documents belonging to a paper together, including PDF, XLSX, XLS, and
CSV files.

Workflow 3 uses progressive identification and retries provisional rows by
their stable `paper_uuid` plus the saved Inbox blocker fingerprint.
Filename evidence is followed by bounded `pypdf`, Workflow 2, a second local
completeness assessment, and—when necessary—first-page EasyOCR. Provider,
catalogue, PDF, OCR, and confirmation
evidence are merged before the durable identity check, so a provider response
does not need to repeat fields already established locally. Conservative
first-page metadata extraction may fill blank title, authors, year, journal,
DOI, and explicitly bounded English or Chinese abstract cells after provider
failure; it never overwrites populated fields. Common journal-name
variants are recorded as notes instead of identity conflicts.

Every processed unresolved PDF receives a stable provisional `paper_uuid` and
at most one active paper-identity review blocker, but no Documents row until
identity is confirmed. The file remains unchanged in `Inbox/`; its retry link
is retained in the machine blocker state.
`USER_CONFIRMED`, `USER_CONFIRMED:`, and field-specific confirmation forms are
recognized; an empty confirmed value still clears the corresponding blocker.
Deleting a snapshotted machine blocker authorizes one retry with the same
evidence and does not immediately recreate it. Materially new evidence may
raise one new blocker. A later durable identity upgrades the same row rather
than creating another one. `--skip-pdf-text` and `--filename-only` disable
page-text extraction and OCR but still permit catalogue/filename matching and
provisional recording.

For Chinese and bilingual first pages, LAM distinguishes the body primary
title, local-language title, translated title, and English search title. It can
extract bilingual author blocks, journal headers, DOI/year, and explicit
`Abstract`/`摘要` sections. English abstracts are preferred; a bounded Chinese
abstract is retained when no English section exists. Volume/issue headers,
page numbers, ISSN, DOI-only lines, and section labels are excluded from title
queries. When Workflow 2 returns no usable record and local authors, journal,
abstract, or English title are still absent, OCR is reconsidered even if a DOI
or high-confidence title was already found.

Every Catalogue row has one immutable UUID `paper_uuid`; external identifiers
are stored only in `pmid`, `doi`, and `arxiv_id`. After an exact provider match,
one canonicalization step normalizes provider fields, official journal title/abbreviation, and the
single primary `source` (`pubmed`, `arxiv`, `unpaywall`, or `local_pdf`). Field
provenance remains in reports and caches rather than accumulating in `source`.

Workflow 2 can revisit registered records without scanning or moving their
PDFs. `search --incomplete-records` selects identifier-backed rows missing
metadata; `search --normalize-existing` performs exact-identifier
canonicalization. `normalize-records` is the migration entry point: dry-run
previews Catalogue changes, while apply mode updates canonical metadata and
sources and runs one final check. It never invokes
Workflow 4 or moves PDFs. Filename changes implied by canonical metadata are
reported as a separate plan only.

`lam doctor` checks pdf2image, Poppler, EasyOCR, Torch/CUDA, and temporary
directory access without initializing a Reader, modifying the model directory,
or using the network. Explicit model initialization is separately authorized by
`lam doctor --initialize-ocr-models`; its report states
`uses_network=true` and `may_download_models=true`. Workflow 3 never starts an
implicit model download.

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
HTTP_USER_AGENT=LAM/0.5.4
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

A dry run previews business-state changes. It may write a report, debug log,
sanitized invocation record, and a short-lived Catalogue preflight probe under
the library; it does not commit the Catalogue, managed files, official
snapshots, operation journals, or `library_changes.md`.

Exit codes are `0` for success, `2` only for completed work with review items,
`3` for no changes, `10` for parser/configuration/lock errors, `20` for catalogue errors,
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
