# Workflows.md

## Shared definitions

This file defines the operational workflows for the research library.

The global safety, authorization, field-ownership, and `uncertainty` rules in `AGENTS.md` apply to every workflow.

---

## Managed file lifecycle

```text
new or manually downloaded PDF
        ↓
      Inbox/
        ↓
Workflow 3: identify, register, rename
        ↓
   Registered/
        ↓
manual review of catalogue.xlsx
        ↓
Workflow 4: file by topic_folder
        ↓
   Topic folders
```

Directory meanings:

- `Inbox/`: identification or registration is incomplete.
- `Registered/`: identification and standard naming are complete, but final filing is incomplete.
- Topic folders: final locations controlled by `topic_folder`.

Recommended `pdf_status` values:

```text
not_downloaded
inbox
registered
filed
missing
unclear
```

Recommended location fields:

```text
pdf_filename
pdf_relative_path
topic_folder
```

Interpretation:

- `pdf_filename`: current filename only.
- `pdf_relative_path`: currently observed relative path.
- `topic_folder`: user-confirmed intended final folder.

If `pdf_relative_path` is missing from `catalogue.xlsx`, propose adding it before automated location maintenance.

---

## Recommended catalogue schema

The catalogue should contain one row per paper.

```text
id
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
manual_tags
suggested_topic
topic_folder
pdf_status
pdf_filename
pdf_relative_path
source
date_added
date_updated
notes
uncertainty
```

Do not remove existing columns.

Duplicate detection order:

1. PMID
2. DOI
3. exact normalized title
4. fuzzy title match with supporting metadata

A probable duplicate should update missing fields in the existing row rather than create a second row. If duplicate status remains uncertain, preserve both possibilities and add `NEEDS_REVIEW:`.

---

## Shared execution rules

1. A user request to run a workflow authorizes its routine reversible actions.
2. Nested workflows inherit authorization from the top-level workflow.
3. Do not request per-file confirmation for high-confidence actions within the requested workflow.
4. Continue processing unrelated high-confidence items when one item requires review.
5. A top-level modifying workflow runs Workflow 1 once in final-check mode after completion.
6. Nested workflows do not run their own final checks.
7. A dry run or preview produces reports only.
8. All modifications must be backed up where required and recorded in `library_changes.md`.

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

---

## Mode A: Initial reconciliation

Use this mode when either required snapshot does not exist.

1. Read `catalogue.xlsx`.
2. Scan managed PDF locations: `Inbox/`, `Registered/`, and topic folders.
3. Match catalogue rows to observed PDFs using identifiers, filename, and path information.
4. Update only objectively determinable fields such as:
   - `pdf_status`;
   - `pdf_filename`;
   - `pdf_relative_path`;
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
5. Update objective catalogue location and status fields where the result is unambiguous.
6. Add or update `NEEDS_REVIEW:` entries for unresolved discrepancies.
7. Refresh the accepted snapshots after successful completion.

Unchanged files must not be re-read or reprocessed.

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

- Catalogue row expects a PDF but none exists:
  - set `pdf_status = missing`;
  - preserve intended `topic_folder`;
  - add `NEEDS_REVIEW:` if the absence is unexplained.

- PDF exists in `Registered/`:
  - set `pdf_status = registered`;
  - update `pdf_relative_path`.

- PDF exists in its confirmed topic folder:
  - set `pdf_status = filed`;
  - update `pdf_relative_path`.

- PDF exists in a different location than `topic_folder`:
  - preserve `topic_folder`;
  - record observed `pdf_relative_path`;
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

# Workflow 2: PubMed/arXiv metadata query and optional download

## Trigger

Run this workflow:

- when explicitly requested by the user;
- when called by Workflow 3 for a newly introduced or unidentified paper.

---

## Purpose

1. Search PubMed or arXiv for paper metadata.
2. Add new catalogue records.
3. Complete missing metadata in existing records.
4. Optionally download a paper into `Inbox/`.

Metadata query is the default mode. Download is optional.

---

## Identification order

Search or match using:

1. PMID
2. DOI
3. exact title
4. title plus authors, year, or journal
5. fuzzy title only when supported by additional metadata

Do not accept a low-confidence fuzzy-title match as final.

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
5. Set `pdf_status = not_downloaded` only when no local PDF is known.
6. Do not assign a final `topic_folder` unless the user has already supplied it.
7. Add new rows at the bottom.

---

## Optional download mode

Download only when:

- the user explicitly requests downloading; or
- the calling workflow explicitly includes download authorization.

Downloaded files must enter:

```text
Inbox/
```

Workflow 2 must not:

- rename downloaded PDFs into their final standard filename;
- move them to `Registered/` or a topic folder;
- treat a download as completed registration.

Those actions belong to Workflow 3.

---

## API behavior

Obey the provider's published limits.

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

The phase-3 implementation processes only direct, non-hidden `.pdf` children
of `Inbox/`. It uses bounded `pypdf` inspection and local catalogue matching,
then calls the replaceable Workflow 2 metadata service only when local evidence
is insufficient. Provider results must be unique and high-confidence before a
new catalogue row is created or registration continues.

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

1. Inspect the filename and safe machine-readable PDF metadata.
2. If the filename already follows the standard convention, attempt direct catalogue matching.
3. Otherwise extract the article title or identifiers from the PDF.
4. Match against existing catalogue records.
5. If no adequate record exists, call Workflow 2 to search and add or complete the record.
6. Confirm that the PDF-paper match is high confidence.
7. Generate a safe standard filename.
8. Update:
   - `pdf_status`;
   - `pdf_filename`;
   - `pdf_relative_path`;
   - relevant missing metadata;
   - `date_updated`;
   - `uncertainty`, when needed.
9. Rename the file.
10. Move it to `Registered/`.

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
- add or update `NEEDS_REVIEW:` in the relevant catalogue row when possible;
- otherwise report the file as unmatched;
- continue processing other eligible files.

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

# Workflow 4: Catalogue-based filing

## Trigger

Run this workflow:

- when explicitly requested by the user; or
- after Workflow 3 only when the user explicitly confirms that manual catalogue review is complete.

A request to run Workflow 4 authorizes routine high-confidence file movements based on confirmed `topic_folder` values.

---

## Purpose

1. Compare intended `topic_folder` values with observed `pdf_relative_path` values.
2. Move eligible PDFs directly from `Registered/` into their confirmed topic folders.
3. Update location and lifecycle fields.
4. Leave unresolved or unclassified files in `Registered/`.

Workflow 4 handles location only.

Workflow 4 must never move files from `Inbox/`. Files in `Inbox/` remain under
Workflow 2/3 authority. Files already in topic folders are checked by Workflow 1
but are not moved by Workflow 4.

It must not:

- query PubMed or arXiv;
- complete bibliographic metadata;
- rename PDFs except when required to preserve the already approved standard filename during the move;
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

For each local PDF with a matched catalogue row:

1. read `topic_folder`;
2. read the observed `pdf_relative_path`;
3. determine whether movement is needed;
4. verify that the target path is safe;
5. verify that no different-content filename collision exists;
6. move the file;
7. update:
   - `pdf_status = filed`;
   - `pdf_filename`;
   - `pdf_relative_path`;
   - `date_updated`;
8. append the operation to `library_changes.md`.

If `topic_folder` is empty or `Unclassified`:

- leave the file in `Registered/`;
- set or preserve `pdf_status = registered`;
- do not create a new topic folder;
- add `NEEDS_REVIEW:` only when user input is actually required.

---

## Folder creation

A missing target folder may be created without a second confirmation only when:

- its exact name is already present in the user-controlled `topic_folder` field;
- the path is a direct safe child of the library root;
- it does not contain traversal components or unsafe path syntax;
- it is not suspiciously similar to an existing folder in a way that suggests a typo.

Otherwise, do not create the folder. Add `NEEDS_REVIEW:` and report it.

Do not merge folders.

---

## Files outside Registered

Workflow 4 must not move a file whose observed location is outside `Registered/`.
If a catalogue row points to `Inbox/`, another topic folder, or a management
directory, leave the file in place and report the discrepancy for Workflow 1 or
the appropriate registration workflow.

---

## Output report

```markdown
# Catalogue-based filing report

Catalogue rows checked:
Files moved:
Already correctly filed:
Left in Registered:
Files outside Registered skipped:

## Created folders

...

## Needs user review

...

## Failures

...
```

After Workflow 4 finishes, run Workflow 1 once in final-check mode.

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
