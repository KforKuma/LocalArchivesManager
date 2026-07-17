# Workflows.md

## Shared definitions

This file defines the operational workflows for the research library.

The global safety, authorization, field-ownership, and `uncertainty` rules in `AGENTS.md` apply to every workflow.

---

## Managed file lifecycle

```text
new or manually downloaded main PDF / supplementary document
        ↓
      Inbox/
        ↓
Workflow 3: identify, bind, register, rename
        ↓
   Registered/
        ↓
manual review of catalogue.xlsx
        ↓
Workflow 4: file by topic_folder
        ↓
   Topics/<topic_folder>/
```

Since 0.5.2, `Catalogue` is the paper table and `Documents` is the physical
file table. Every paper has an immutable `paper_uuid`; every file has a stable
`document_id`. Main and supplementary PDF/XLSX/XLS/CSV files share the same
paper UUID. `paper_uuid` is the sole internal paper key; file names, locations,
hashes, and lifecycle state exist only in `Documents`.

Upgrade an existing workbook with:

```powershell
lam --root D:\ResearchLibrary migrate identifiers --dry-run
lam --root D:\ResearchLibrary migrate identifiers --apply
```

Modifying commands run Catalogue preflight before network/PDF/OCR work and
repeat the concurrency check before commit. Workflow 3 classifies UUID and
same-stem supplementary files before the main-PDF identification chain.
Workflow 1 reconciles each managed file against `Documents`; Workflow 4 moves
all Documents sharing a paper UUID in one group.

Directory meanings:

- `Inbox/`: identification or registration is incomplete.
- `Registered/`: identification and standard naming are complete, but final filing is incomplete.
- `Topics/`: the only parent for final locations controlled by `topic_folder`.

Recommended `Documents.file_status` values:

```text
not_downloaded
inbox
registered
filed
missing
unclear
```

Current location fields in `Documents`:

```text
filename
relative_path
file_status
```

Interpretation:

- `filename`: current filename only.
- `relative_path`: currently observed library-relative path.
- `Catalogue.topic_folder`: user-confirmed intended path relative to `Topics/`; it must
  not contain a leading `Topics/`, an absolute path, `..`, hidden components,
  or reserved directory names. Limited nesting is allowed.

Older `id`, `record_uid`, and `pdf_*` columns are accepted only by the explicit
identifier migration. Ordinary workflows require the strict 0.5.2 schema.

---

## Workbook schema

`Catalogue` contains exactly one row per paper:

```text
paper_uuid
uncertainty
title
authors
year
journal
journal_abbrev
publication_type
abstract
keywords
manual_tags
auto_tags
suggested_topic
topic_folder
source
date_added
date_updated
notes
doi
pmid
arxiv_id
```

`Documents` contains exactly one row per managed physical file:

```text
document_id
paper_uuid
uncertainty
document_type
supplementary_type
sequence
filename
relative_path
extension
sha256
file_status
source
date_added
date_updated
```

`paper_uuid` is the immutable internal paper identity. PMID, DOI, and arXiv ID
are external identifiers only. A paper has at most one `main` document and may
have multiple `supplementary` documents. `document_id` is stable and unique.

Legacy columns are accepted only while `migrate identifiers` plans or applies
the upgrade. Apply validates or recovers each UUID, reconciles old PDF fields
against Documents and the observed filesystem, validates every foreign key,
then removes all non-schema columns only after the checks succeed. A
`record_uid`/`paper_uuid` disagreement blocks the whole migration. Dry-run does
not modify the workbook; apply creates a backup, saves atomically, journals the
operation, and runs Workflow 1 once in final-check mode.

Duplicate paper detection order is:

```text
PMID exact > DOI exact > arXiv exact > high-confidence title + author/year
```

Exact file duplication is determined by SHA-256. Confirmed duplicates remain
in `Inbox/`, create no new paper or document row, and are reported as one
file-level blocker without contaminating paper-level uncertainty.

---

## Shared execution rules

1. A user request to run a workflow authorizes its routine reversible actions.
2. Nested workflows inherit authorization from the top-level workflow.
3. Do not request per-file confirmation for high-confidence actions within the requested workflow.
4. Continue processing unrelated high-confidence items when one item requires review.
5. A top-level modifying workflow runs Workflow 1 once in final-check mode after completion.
6. Nested workflows do not run their own final checks.
7. A dry run or preview does not commit business state; it may write runtime
   reports, logs, sanitized invocation records, and a temporary preflight probe.
8. All modifications must be backed up where required and recorded in `library_changes.md`.

Production temporary artifacts use one manifested `RunWorkspace` below
`.library_state/tmp/`. Every workspace contains `.lam-temp.json` with its run,
workflow, creator, artifact type, cleanup policy and status. Callers close PDF
readers, streams and detached PIL images before bounded cleanup retries. Success,
dry-run and ordinary failure clean immediately; explicit debug retention records
an expiry. pytest basetemp is never allowed below the project or real library.

---

# Workflow 1: Daily catalogue and file-state check

## Trigger

Run this workflow:

- when the user explicitly requests a routine check, reconciliation, or status scan;
- once after a top-level modifying workflow finishes.

Do not run it recursively.

---

## Purpose

1. Reconcile the intended state in `catalogue.xlsx` with the observed filesystem state.
2. Confirm whether registered PDFs exist and where they currently reside.
3. Detect newly added, missing, moved, renamed, or unexpectedly modified files.
4. Update objective PDF location and status fields.
5. Identify catalogue rows that require metadata completion.
6. Maintain `.library_state/` so unchanged files do not require repeated inspection.
7. Produce a concise difference report.

The main focus is file existence and location. Do not repeatedly read unchanged PDF content.

---

## Machine state

Use:

```text
.library_state/
├── catalogue_snapshot.json
├── file_manifest.json
└── last_diff.json
```

Definitions:

- `catalogue_snapshot.json`: the last accepted machine-readable snapshot of relevant catalogue fields.
- `file_manifest.json`: the last accepted machine-readable inventory of managed files.
- `last_diff.json`: differences detected during the latest check.

`.library_state/` is excluded from ordinary library scanning.

Also exclude temporary and backup artifacts unless explicitly auditing them, including:

```text
~$*.xlsx
*.tmp
catalogue.backup.*.xlsx
summary.backup.*.md
__pycache__/
.git/
```

Never inspect `summary.md`.

Root-level PDFs and unknown ordinary root directories are reported as
`unmanaged_items` and are not scanned recursively. A Catalogue-referenced PDF
in a historical root-level topic directory is reported as
`legacy_topic_location`; ordinary workflows do not move it or create new
root-level topic directories. Run `migrate topics` explicitly to convert it.

---

## Mode A: Initial reconciliation

Use this mode when either required snapshot does not exist.

1. Read `catalogue.xlsx`.
2. Scan only `Inbox/*.pdf`, `Registered/*.pdf`, and `Topics/**/*.pdf`.
3. Match Catalogue rows to observed files through `Documents.paper_uuid`.
4. Update only objectively determinable Documents fields such as:
   - `file_status`;
   - `filename`;
   - `relative_path`;
   - `date_updated`.
5. Record unresolved mismatches in `uncertainty`.
6. Report unmatched catalogue rows and unmatched files.
7. Create the initial catalogue snapshot and file manifest after reconciliation.

The absence of a prior snapshot means no historical “change” can be proven. Report the result as an initial baseline, not as “no changes”.

---

## Mode B: Incremental check

Use this mode when both snapshots exist.

1. Compare the current relevant catalogue fields with `catalogue_snapshot.json`.
2. Compare current managed files with `file_manifest.json`.
3. Detect:
   - added files;
   - missing files;
   - moved or renamed files;
   - possible content changes;
   - catalogue field changes;
   - mismatches between `topic_folder` and observed location.
4. Write the complete machine-readable result to `last_diff.json`.
5. Update objective Documents location and status fields where the result is unambiguous.
6. Add or update `NEEDS_REVIEW:` entries for unresolved discrepancies.
7. Refresh the accepted snapshots after successful completion.

Unchanged files must not be re-read or reprocessed.

### File-difference classification

Treat `quick_hash` only as a candidate hint. It must not by itself establish
paper identity or create a collision review item.

- A completed Workflow 3/4 journal entry, disappearance of the old path,
  appearance of the new path, and a matching fingerprint classify the change
  as `expected_move_or_rename`.
- A same-path size or modification-time change is `modified`.
- A hash-only one-to-one path candidate is internal
  `quick_hash_candidate`; it does not enter `NEEDS_REVIEW:`.
- Compute a full hash only when current files coexist with the same quick hash
  or a target collision otherwise needs resolution.
- Use `possible_collision` only for real coexistence, target conflicts, or
  cases where full state cannot prove the files distinct.

Known movements from the current operation journal must be classified before
generic quick-hash candidates so the final check does not re-report the
workflow's own rename or move as a collision.

---

## Metadata completion behavior

Workflow 1 identifies metadata gaps but does not automatically perform broad external searches in final-check mode.

When Workflow 1 is explicitly requested by the user:

- it may call Workflow 2 for newly discovered papers or clearly missing metadata;
- it should query only affected rows;
- it must not refresh complete records without a specific reason.

When Workflow 1 is running as the automatic final check:

- do not call external literature APIs;
- only verify the effects of the completed task and update local state.

---

## Catalogue reconciliation rules

Examples:

- Documents row expects a file but none exists:
  - set `file_status = missing`;
  - preserve intended `topic_folder`;
  - add `NEEDS_REVIEW:` if the absence is unexplained.

- Document exists in `Registered/`:
  - set `file_status = registered`;
  - update `relative_path`.

- Document exists in its confirmed topic folder:
  - set `file_status = filed`;
  - update `relative_path`.

- Document exists in a different location than `topic_folder`:
  - preserve `topic_folder`;
  - record observed Documents `relative_path`;
  - report the discrepancy;
  - do not move it unless Workflow 4 is authorized.

- A relevant `USER_CONFIRMED:` entry resolves a recurring classification question:
  - do not re-raise the same question unless materially new evidence appears.

---

## Output report

Provide:

```markdown
# Daily library check

Mode:
Catalogue rows checked:
Managed PDFs checked:

## Detected changes

Added:
Missing:
Moved or renamed:
Possible content changes:
Catalogue changes:

## Catalogue updates completed

...

## Needs user review

...

## Metadata candidates

...

State files updated:
```

When nothing changed, report only a concise no-change result and counts.

---

# Workflow 2: Provider metadata query and optional download

## Trigger

Run this workflow:

- when explicitly requested by the user;
- when called by Workflow 3 for a newly introduced or unidentified paper.

---

## Purpose

1. Search PubMed, Crossref, arXiv, or Unpaywall for paper metadata.
2. Add new catalogue records.
3. Complete missing metadata in existing records.
4. Optionally download a paper into `Inbox/`.

Metadata query is the default mode. Download is optional.

---

## Identification order

Choose the provider route from the strongest available evidence:

1. PMID → exact PubMed query.
2. DOI → exact Crossref query, followed by Unpaywall OA enrichment.
3. arXiv ID → exact arXiv query.
4. high-quality title → Crossref bibliographic query, followed by applicable
   fallback providers when Crossref does not confirm the identity.
5. fuzzy title only when author, year, or journal supplies independent support.

Crossref does not replace PubMed for biomedical PMID records. A Crossref DOI
response must return the requested DOI. A title result is accepted only when
the title is highly consistent and at least one of author, year, or journal
supports it; the first returned result is never accepted merely by rank.
Ambiguous, identifier-mismatched, or materially conflicting Crossref results
remain provisional as `crossref_query_ambiguous`,
`crossref_identifier_mismatch`, or `crossref_metadata_conflict`.

---

## Metadata update rules

The workflow may add or complete:

```text
title
authors
year
journal
journal_abbrev
doi
pmid
publication_type
abstract
keywords
auto_tags
suggested_topic
source
date_added
date_updated
```

Rules:

1. Fill blank fields when confidence is sufficient.
2. Do not overwrite `manual_tags`, `topic_folder`, `notes`, or user confirmations.
3. Do not silently overwrite non-empty bibliographic fields.
4. Record material conflicts in `uncertainty`.
5. Create or update a Documents row only when a local file is actually known.
6. Do not assign a final `topic_folder` unless the user has already supplied it.
7. Add new rows at the bottom.

After accepting a unique high-confidence provider record, canonicalize the
whole machine-owned identity before writing. PubMed journal title maps to
`journal`, ISO abbreviation maps to `journal_abbrev`, and equivalent legacy
placement is corrected without a note or blocker. `source` stores only the
current primary canonical source in this priority order:

```text
pubmed > crossref > arxiv > unpaywall > local_pdf
```

Use `--incomplete-records` to complete identifier-backed rows with missing
metadata. Use `--normalize-existing` for exact PMID/DOI/arXiv re-query and
canonicalization. These modes may update registered Catalogue rows but do not
scan or move files in `Registered/` and never invoke Workflow 4.

---

## Optional download mode

Download only when:

- the user explicitly requests downloading; or
- the calling workflow explicitly includes download authorization.

Downloaded files must enter:

```text
Inbox/
```

Eligible download candidates are restricted to:

1. the official PDF link returned by an arXiv Atom record;
2. an explicit Unpaywall `url_for_pdf` location.

Do not infer a direct PDF from a filename suffix and do not scrape a landing
page for links. Stream the response into `.library_state/tmp/<run_id>/*.part`,
enforce configured size and timeout limits, validate every redirect against
public-address rules, then require a PDF signature, a readable document with at
least one page, and a matching DOI or arXiv identifier. Commit to `Inbox/`
without overwriting an existing different file. A same-content target is
idempotent; a different-content target requires review.

Download dry runs stop after candidate selection and planning. They must not
request the PDF, create a `.part` file, modify the catalogue, write an operation
journal, or commit snapshots.

Workflow 2 must not:

- rename downloaded PDFs into their final standard filename;
- move them to `Registered/` or a topic folder;
- treat a download as completed registration.

Those actions belong to Workflow 3.

---

## API behavior

Obey the provider's published limits.

Crossref uses the existing synchronous HTTP client, retry policy, metadata
cache, and offline/refresh/no-cache-write controls. Requests send a configured
`CROSSREF_EMAIL` contact when available and otherwise retain a descriptive
User-Agent. The default minimum interval is one second. `not_found` from a
Crossref title query is a normal degradation result and is not itself a
workflow failure.

For arXiv:

1. use one connection at a time;
2. make no more than one request every 3 seconds;
3. prefer larger paginated requests;
4. do not run parallel searches;
5. do not retry immediately after failure;
6. obey `Retry-After` when provided;
7. otherwise use delays of 30, 60, and 120 seconds, then stop;
8. cache raw responses when useful;
9. do not repeat an identical query during one session without a reason.

Recommended defaults:

```text
delay_seconds >= 3.2
max_retries = 3
```

---

## Output report

```markdown
# Metadata query report

Query or source:
Date:

New records added:
Existing records completed:
Conflicts recorded:
Possible duplicates:
Downloads placed in Inbox:
Needs user review:
```

---

# Workflow 3: Register new files from Inbox

## Trigger

Run only when explicitly requested by the user.

A request to run Workflow 3 authorizes routine high-confidence renaming and movement from `Inbox/` to `Registered/`.

The implementation processes only direct, non-hidden `.pdf` children of
`Inbox/`. It gathers filename, PDF metadata, and bounded `pypdf` evidence, then
performs an identifier or high-quality title lookup before expensive OCR. It
re-evaluates local completeness after an unsuccessful provider result and may
visually inspect and OCR bounded page-1 regions. Provider, catalogue, PDF, OCR, and user
confirmation evidence are merged before the durable identity decision;
identifier conflicts remain hard blockers. Every processed unresolved PDF
receives a stable provisional catalogue row and remains in `Inbox/`.

### PDF visual classification and first-page OCR fallback

Workflow 3 requests local analysis through `DocumentAnalysisService`, not an
OCR implementation directly. Every backend implements the same bounded
`DocumentAnalysisRequest` / `DocumentAnalysisResult` contract. The installed
backends are `NativePdfBackend` and `EasyOcrRegionBackend`.

`DOCUMENT_ANALYSIS_BACKEND=auto` selects native analysis when embedded text is
sufficient and EasyOCR regions for scanned or screenshot-wrapped content.
`DOCUMENT_ANALYSIS_FALLBACKS=native,easyocr` reserves an ordered extension point;
uninstalled Docling, GROBID, layout-aware, or advanced-vision backends are not
called implicitly and return an explainable unavailable result. Version 0.5.9
adds no new large model dependency.

Workflow 3 continues to prefer PDF metadata and bounded `pypdf` text. A usable
title or identifier is submitted to Workflow 2 before deeper OCR. A confirmed
provider result skips unnecessary OCR. After Workflow 2 returns `not_found`,
`ambiguous`, an incomplete record, or provider failure, the workflow classifies
the file as `native_text_pdf`, `scanned_article_pdf`,
`screenshot_wrapped_pdf`, or `unknown_image_pdf` and reconsiders OCR when key
identity clues remain absent. An existing unverified DOI or title does not
suppress this second gate. The report reason is
`provider_not_found_local_metadata_incomplete`. `--ocr never` disables OCR,
while `--ocr always` performs one page-1 comparison. `--skip-pdf-text` and
`--filename-only` disable all page-content extraction, including OCR.

Screenshot-wrapped PDFs are recognized from little native text, near-page
images, and repeated top/bottom content across the first two or three pages.
The repeated viewer shell is excluded by a non-destructive content crop; the
footer URL region is retained separately. The original PDF is never modified
and no cleaned replacement is produced. Classification controls extraction
strategy only and never establishes paper identity.

OCR uses `pdf2image` and Poppler for rendering and EasyOCR for recognition.
Poppler, EasyOCR, or model unavailability is an OCR-specific state and must not
be reported as PDF corruption. Model downloading is disabled during ordinary
registration. Images are bounded, written only below
`.library_state/tmp/<run_id>/ocr/`, and removed after use unless debug retention
is explicitly enabled.

For scanned, screenshot-wrapped, and unknown-image files, OCR is restricted to
bounded metadata regions: journal header, title/author, article information,
DOI, abstract header, and viewer footer URL. Each region receives at most three
deterministic attempts: raw crop, 2× grayscale/autocontrast, and 2× light
sharpening or thresholding. DOI/footer regions use a bounded 3× final attempt
and a DOI-character allowlist. OCR does not recover full text, transcribe the full
abstract, parse references, tables, or every page.

OCR results preserve text boxes and confidence, are spatially ordered, and may
add title, first-author, DOI, year, journal, URL, and Supplementary candidates.
They do not lower the registration threshold: a fuzzy OCR title, a corrected
DOI without independent provider verification, or conflicting embedded-text/OCR
evidence cannot authorize a move. OCR-derived
identifiers with unique confirmation may proceed through the existing local
match or Workflow 2 verification. Successful files still move only from
`Inbox/` to `Registered/`; Workflow 4 is unchanged.

### Candidate cleaning and reconstruction

Every local title, author, journal, year, and DOI candidate uses one confidence
vocabulary:

```text
trusted > usable > weak > rejected
```

Only `trusted` and `usable` candidates enter provider queries. `weak` candidates
may support scoring but cannot create a hard conflict. `rejected` candidates are
reported with rejection reasons and otherwise ignored. PDF metadata containing
viewer/download-source text, `Anna's Archive`, publisher navigation,
`journal homepage`, `ScienceDirect`, volume/ISSN/page headers, or URLs is marked
`metadata_title_contaminated`; it does not enter canonical title selection,
provider lookup, or `pdf_text_ocr_conflict`.

Within the title region, OCR blocks are ordered by geometry and consecutive
one-to-four-line combinations are evaluated. Composition stops before authors,
affiliations, article information, or abstract headings. A line-final hyphen is
removed only when the next line begins with a lowercase continuation; ordinary
line breaks retain a word space.

DOIs are URL-decoded, stripped of known prefixes and whitespace, then validated
against `10.<4-9 digits>/<suffix>` plus centralized suffix and total-length
limits. Adjacent fragments are joined only inside one DOI/footer region and
only when subsequent fragments use the DOI suffix character set. A partial
value such as `10.1016/j` is `doi_prefix_only`: it is never queried exactly,
written to Catalogue, or used alone for identity. It may support a complete
provider DOI only after compatible title/year evidence. Corrected or merged OCR
DOIs still require Crossref, PubMed, or Unpaywall verification.

Repeated pypdf font-dictionary diagnostics such as duplicate `/Ascent`
definitions are collapsed into one `pypdf_font_dictionary_warning`. They are
non-blocking parser warnings: successful text extraction keeps the PDF
readable and Workflow 3 continues normally.

OCR cache keys include the file fingerprint, page, engine/version, languages,
DPI, preprocessing, visual classification, crop, region strategy, and
configuration version. Dry runs may read this cache but do not write it.
Reports include visual type, chrome/crop decisions, metadata-region summaries,
DOI sources and correction/verification state—not full recognized page text.

---

## Purpose

1. Identify new files in `Inbox/`.
2. Match them to existing catalogue records.
3. Call Workflow 2 when metadata is missing or no record exists.
4. Standardize filenames.
5. Update `catalogue.xlsx`.
6. Move successfully registered files to `Registered/`.
7. Present unresolved files together for review.
8. Stop at the manual catalogue review checkpoint.

---

## Processing sequence

For each candidate file:

1. Reconnect an existing provisional row by `paper_uuid` and the saved Inbox
   fingerprint, or match a confirmed row through Documents and identifiers.
2. Parse relevant `USER_CONFIRMED` instructions and filename evidence.
3. Inspect PDF metadata and bounded first-page text with `pypdf`.
4. Filter title candidates by section, position, and title quality.
5. Query Workflow 2 with identifiers, English/local titles, authors, year, and journal.
6. After an unsuccessful or incomplete provider result, re-evaluate local fields
   and classify the PDF visual type.
7. Detect repeated viewer chrome and OCR only high-value page-1 metadata regions
   when the second gate finds identity clues missing.
8. Merge filename, PDF metadata, pypdf, regional OCR, and provider evidence
   before creating a new provisional row.
9. Fill only reliable blank local fields and preserve provenance/confidence.
10. Confirm that the combined PDF-paper evidence meets the high-confidence threshold.
11. Preserve `paper_uuid` and canonicalize provider metadata, PubMed journal
    fields, publication type, and the single primary `source`.
12. Generate a safe standard filename.
13. Update Documents:
   - `file_status`;
   - `filename`;
   - `relative_path`;
   - relevant missing metadata;
   - `date_updated`;
   - `uncertainty`, when needed.
14. Rename the file.
15. Move it to `Registered/`.
16. If identity remains unconfirmed, create or update one stable provisional
    `paper_uuid` row, add one paper-identity blocker, create or update a main
    Documents row with the unchanged `Inbox/...` path and `file_status=inbox`,
    and leave the PDF unchanged in `Inbox/`. The Documents row records physical
    custody only and does not assert identity; Workflow 1 must not repeat
    `unmatched_local_document` for this tracked file.

Before applying ready operations, write a recoverable journal under
`.library_state/runs/<run_id>/operation_journal.json`. Update it through
`planned`, `file_moved`, `catalogue_committed`, and `final_check_committed`.
Dry runs do not create a formal operation journal.

Nested Workflow 2 calls do not run their own final Workflow 1 check.

---

## Registration eligibility

Move a file to `Registered/` only when:

- the paper has been identified with high confidence;
- it matches one catalogue row;
- required naming metadata is available;
- the proposed filename is safe;
- no conflicting different-content file exists at the target path.

Leave the file in `Inbox/` when:

- the title cannot be identified;
- no confident catalogue match can be established;
- multiple records remain plausible;
- the PDF is damaged or unreadable;
- the file is not a paper PDF;
- supplementary material cannot be linked to its main paper;
- a filename collision cannot be resolved safely.

For unresolved files:

- preserve the file;
- create or update a stable provisional row when no unique row is known;
- track the unchanged Inbox file in Documents without claiming identity;
- add or update one `NEEDS_REVIEW:` paper-identity blocker;
- continue processing other eligible files.

Local first-page metadata fallback may fill blank `title`, `authors`, `year`,
`journal`, `doi`, and `abstract` values. Abstracts require an explicit
`Abstract` or `摘要` section, a recognized ending boundary, and a reasonable
bounded length; English is preferred and Chinese is allowed as fallback. It
must preserve populated cells and user-controlled content, keep
`source = local_pdf`, retain `metadata_identity_unconfirmed`, and record field
source/confidence in `uncertainty`. Common short/full journal-name variants, including a base journal name
with an indexed parenthetical qualifier, are compatible evidence and receive a
machine note rather than a review blocker.

---

## PDF filename convention

Use:

```text
[Journal Abbrev], [Year](, [Publication Type]) - [Title] (- [Supplementary Material Type]).pdf
```

Examples:

```text
Nat Immunol, 2023 - Tissue-resident memory T cells in intestinal inflammation.pdf
Gut, 2024, Review - Epithelial barrier dysfunction in inflammatory bowel disease.pdf
Cell, 2022 - Single-cell atlas of human intestinal inflammation - Supplementary Tables.pdf
```

For ordinary research articles, omit publication type.

For non-research articles, use an appropriate type such as:

```text
Review
Editorial
Commentary
Letter
Perspective
Protocol
Guideline
Meta-analysis
```

---

## Filename safety

Remove or replace Windows-unsafe characters:

```text
< > : " / \ | ? *
```

Also avoid:

- line breaks;
- repeated spaces;
- trailing spaces;
- trailing periods;
- excessive punctuation.

Use plain ASCII punctuation when practical.

If the filename exceeds 180 characters, shorten the title conservatively while preserving identification.

Do not invent a journal abbreviation. Use the full journal name or record uncertainty when no reliable abbreviation is available.

---

## Filename collisions

If the proposed destination filename already exists:

- if the files are confidently identical, do not create a duplicate; reconcile the catalogue and report it;
- if contents differ, do not overwrite either file;
- add `NEEDS_REVIEW:` and leave the incoming file in `Inbox/`.

---

## Manual catalogue review checkpoint

At the end of Workflow 3:

1. summarize completed registrations;
2. list unresolved Inbox files;
3. ask the user to inspect `catalogue.xlsx`, especially:
   - `topic_folder`;
   - `manual_tags`;
   - `notes`;
   - `uncertainty`;
4. stop.

Do not automatically run Workflow 4.

Workflow 4 becomes eligible only after the user explicitly confirms that catalogue review is complete.

After Workflow 3 finishes, run Workflow 1 once in final-check mode.

---

## Output report

```markdown
# Inbox registration report

Files inspected:
Successfully registered:
Moved to Registered:
Existing catalogue rows matched:
New catalogue rows added:

## Remaining in Inbox

...

## Needs user review

...

## Manual checkpoint

Please review catalogue.xlsx before running Workflow 4.
```

---

# Workflow 3B: Reference-text import

Run only through explicit Workflow 3 options:

```text
lam register --reference-text auto --dry-run --json
lam register --reference-text auto --json
lam register --reference-text only --reference-file refs1.txt --json
```

The default is `--reference-text never`; ordinary registration ignores `.txt`.
`auto` processes recognized reference lists and Inbox documents in one top-level
run, while `only` skips PDFs. `--reference-file` may be repeated and
`--max-references` bounds provider work.

Reference text is a `reference_import_batch`, not a managed Document. LAM
normalizes Unicode and zero-width characters, recognizes numbered, bulleted and
blank-separated entries, repairs soft line wrapping and conservative
hyphenation, then preserves both raw and normalized candidate text with source
line numbers. A low-confidence note or prose file is skipped as
`plain_text_not_recognized_as_reference_list`.

Each candidate uses the strongest available route:

```text
PMID exact > DOI exact > arXiv exact > supported Crossref title > applicable fallback
```

Title results require author, year or journal support; provider rank alone is
never identity evidence. Before Catalogue mutation, deduplicate within the
batch and existing Catalogue by PMID, DOI, arXiv ID, then title + first author +
year. Valid outcomes are `registered_new`, `matched_existing`,
`metadata_updated`, `ambiguous`, `not_found`, `invalid_reference`,
`duplicate_in_batch`, and `identifier_conflict`. Text-only Catalogue records
are valid and the source `.txt` never creates a Documents row.

Receipts under `.library_state/imports/reference_text/` bind the input SHA-256
to candidate terminal states. A partial rerun skips successful candidates and
retries unresolved ones. When no ambiguous, not-found or identifier-conflict
candidate remains, the opaque source file moves without overwrite to
`Imports/ReferenceText/Processed/`; otherwise it remains in `Inbox/`.

`--download-missing` explicitly enables OA download after canonical identity is
confirmed. Eligible sources are official arXiv PDFs, explicit Unpaywall PDFs,
and Crossref member-submitted `application/pdf` links. The transfer uses a
manifested production workspace, size/redirect/content/PDF/identity/hash checks,
then commits directly to `Registered/` under the canonical filename and creates
one main Documents row. It never bypasses a paywall or authenticated resource.
No OA location and ordinary transfer failure are non-blocking warnings;
`--require-download`, identity mismatch and different-content collisions require
review. A failed download does not undo a successful Catalogue registration.

After the top-level registration completes, Workflow 1 runs exactly once.

---

# Maintenance workflow: Record normalization

Run explicitly with:

```text
lam --root D:\ResearchLibrary search --normalize-existing --max-records 1000 --dry-run
lam --root D:\ResearchLibrary search --normalize-existing --max-records 1000
```

This maintenance workflow uses existing PMID, DOI, or arXiv identifiers for
exact provider queries. Accepted records retain `paper_uuid` and receive a canonical primary `source`,
provider title/author/year fields where safe, and correct journal title and
abbreviation placement. It preserves user-controlled fields, every
`USER_CONFIRMED` line, and arbitrary user text.

Dry-run may query providers and write an ordinary report/cache entry but must
not save `catalogue.xlsx`, create a backup or operation journal, move PDFs, or
commit snapshots. Apply mode backs up the Catalogue, writes a record-UID-aware
operation journal and change-log entry, and runs Workflow 1 exactly once in
final-check mode. It never scans or moves `Registered/` or topic-folder PDFs.
If canonical naming metadata changes, report the implication only; filename
repair remains a separate explicitly requested operation.

---

# Maintenance workflow: Publication type repair

## Trigger

Run explicitly with:

```text
lam --root D:\ResearchLibrary recover --scope publication-types --dry-run
lam --root D:\ResearchLibrary recover --scope publication-types --apply
```

## Canonical publication type

Provider values and catalogue values are passed through one shared
canonicalizer. Raw provider values remain in metadata cache and provenance;
`catalogue.xlsx` stores at most one canonical special genre.

Ordinary article and indexing values, including `Journal Article`,
`journal-article`, `Research Article`, and `Research Support, ...`, map to an
empty canonical value and are omitted from filenames. Recognized special
genres include:

```text
Review
Systematic Review
Meta-analysis
Erratum
Retraction
Editorial
Commentary
Letter
Guideline
Protocol
Case Report
```

When multiple special genres are present, use the documented priority order.
Equally ranked incompatible genres create `publication_type_conflict`.
Unknown values are omitted from filenames and create
`publication_type_unrecognized`; raw provider values must not be copied into
`uncertainty`.

## Repair behavior

Dry-run reports old/new types, old/new filenames, title truncation changes,
and blockers without modifying managed files, the catalogue, snapshots,
operation journals, or the change log.

Apply mode:

1. normalizes every existing `publication_type` cell;
2. generates names from the full canonical title, truncating only the title
   portion when the complete filename would exceed 180 characters;
3. renames only direct PDF children of `Registered/`;
4. refuses different-content target collisions and revalidates source
   size/mtime immediately before each no-overwrite rename;
5. leaves topic-folder PDFs in place and reports any proposed name;
6. updates Catalogue `publication_type` and the linked Documents `filename`,
   `relative_path`, and `date_updated` while preserving user-controlled fields;
7. backs up `catalogue.xlsx`, writes a recoverable operation journal and change
   log entry, and runs Workflow 1 exactly once in final-check mode.

Repair uncertainty keys are limited to:

```text
publication_type_conflict
publication_type_unrecognized
publication_type_repair_collision
publication_type_file_missing
```

---

# Workflow 4: Catalogue-based filing

## Trigger

Run this workflow:

- when explicitly requested by the user; or
- after Workflow 3 only when the user explicitly confirms that manual catalogue review is complete.

A request to run Workflow 4 authorizes routine high-confidence file movements based on confirmed `topic_folder` values.

---

## Purpose

1. Compare intended `topic_folder` values with observed Documents `relative_path` values.
2. Move eligible Documents from `Registered/` or an existing path below `Topics/`
   into `Topics/<topic_folder>/`.
3. Update location and lifecycle fields.
4. Leave unresolved or unclassified files in place.
5. Remove the old directory below `Topics/` only when it becomes truly empty.

Workflow 4 handles location only.

Workflow 4 must never move files from `Inbox/`. Files in `Inbox/` remain under
Workflow 2/3 authority. It accepts only Catalogue-linked supported Documents in
`Registered/` or visible descendants of `Topics/`. It rejects legacy
root-level topic locations, hidden and management directories, unsupported files,
and paths outside the library root. Legacy locations are reported with
`legacy_topic_location` and must be handled by `migrate topics`.

It must not:

- query PubMed or arXiv;
- complete bibliographic metadata;
- read PDF content or repeat paper identification;
- regenerate or change the approved Documents `filename`;
- inspect or modify `summary.md`;
- infer final folders from `auto_tags` or `suggested_topic`.

---

## Filing authority

Only `topic_folder` controls final filing.

Do not use:

```text
auto_tags
suggested_topic
uncertainty
```

as substitutes for a confirmed `topic_folder`.

A `USER_CONFIRMED:` entry may explain or preserve a user's decision, but the actual target remains the value in `topic_folder`.

---

## Filing rules

For each local Document linked to a Catalogue row:

1. read `topic_folder`;
2. read the observed Documents `relative_path`;
3. determine whether movement is needed;
4. verify that the target path is safe;
5. verify that no different-content filename collision exists;
6. move the file without changing its name;
7. update Documents:
   - `file_status = filed`;
   - `filename`;
   - `relative_path`;
   - `date_updated`;
8. append the operation to `library_changes.md`.

If `topic_folder` is empty or `Unclassified`:

- leave the file in its current location;
- preserve an already filed status or set a Registered file to `registered`;
- do not create a new topic folder;
- add `NEEDS_REVIEW:` only when user input is actually required.

---

## Folder creation

A missing target folder may be created without a second confirmation only when:

- its exact relative path is already present in the user-controlled
  `topic_folder` field;
- the resolved path remains below `Topics/`;
- it does not contain traversal components or unsafe path syntax;
- it is not suspiciously similar to an existing folder in a way that suggests a typo.

Otherwise, do not create the folder. Add `NEEDS_REVIEW:` and report it.

Do not merge folders.

---

## Re-filing and old-folder cleanup

When an eligible PDF is already below `Topics/` and its current topic path
differs from `topic_folder`, classify the successful move as
`refiled_from_topic`. A successful move from `Registered/` is
`filed_from_registered`.

After a topic-to-topic move, check only the old topic directory. Remove it
with a non-recursive empty-directory operation only if it is truly empty. Keep
it when it contains `summary.md`, a hidden entry, or any other content. Never
remove `Topics/` itself, `Inbox/`, `Registered/`, a hidden/management
directory, or a path outside the root.
Record every removed empty directory in the operation journal and report.

Per-record result classifications are:

```text
filed_from_registered
refiled_from_topic
already_correct
unclassified
source_missing
target_collision
unsafe_source
unsafe_target
```

---

## Output report

```markdown
# Catalogue-based filing report

Catalogue rows checked:
Files moved:
Already correctly filed:
Unclassified files left in place:
Unsafe or missing sources skipped:
Empty old topic directories removed:

## Created folders

...

## Needs user review

...

## Failures

...
```

After Workflow 4 finishes, run Workflow 1 once in final-check mode.

---

# Maintenance workflow: Topics namespace migration

## Trigger

Run only when explicitly requested:

```text
lam --root D:\ResearchLibrary migrate topics --dry-run
lam --root D:\ResearchLibrary migrate topics --apply
```

Ordinary workflows must never trigger this structural migration implicitly.
Dry run produces a complete plan without modifying directories, Catalogue,
official snapshots, operation journals, or the change log.

## Candidate classification

The centralized root-directory policy reserves `Inbox`, `Registered`,
`Topics`, `LAM_tools`, `scripts`, `build`, `dist`, `__pycache__`, hidden and
management directories, plus names configured through
`RESERVED_ROOT_DIRECTORIES`.

An ordinary root directory is a legacy topic candidate only when Catalogue
`topic_folder` or a Documents `relative_path` references it, it contains a file
registered by Documents, or the user explicitly supplies `--include-topic`. A directory
that merely contains a PDF is not sufficient. Unknown directories are reported
without movement.

## Apply behavior and recovery

For each confirmed candidate, move the whole directory from
`Root/<topic>` to `Root/Topics/<topic>` through the file service. Moving the
directory carries `summary.md` opaquely without reading or modifying it.
Revalidate registered PDF size, mtime, and quick fingerprint immediately before
the no-overwrite directory move.

If the target exists and is empty, it may be replaced by the directory entry.
If it contains anything, block the candidate without merging or overwriting.
Update Documents `relative_path` values with the leading `Topics/`; keep a
relative `topic_folder` unchanged and normalize only the historical equivalent
`Topics/<topic>` form.

Use the global CLI lock, operation journal, atomic Catalogue backup/write,
rollback before Catalogue commit, change log, and one final Workflow 1 check.
If a prior interruption already moved the directory but did not update
Catalogue, a rerun detects `Topics/<topic>` and completes the Catalogue phase.
The final check refreshes the committed snapshot generation.

---

# Initialization, review, status, recovery, and migration

## Initialization

`lam init --dry-run|--apply` is the only public new-library initializer. It
accepts only an absent or demonstrably empty target and never merges or
overwrites an existing workbook. Apply creates the standard directories, exact
Catalogue/Documents schema, change log, and secret-free `.env.example`, then
commits one initial Workflow 1 baseline. Initialization never contacts a
provider, reads a PDF, or initializes OCR.

## Review

`lam review` requires exactly one selector (`--all`, `--paper-uuid`, or
`--document-id`) and an explicit mode. It inventories active machine blockers,
rechecks their objective conditions, and on apply removes only blockers whose
conditions are provably gone. It preserves `USER_CONFIRMED`, user free text,
`manual_tags`, `topic_folder`, and `notes`; it never approves identity or runs
Workflow 4. Provider retry is disabled unless `--provider` is supplied.

## Status

The read-only status family is:

```text
lam status library
lam status environment
lam status commands
lam status recovery
lam status config
```

`doctor` aliases `status environment`; `commands` aliases `status commands`.
Environment and command status work without an initialized workbook. Commands
status does not create a library report. Config status exposes secret fields
only as `configured` or `missing`.

## Recovery

`lam recover --dry-run|--apply` accepts scope `auto`, `workbook`, `inbox`,
`registered`, or `publication-types`. It is for unfinished or inconsistent operations, not
routine migration. Inbox scope re-enters Workflow 3 and is the only recovery
scope allowed to use provider policy. Registered scope restores a Documents
binding only from unique journal/name/hash evidence and never files it.
Already filed documents are not re-registered, parsed, queried, renamed, or
moved. Publication-type repair runs only when a historical mixed value is
detected. User-owned workbook fields are never restored wholesale from an old
backup.

## Migration

`lam migrate identifiers --dry-run|--apply` strictly classifies the workbook as
current, supported legacy, or unknown/future. Current returns `no_changes`;
unknown/future is refused. Historical Documents conversion is an internal
stage. `lam migrate topics --dry-run|--apply` retains its separate transactional
legacy-root-topic implementation.

---

# CLI execution and invocation audit

Since 0.5.4, `--root`, `--json`, `--verbose`, and `--caller` are true top-level
options. Prefer `lam --root D:\ResearchLibrary --caller agent --json check`;
the historical placement after the command remains accepted during 0.5.x.
Daily commands apply by default and use `--dry-run` for preview. Maintenance
and migration commands require exactly one of `--dry-run` or `--apply`.
Diagnostic commands expose neither flag.

Provider-capable `register`, `search`, and normalization modes share
`--offline`, `--refresh`, and `--no-cache-write`. Offline mode performs no
provider request. No-cache-write also suppresses persistent provider quota
counters. Provider-cache and OCR-cache policies are independent and reported.

---

# Zotero-compatible citation export

Run citation export only with an explicit selector and mode:

```text
lam --root D:\ResearchLibrary --caller agent export zotero --all --dry-run
lam --root D:\ResearchLibrary --caller agent export zotero --all --apply
lam --root D:\ResearchLibrary --caller agent export zotero --paper-uuid UUID --apply
lam --root D:\ResearchLibrary --caller agent export zotero --topic-folder TOPIC --apply
```

The three selectors are mutually exclusive. NBIB is the default; optional
`--format pubmed-xml` exports only official records with PMID. Registered
papers are selected through Documents, while `Exports/` remains outside all
Workflow 1–4 scans and is never added to Documents.

For PMID records, use PubMed EFetch and validate that the returned record has
the requested PMID before caching or exporting it. For records without PMID,
generate a LAM-authored NBIB only when title, at least one author, year, and
journal/publication source are available. Preserve Catalogue values, fill only
blank export-projection fields from valid exact-match provider cache entries,
and mark local output with `DB - LAM` and `OWN - LAM`.

`--offline`, `--refresh`, and `--no-cache-write` affect only provider behavior
and the dedicated `.library_state/cache/citation_export/` response cache.
Export obtains a separate export lock, validates and atomically commits output,
refuses to overwrite non-LAM content, writes one report, and does not acquire
the workbook mutation lock or run Workflow 1 final-check.

All JSON success, workflow failure, and parser failure output uses schema
version 1 with exactly one object on stdout. Exit code 2 is reserved for
`needs_review`; parser, configuration, and lock errors use 10. Every dispatched
invocation is finalized in the audit log, including caught failures. Help and
version exit before dispatch and are not audited.

`lam status environment` and its `lam doctor` alias never initialize or
download OCR models by default. Explicit `--initialize-ocr-models`
authorization reports `uses_network=true` and
`may_download_models=true`.

Every public CLI command uses one top-level `RunContext` containing the
invocation ID, caller, root, dry-run state, lock state, top-level command, and
final-check permission. Nested workflows reuse that context and its one-shot
final-check claim: they do not
acquire a second CLI lock, create another invocation entry, or independently
authorize a duplicate final check.

Agent calls pass `--caller agent`. One sanitized JSONL record is appended to
`.library_state/invocations/YYYY-MM.jsonl`; it contains command arguments,
status, exit code, report link, change counts, and duration, but never API keys,
`.env` contents, PDF/OCR text, or private reasoning.

`lam status commands --json` and its `lam commands --json` alias expose the
single public command registry used for CLI
help and documentation. CLI stdout uses the stable envelope fields
`schema_version`, `command`, `canonical_command`, `status`, `exit_code`,
`errors`, `warnings`, `report_path`, `invocation_id`, and `details`.

---

# Maintenance cleanup

## Trigger and commands

Run only when explicitly requested:

```text
lam --root D:\ResearchLibrary cleanup --dry-run
lam --root D:\ResearchLibrary cleanup --apply
lam --root D:\ResearchLibrary cleanup --dry-run --include-test-artifacts
lam --root D:\ResearchLibrary cleanup --apply --include-test-artifacts
```

Dry run reports each candidate, its reason, and estimated recoverable bytes
without deleting it. Apply mode removes only candidates selected by the same
strict allowlist and writes a maintenance report and change-log entry.

## Allowlist and retention

Eligible machine-generated artifacts are limited to:

- `catalogue.backup.*.xlsx`: keep the latest 10 and every backup from the last
  30 days;
- `.library_state/reports/`: keep the latest 200 report groups and every report
  from the last 90 days;
- `.library_state/logs/`: keep the active log and five rotated logs;
- `.library_state/runs/`: remove only successfully completed journals older
  than 30 days; never remove failed, incomplete, or unreadable journals;
- `.library_state/tmp/`: remove stale successful-task artifacts;
- expired metadata-cache entries according to their recorded TTL;
- snapshot-generation backups other than the active and immediately previous
  generations.
- expired citation-export cache entries and stale failed/temporary export
  artifacts; formal `library.nbib`, `library.pubmed.xml`, per-record outputs,
  ownership manifests, and user-selected custom output paths are retained.

Cleanup must never select a PDF, `catalogue.xlsx`, `AGENTS.md`, `Workflows.md`,
`summary.md`, ordinary topic content, symlink/reparse content, or anything
outside these explicit maintenance roots. It must not infer deletability from a
generic wildcard, and it must not recursively delete an allowlisted directory
that contains protected or unrecognized content.

Temporary entries are classified as `production_temporary_artifact`,
`failed_temporary_artifact`, `ocr_debug_artifact`, `download_partial`,
`test_temporary_artifact`, or `unknown_temporary_artifact`. New production
artifacts require `.lam-temp.json`; unknown and unreadable entries are reported,
not guessed safe. Strict historical `pytest-*` roots are only deleted with
`--include-test-artifacts`, after retention, reparse, lock and active-pytest
checks. Their contained PDFs may be treated as test fixtures. Cleanup never
changes ACLs or takes ownership; access denial is
`cleanup_candidate_unreadable`.

Cleanup reports `deleted`, `skipped`, `failed`, and `partial_success`. If some
entries were deleted before another failed, top-level status is `failed`, while
`state_committed=true` and `partial_success=true`. `status library --json`
exposes temporary directory/file/byte counts, expired, unreadable and unknown
artifacts, plus the oldest artifact timestamp.

---

# Shared uncertainty behavior

Use the `uncertainty` column as described in `AGENTS.md`.

Typical machine-generated entries include:

```text
NEEDS_REVIEW: field=paper_identity; issue=Two catalogue rows plausibly match this PDF.
NEEDS_REVIEW: field=topic_folder; issue=No final folder has been selected.
NEEDS_REVIEW: field=pdf_file; issue=Catalogue expects a PDF but no local file was found.
MACHINE_NOTE: field=journal_abbrev; issue=Full journal name retained because no reliable abbreviation was found.
RESOLVED: field=paper_identity; value=PMID 12345678; method=DOI match.
```

When a user has added:

```text
USER_CONFIRMED: field=topic_folder; value=T_cell; note=Keep this classification.
```

do not repeatedly question that decision unless materially new conflicting evidence appears.

For paper identity, the shorthand forms `USER_CONFIRMED` and
`USER_CONFIRMED:` are equivalent to a confirmation with
`field=paper_identity`. Field-specific forms may include a value, and an empty
confirmed value is still an explicit decision. Preserve the original user text.

Prefer one current concise review entry over multiple repetitive machine warnings.

Maintain at most one active machine-generated `NEEDS_REVIEW:` blocker per
catalogue row and field. While that blocker is unresolved, do not append newer
versions of the same warning. Report active blockers once in the consolidated
workflow report and continue unrelated safe rows.

A matching `USER_CONFIRMED:` entry may use an empty `value=` and still resolves
the blocker. If the user deliberately removes a previously snapshotted machine
blocker, treat that removal as a one-time decision for the same evidence; do not
immediately recreate it unless materially new evidence appears.

---

# Shared failure behavior

When an individual item fails:

1. preserve the original file;
2. preserve existing user-controlled catalogue content;
3. record the issue in `uncertainty` when a row is known;
4. continue unrelated high-confidence work;
5. consolidate failures in the final report.

Stop the entire workflow only when continuing would risk:

- overwriting content;
- broad misidentification;
- unintended mass movement;
- catalogue corruption;
- operating outside the requested scope.
