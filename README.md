# LAM — Local Archives Manager

LAM is a deterministic local manager for a research literature library.
Version 0.6.0 starts the stable public-source release line, separates source
code from user library data, and preserves the 0.5.9 temporary-workspace and
reference-text safety contracts.
It never reads or modifies `summary.md`.

```text
ResearchLibrary/
|-- Inbox/          # unidentified or incomplete files
|-- Registered/     # registered files awaiting topic filing
|-- Topics/         # final topic namespace
|-- Imports/        # completed reference-text batches (not Documents)
|-- catalogue.xlsx  # Catalogue metadata + Documents file state
`-- .library_state/ # derived snapshots, cache, reports, journals and invocations
```

`Catalogue.paper_uuid` is the immutable internal paper key. PMID, DOI and arXiv
ID are external identifiers only. `Documents` is the sole source for file name,
path, hash, status and file-level uncertainty. `topic_folder` is relative to
`Topics/`; `Documents.relative_path` includes the leading `Topics/` component.

## Install

From the repository root in the `lam_agent` Conda environment:

```powershell
conda activate lam_agent
python -m pip install -e ".[dev]"
```

## Public commands

This table is generated from the same registry returned by
`lam status commands --json` and `lam commands --json`:

| Command | Category | Purpose | Dry run | Network |
|---|---|---|---:|---:|
| `lam init` | setup | Initialize a new empty LAM library | yes | no |
| `lam check` | daily | Reconcile Catalogue and managed file state | yes | no |
| `lam register` | daily | Register Inbox PDFs, supplements, and reference text | yes | yes |
| `lam search` | daily | Query providers and optionally update, normalize, or download records | yes | yes |
| `lam file` | daily | File or refile registered Documents under Topics/ | yes | no |
| `lam export` | export | Export registered citations for Zotero without modifying the library | yes | yes |
| `lam review` | maintenance | Recheck and clear objectively resolved machine blockers | yes | yes |
| `lam status` | diagnostic | Inspect library, environment, commands, recovery, or configuration | no | yes |
| `lam recover` | maintenance | Recover interrupted operations and unambiguous record bindings | yes | yes |
| `lam migrate` | migration | Upgrade identifiers/Documents schema or legacy Topics layout | yes | no |
| `lam cleanup` | maintenance | Apply allowlisted generated-file retention | yes | no |
| `lam doctor` | diagnostic | Alias for status environment | no | yes |
| `lam commands` | diagnostic | Alias for status commands | no | no |

`doctor` maps to `status environment`; `commands` maps to `status commands`.
Each alias uses one RunContext and writes one invocation. JSON preserves both
the invoked `command` and its `canonical_command`.

## Initialize a library

Initialization requires an absent or demonstrably empty target and an explicit
mode. It never merges or overwrites a workbook, contacts a provider, reads a
PDF, or initializes OCR.

```powershell
lam --root D:\NewLibrary init --dry-run
lam --root D:\NewLibrary init --apply
```

Apply creates `Inbox/`, `Registered/`, `Topics/`,
`Imports/ReferenceText/Processed/`, `.library_state/`, an exact
current-schema `catalogue.xlsx`, `library_changes.md`, and a secret-free
`.env.example`, then commits an initial Workflow 1 baseline.

## Daily no-Agent workflow

The daily commands apply by default; add `--dry-run` to preview.

```powershell
lam --root D:\ResearchLibrary status environment
lam --root D:\ResearchLibrary check
lam --root D:\ResearchLibrary register --offline
lam --root D:\ResearchLibrary search --incomplete-records
lam --root D:\ResearchLibrary review --all --dry-run
lam --root D:\ResearchLibrary review --all --apply
lam --root D:\ResearchLibrary file --dry-run
lam --root D:\ResearchLibrary file
lam --root D:\ResearchLibrary status library
```

Users may leave `auto_tags` and `suggested_topic` blank. Filing depends only on
a valid user-controlled `topic_folder`; after filling that cell, run `lam file`.
Workflow 4 does not use the network, read PDFs, infer identity or rename files.

`review` only rechecks objective machine blockers. It does not write
`USER_CONFIRMED`, approve identity, change `topic_folder`, overwrite user text,
or run Workflow 4. A provider retry occurs only when `--provider` is explicitly
given.

## Status and recovery

```powershell
lam --root D:\ResearchLibrary status library
lam --root D:\ResearchLibrary status environment
lam --root D:\ResearchLibrary status commands --json
lam --root D:\ResearchLibrary status recovery
lam --root D:\ResearchLibrary status config
lam --root D:\ResearchLibrary recover --scope auto --dry-run
lam --root D:\ResearchLibrary recover --scope auto --apply
```

`status environment` is safe before initialization and does not download OCR
models unless `--initialize-ocr-models` is explicitly supplied. `status config`
shows secrets only as `configured` or `missing`. `status recovery` reports lock
state, unfinished journals, snapshot generations, backups and orphan
Inbox/Registered files.

Recovery is limited to abnormal or interrupted state. Inbox recovery re-enters
normal Workflow 3 and may use provider flags. Registered recovery reconnects
only unique evidence from journals, hashes and names. Filed documents are never
re-registered, queried, parsed or renamed. Historical mixed publication types
are repaired only when that anomaly is detected.

## Search, providers and downloads

```powershell
lam --root D:\ResearchLibrary search --pmid 34265844 --dry-run
lam --root D:\ResearchLibrary search --paper-uuid UUID --normalize-existing
lam --root D:\ResearchLibrary search --normalize-existing --max-records 1000
lam --root D:\ResearchLibrary search --doi 10.1000/example --offline --no-cache-write
lam --root D:\ResearchLibrary search --arxiv-id 1706.03762 --download
```

Provider-capable `register`, `search`, explicit-provider `review`, and Inbox
`recover` support `--offline`, `--refresh`, and `--no-cache-write`. PubMed,
Crossref, arXiv and Unpaywall clients are synchronous, cached and rate-limited.
PMID uses PubMed; DOI uses Crossref before Unpaywall enrichment; title-only
queries use Crossref bibliographic search before applicable fallback providers.
Ambiguous or conflicting identities are not silently accepted.

PDF transfer is opt-in through `search --download`, restricted to explicit
arXiv and Unpaywall PDF locations. Downloads are streamed to managed temporary
storage, size-limited, validated and committed to `Inbox/` without overwrite.

## Reference-text registration

Ordinary `lam register` ignores `.txt` files. Enable bibliography import
explicitly:

```powershell
lam --root D:\ResearchLibrary register --reference-text auto --dry-run --json
lam --root D:\ResearchLibrary register --reference-text only --reference-file refs1.txt --json
lam --root D:\ResearchLibrary register --reference-text auto --download-missing --json
```

`auto` processes recognized reference lists alongside PDFs; `only` processes
only selected or discovered `.txt` batches. Numbered, bulleted, blank-line and
soft-wrapped references are normalized and segmented before PMID, DOI, arXiv or
supported-title provider lookup. Ambiguous, invalid and not-found entries never
create speculative rows. Batch and existing-Catalogue deduplication use PMID,
DOI, arXiv ID, then title/first-author/year. The source `.txt` never creates a
Documents row or enters Workflow 4.

Completed batches move opaquely to `Imports/ReferenceText/Processed/`; partial
batches remain in `Inbox/` and use a SHA-256 receipt so a rerun retries only
unresolved candidates. `--download-missing` is opt-in. Verified arXiv,
Unpaywall, or Crossref member-submitted PDF links are validated and committed
directly to `Registered/` with a Documents row. Download absence is a warning
unless `--require-download` is supplied; identity mismatch always requires
review.

Workflow 3 progressively uses filename/PDF metadata, bounded pypdf text and a
title or identifier lookup before OCR. Native PDF and EasyOCR implementations
now conform to one document-analysis request/result protocol; future layout or
vision backends can be added without changing Workflow 3. No new large model is
installed in the current release line.

All local candidates are classified as `trusted`, `usable`, `weak`, or
`rejected`. Viewer/publisher navigation, contaminated PDF metadata, truncated
DOIs, URL-like titles, and layout headers are rejected before provider lookup
or conflict evaluation. Adjacent title lines and DOI/URL fragments may be
reconstructed within bounded regions; line-end hyphenation is repaired without
joining ordinary words. A DOI must have a complete `10.<registrant>/<suffix>`
structure and reasonable length. A prefix such as `10.1016/j` is auxiliary
evidence only and cannot be queried, written to Catalogue, or establish identity.

If identity remains unconfirmed, screenshot-like files still use bounded
first-page metadata regions rather than full-page or full-document OCR.
Corrected or reconstructed OCR DOI candidates require provider verification.
The provisional Catalogue row now has a corresponding `Documents` row pointing
to the unchanged Inbox file with `file_status=inbox`; this tracks the physical
file without claiming its identity and prevents duplicate unmatched reports.
Supplementary registration never calls Workflow 2.

## Zotero-compatible citation export

Citation export requires an explicit target and mode. It writes regenerable
artifacts under `Exports/Zotero/` by default, but never changes Catalogue,
Documents, PDFs, Zotero databases, or topic paths and does not run Workflow 1.

```powershell
lam --root D:\ResearchLibrary export zotero --all --dry-run
lam --root D:\ResearchLibrary export zotero --all --apply
lam --root D:\ResearchLibrary export zotero --paper-uuid UUID --apply
lam --root D:\ResearchLibrary export zotero --topic-folder "Topic A" --apply
lam --root D:\ResearchLibrary export zotero --all --format pubmed-xml --apply
```

Records with PMID use validated PubMed EFetch MEDLINE/NBIB (or official XML).
Records without PMID may use a clearly marked `DB - LAM` / `OWN - LAM` local
NBIB only when title, author, year and journal are complete. `--official-only`
skips local records. `--offline`, `--refresh`, and `--no-cache-write` control a
dedicated citation-response cache without changing metadata provider caches.
Existing non-LAM output files are never overwritten.

## Migration and cleanup

Maintenance and migration commands require exactly one of `--dry-run` and
`--apply`.

```powershell
lam --root D:\ResearchLibrary migrate identifiers --dry-run
lam --root D:\ResearchLibrary migrate identifiers --apply
lam --root D:\ResearchLibrary migrate topics --dry-run
lam --root D:\ResearchLibrary migrate topics --apply
lam --root D:\ResearchLibrary cleanup --dry-run
lam --root D:\ResearchLibrary cleanup --apply
lam --root D:\ResearchLibrary cleanup --dry-run --include-test-artifacts
```

`migrate identifiers` detects current, supported legacy, and unknown/future
schemas. A current workbook returns `no_changes`; an unknown or future layout
is refused. Documents migration is an internal stage of this command.
`migrate topics` only upgrades confirmed legacy root topic directories.

Cleanup only applies documented retention to allowlisted machine artifacts. It
never selects PDFs, `catalogue.xlsx`, project instructions, `summary.md`, user
notes, topic folders, unfinished journals, or paths outside maintenance roots.
Manifested production workspaces are cleaned immediately after closed resources;
retained debug artifacts carry an expiry. Historical strict `pytest-*` roots
are only eligible with `--include-test-artifacts`; cleanup never changes ACLs or
takes ownership, and reports unreadable candidates explicitly. `status library
--json` reports temporary directory/file/byte counts, expiry, unknown and
unreadable artifacts.

## Configuration

Copy the generated `.env.example` or `LAM_tools/.env.example` and configure:

```text
NCBI_EMAIL=you@example.org
NCBI_TOOL=LAM
NCBI_API_KEY=
UNPAYWALL_EMAIL=you@example.org
CROSSREF_ENABLED=true
CROSSREF_EMAIL=you@example.org
CROSSREF_MIN_INTERVAL_SECONDS=1.0
CROSSREF_MAX_RESULTS=10
HTTP_USER_AGENT=LAM/0.6.0
LAM_KEEP_FAILED_TEMP=false
LAM_TEMP_RETENTION_HOURS=24
OCR_ENABLED=true
OCR_LANGUAGES=en
OCR_DPI=250
OCR_GPU=auto
OCR_DOWNLOAD_ENABLED=false
POPPLER_PATH=
OCR_MODEL_STORAGE_DIR=
DOCUMENT_ANALYSIS_BACKEND=auto
DOCUMENT_ANALYSIS_FALLBACKS=native,easyocr
DOI_MIN_SUFFIX_ALNUM=3
DOI_MAX_LENGTH=200
```

LAM never prints secret values. PubMed uses at least 0.36 seconds between
requests without an API key and 0.11 seconds with one; Crossref defaults to a
one-second interval and sends the configured contact email; arXiv uses one
synchronous connection and at least 3.2 seconds; Unpaywall uses a local
0.25-second interval and persistent daily accounting.

## CLI contract

Global options work before or after the top-level command:
`--root`, `--json`, `--verbose`, and `--caller`. Agent calls pass
`--caller agent`. Every dispatched top-level command writes one sanitized
invocation. A dry run may write runtime reports, logs and invocation records,
but does not commit workbook changes, managed files, official snapshots,
operation journals or `library_changes.md`.

Exit codes are `0` success, `2` completed with review items, `3` no changes,
`10` parser/configuration/lock error, `20` catalogue error, `30` file-operation
failure and `40` provider/network failure.

## Test

```powershell
python -m pytest
python -m pytest -m live
python -m pytest -m live_download
python -m pytest -m ocr_live
```

Ordinary tests use temporary libraries and do not touch the real Catalogue or
PDF collection. Session startup rejects a basetemp below the project/real
library, masks `LIBRARY_ROOT`, refuses implicit Settings roots in test mode and
terminates elevated Windows runs unless explicitly overridden. Live tests are
excluded unless explicitly selected.

## Stable public surface

The command registry/help, JSON envelope, statuses/exit codes,
Catalogue/Documents schema, `.env.example`, Workflow 1–4 rules, `lam init`
layout and reference-text import behavior are public 0.6.0 contracts. Provider
classes, cache formats, OCR images, temporary layout details, test helpers and
private debug reports remain internal.
