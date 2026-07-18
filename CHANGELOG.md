# Changelog

## 0.6.1 - in development

- Added `record_origin` and `document_expectation` to the Catalogue contract,
  with constrained values and full-row official snapshots for exact recovery.
- Added explicit `lam migrate schema --dry-run|--apply` support for strict
  0.6.0 workbooks. Migration preserves UUIDs and user text, infers required
  Documents and reference-text-only records, and flags unresolved legacy rows.
- Updated PDF, provider and reference-text record creation paths to establish
  provenance and document expectations at creation time.
- Bumped the package and library schema contracts to 0.6.1 while retaining the
  old strict field signature for deterministic migration detection.
- Added paper-entity `lam delete` with mandatory dry-run/apply modes, recoverable
  trash manifests, complete Catalogue/Documents capture, file rollback on
  Catalogue commit failure, and an apply prohibition for agent callers.
- Added trash listing and exact UUID/document-id restoration through `recover`,
  plus explicit age-gated trash payload purge in `cleanup` while retaining
  tombstones.
- Added a public, reproducible synthetic PDF/reference-text corpus with pinned
  hashes, a download-only fetcher, corpus regression tests, and source install,
  corpus, and development tutorials.

## 0.6.0 - in development

- Started the public-source release line with a standalone `LAM_tools`
  `pyproject.toml` and Conda development environment definition.
- Separated package, library-schema and CLI JSON-contract versions into one
  public version module and exposed all three through library status.
- Removed implicit fallback from the source directory to a real library root;
  callers must now pass `--root` or configure `LIBRARY_ROOT`.
- Added the initial PyInstaller 6 onedir spec, EasyOCR collection hook and an
  isolated Windows build script using `.build/pyinstaller/` and `dist/`.
- Made the EasyOCR availability probe frozen-aware so `lam.exe` does not try
  to relaunch itself as a Python `-c` interpreter.
- Set Python 3.14 as the official 0.6.0 source/frozen baseline and bounded
  runtime/build dependencies to compatible major versions validated by the
  local Python 3.14.6 and PyInstaller 6.21 build environment.
- Required PyInstaller hooks-contrib 2026.6 or newer within the 2026 series,
  matching PyInstaller 6.21's own declared dependency.

## 0.5.9 - 2026-07-17

- Added manifested `RunWorkspace` lifecycle management for production OCR,
  visual analysis and downloads, with detached/closed PIL images, bounded
  Windows cleanup retries, explicit debug expiry and cleanup-failure evidence.
- Extended cleanup and library status with production, failed, OCR debug,
  download, pytest, unknown and unreadable temporary-artifact classifications;
  strict historical pytest cleanup now requires `--include-test-artifacts` and
  never changes ACLs or ownership. Partial cleanup commits are explicit.
- Added pytest session guards that reject project/real-library basetemp roots,
  mask real `LIBRARY_ROOT`, prohibit implicit test Settings roots, refuse
  elevated Windows tests by default and keep live providers opt-in.
- Added conservative reference-list detection, Unicode/multiline segmentation,
  DOI/PMID/arXiv/title candidate extraction, supported provider matching,
  batch/Catalogue deduplication and SHA-256 import receipts.
- Added `register --reference-text {never,auto,only}`, repeatable
  `--reference-file`, `--max-references`, `--download-missing` and
  `--require-download`. Text inputs never create Documents and partial batches
  remain in Inbox; complete batches move to `Imports/ReferenceText/Processed/`.
- Added opt-in verified OA download from arXiv, Unpaywall and Crossref
  member-submitted PDF links directly to Registered, with canonical naming,
  identity validation and Documents creation. Non-required download failure
  does not undo successful metadata registration.
- Hardened `.gitignore`, registered deterministic pytest markers and added
  package-content, temporary lifecycle, reference parsing, receipt, download
  and existing Workflow regression coverage. Updated public docs and version.

## 0.5.8 - 2026-07-17

- Added a replaceable `DocumentAnalysisBackend` protocol with unified request,
  result, capability and candidate models; native PDF and EasyOCR region
  adapters are the only installed implementations in this release.
- Added centralized `trusted`/`usable`/`weak`/`rejected` candidate grading and
  active rejection of contaminated metadata, publisher/navigation headers,
  URL-like titles, layout noise and repeated viewer content.
- Added spatially bounded one-to-four-line OCR title composition, safe line-end
  hyphenation repair, author/abstract stopping rules and nonblocking local
  candidate disagreement semantics.
- Added centralized DOI decoding, structure/length/completeness validation,
  prefix-only classification, adjacent DOI/footer URL fragment reconstruction,
  and dedicated bounded OCR preprocessing with a DOI character allowlist.
- Prevented rejected titles and partial/corrected identifiers from entering
  provider exact queries or creating hard conflicts; DOI prefixes may only
  support a complete provider DOI after title/year agreement.
- Added provisional main Documents rows for unresolved Inbox PDFs so physical
  files remain traceable without claiming identity and final-check no longer
  repeats `unmatched_local_document` for known provisional files.
- Extended Workflow 3 reports with backend attempts, candidate/rejection counts,
  merge/repair metrics, suppressed query counts and candidate disagreements;
  invalidated pre-0.5.8 OCR candidate caches.
- Added 0.5.8 regression coverage and updated configuration, status output,
  README, Workflows and package versioning without adding model dependencies.

## 0.5.7 - 2026-07-17

- Added a cached, rate-limited Crossref provider supporting verified DOI lookup
  and conservative bibliographic title/author/year queries, with offline,
  refresh and no-cache-write behavior shared with existing providers.
- Routed DOI evidence through Crossref before Unpaywall enrichment and moved
  high-quality title lookup ahead of deep OCR without replacing PubMed as the
  biomedical PMID authority or accepting the first fuzzy result blindly.
- Added native-text, scanned, screenshot-wrapped and unknown-image PDF visual
  classification, including repeated top/bottom chrome detection and a
  non-destructive content crop for low-resolution viewer captures.
- Added bounded metadata-region OCR for title/author, article information, DOI
  and footer URL clues with at most three deterministic preprocessing attempts;
  full-document OCR remains out of scope.
- Added source-ranked DOI candidate fusion and limited OCR correction. Corrected
  candidates cannot establish durable identity until verified by a provider.
- Extended Workflow 3 JSON/report details and provisional local evidence while
  preserving Inbox safety, user fields, Supplementary isolation, Workflow 4 and
  Zotero export behavior.
- Added Crossref, PDF visual classification, regional OCR and Workflow 3
  regression coverage and updated configuration, documentation and versioning.

## 0.5.6 - 2026-07-17

- Added `lam export zotero` for explicit whole-library, single-paper, or
  topic-folder NBIB export without modifying Catalogue, Documents, PDFs, or
  Zotero state and without running Workflow 1.
- Added validated PubMed EFetch MEDLINE/NBIB and optional PubMed XML export,
  reusing NCBI rate limiting, retry policy and credentials with a dedicated
  verified response cache supporting offline, refresh, and no-cache-write.
- Added conservative LAM-authored UTF-8 NBIB generation for complete local
  records, including explicit provenance markers, multiline fields, authors,
  keywords, identifiers, abstracts and publication metadata.
- Added PMID/DOI deduplication and conflict blocking, export-only locking,
  non-LAM collision protection, atomic multi-artifact commit and ownership
  manifests under `Exports/Zotero/`.
- Integrated stale export temporary/cache cleanup, stable JSON/reporting,
  command-registry capabilities, documentation, and 0.5.6 regression tests.

## 0.5.5 - 2026-07-17

- Froze the public CLI at twelve commands and added grouped `status` and
  `migrate` subcommands with hidden 0.5.x compatibility shims.
- Added safe pure-CLI library initialization with exact Catalogue/Documents
  schema, configuration hints, and an initial Workflow 1 baseline.
- Added objective blocker review that preserves user confirmations, notes,
  tags, topic choices, and identity-approval boundaries.
- Added library, environment, command-registry, recovery, and non-sensitive
  configuration status views; `doctor` and `commands` are single-invocation
  aliases with stable JSON canonical identities.
- Added conservative scoped recovery for interrupted workbook/Inbox/Registered
  operations and detected historical publication-type anomalies.
- Consolidated identifier and topic migration under `lam migrate`; current
  schemas return `no_changes` and unknown/future schemas are refused.
- Merged public record normalization into `search --normalize-existing` while
  retaining hidden compatibility entry points for 0.5.x scripts.
- Added 0.5.5 CLI, initialization, status, review, migration and provider-policy
  regression coverage and synchronized README, Workflows, AGENTS and registry.

## 0.5.4 - 2026-07-17

- Removed automatic backup pruning from ordinary Catalogue saves and made
  explicit cleanup retain the latest 10 valid backups plus every valid backup
  from the last 30 days, while protecting unfinished-journal references.
- Retired the unsafe historical metadata writer, removed the unrelated root
  entrypoint and removed the direct EasyOCR initialization script.
- Added the versioned JSON CLI envelope for success, workflow failure and
  parser failure; parser/configuration/lock errors now use exit 10 while exit 2
  is reserved for `needs_review`.
- Finalized invocation audit records for caught failures with canonical command,
  sanitized arguments, error type, and start/completion timestamps; CLI-owned
  log handlers are closed before embedded calls return.
- Made shared root/JSON/verbosity/caller options truly top-level while retaining
  legacy placement, required explicit preview/apply for maintenance and
  migration commands, and removed invalid diagnostic mode flags from help.
- Added safe default Doctor behavior with explicit
  `--initialize-ocr-models`, unified offline/refresh/cache-write provider policy,
  and non-persistent quota checks when cache writes are disabled.
- Added schema-dirty tracking for empty-workbook migrations, removed ordinary
  workflow reads of legacy row/file identity fields, and centralized a
  one-shot final-check claim in `RunContext`.
- Expanded the command registry capability and actual-exit-code schema and
  updated current-schema CLI fixtures, documentation and regression coverage.

## 0.5.3 - 2026-07-16

- Reordered Workflow 3 local fallback to inspect pypdf text before provider
  lookup, re-evaluate local completeness after provider failure, optionally
  OCR page 1, and only then create or update a provisional Catalogue row.
- Added provider-failure OCR re-gating with
  `provider_not_found_local_metadata_incomplete`; an existing DOI or title no
  longer permanently suppresses OCR when authors, journal, abstract, or a
  usable English title remain absent.
- Added bilingual first-page extraction for primary/local-language and English
  titles, bilingual author blocks, journal headers, DOI, year, and explicitly
  bounded English or Chinese abstract sections.
- Provisional rows now immediately fill reliable blank local fields, preserve
  `source = local_pdf` and `metadata_identity_unconfirmed`, retain English
  search titles and field confidence as provenance notes, remain in Inbox, and
  do not create Documents rows before identity confirmation.
- Hardened title filtering against volume/issue headers, page numbers, ISSN,
  DOI-only lines, journal headers, and section labels.
- Collapsed repeated pypdf font-dictionary warnings into one non-blocking
  parser warning when text extraction otherwise succeeds.
- Added 0.5.3 regression coverage for bilingual extraction, abstract
  selection, OCR re-gating, user-value protection, provisional safety, warning
  handling, and the single-final-check boundary.

## 0.5.2 - 2026-07-16

- Made immutable UUID4 `paper_uuid` the sole Catalogue row identity; PMID,
  DOI, and arXiv ID are now external identifiers only.
- Removed `id`, `record_uid`, `pdf_status`, `pdf_filename`, and
  `pdf_relative_path` from the strict Catalogue schema and made Documents the
  sole file-state table.
- Added `migrate-identifiers --dry-run/--apply` with legacy UUID recovery,
  identity-conflict blocking, Documents reconciliation and foreign-key checks,
  exact column reordering, atomic backup/save, operation journaling, and one
  final check.
- Updated snapshot comparison, review state, matching, metadata targeting,
  Workflow 1, Workflow 3, Workflow 4, and reports to associate papers by
  `paper_uuid`.
- Added regression coverage for non-mutating dry runs, blocked identity
  disagreements, exact post-migration schemas, Workflow 2 UUID creation, and
  provisional Documents registration.

## 0.5.1 - 2026-07-16

- Added the `Documents` worksheet with stable `paper_uuid` and `document_id`
  linkage for one main document and multiple supplementary files per paper.
- Added `migrate-documents --dry-run/--apply`, preserving valid historical
  `record_uid` UUIDs, migrating legacy main PDF paths, calculating SHA-256,
  verifying the saved workbook, and running one final check.
- Added Catalogue preflight before modifying commands and before commit,
  including Excel lock detection, writable temporary workbook verification,
  dual-sheet schema validation, and concurrent-change detection.
- Added strict UUID and same-stem supplementary parsing, standardized names,
  file-level uncertainty, and collision checks for PDF, XLSX, XLS, and CSV.
- Updated Workflows 1, 3, and 4 to reconcile, register, and file Documents;
  supplementary registration does not use network metadata or parse table
  contents, and Workflow 4 moves all files for one paper as a group.
- Changed valid Catalogue backup retention to the most recent five while
  protecting backups referenced by unfinished operation journals.

## 0.5.0 - 2026-07-16

- Added the `Topics/` namespace and centralized root-directory classification,
  including configurable reserved names and safe nested topic paths relative
  to `Topics/`.
- Added transactional `migrate-topics` dry-run/apply modes with conservative
  candidate selection, registered-PDF fingerprints, whole-directory
  no-overwrite moves, opaque `summary.md` carriage, Catalogue backup/atomic
  update, operation journal, rollback, interruption recovery, and one final
  check.
- Restricted Workflow 1 scanning to direct Inbox/Registered PDFs and recursive
  Topics PDFs; legacy root topics and unknown root items are now reported
  without implicit structural migration.
- Updated Workflow 4 to file and refile only between Registered and Topics
  paths, including safe nested topics and empty old-directory cleanup.
- Added `RunContext`, `--caller`, monthly sanitized invocation JSONL logs, and a
  unified public JSON report envelope without duplicate nested invocations.
- Added the single-source public command registry and `lam commands --json`,
  with CLI help and README command documentation checked against the registry.
- Documented the migration procedure and CLI business audit, and expanded
  regression coverage for Topics, recovery, Agent audit, report correlation,
  locking/final-check boundaries, and legacy compatibility.

## 0.4.2 - 2026-07-15

- Reworked snapshot classification so operation-journal moves become
  `expected_move_or_rename`, same-path timestamp/size changes become
  `modified`, and quick hashes remain internal candidate evidence instead of
  creating collision review items.
- Added on-demand full-hash confirmation for coexisting duplicate candidates
  while preserving genuine target and content-collision reporting.
- Added `lam cleanup --dry-run` and `lam cleanup --apply` with strict generated
  artifact allowlists, age/count retention, protected-content checks, byte
  accounting, maintenance reports, and change logging.
- Extended Workflow 4 to refile catalogue-registered PDFs from ordinary
  top-level topic folders after user `topic_folder` changes without PDF reads,
  metadata queries, re-identification, or filename regeneration.
- Added non-recursive cleanup of only truly empty old topic directories and
  retained directories containing `summary.md`, hidden files, or other content.
- Added regression coverage for snapshot move/collision classification,
  cleanup safety and retention, Workflow 4 re-filing, target collisions, empty
  directory handling, and single final-check execution.

## 0.4.1 - 2026-07-15

- Added immutable UUID `record_uid` values and changed snapshot, review-decision,
  and operation-journal association to prefer that stable row identity while
  remaining compatible with older row-number snapshots.
- Added post-match record canonicalization with user-facing ID priority
  `PMID > DOI > ARXIV > LOCAL`, canonical provider source selection, and safe
  updates of equivalent machine metadata.
- Corrected PubMed journal placement: the official title is stored in `journal`
  and ISO abbreviation in `journal_abbrev`; equivalent legacy placement no
  longer creates a note or blocker.
- Added Workflow 2 `--incomplete-records` and `--normalize-existing` modes for
  exact-identifier completion of registered records without PDF movement.
- Added `lam normalize-records` dry-run/apply migration with Catalogue-only
  changes, backup and change logging, stable UID assignment, and one final
  Workflow 1 check.
- Added 20 focused 0.4.1 regression tests covering UID stability, snapshot and
  issue continuity, source semantics, journal placement, registered-record
  completion, dry-run safety, user confirmation preservation, Inbox scope, and
  final-check count.

## 0.4.0 - 2026-07-15

- Added tolerant `USER_CONFIRMED` parsing, including shorthand, field-specific,
  and empty-value confirmations, while preserving all user confirmation text.
- Added one-time blocker-clearance handling: removing a snapshotted machine
  review entry triggers a retry without immediately recreating the same issue;
  materially new evidence can still raise one concise blocker.
- Reordered Workflow 3 retries around confirmed identity and catalogue/PDF
  identifiers before title, filename, embedded-text, and OCR evidence.
- Added durable identity merging across provider, catalogue, PDF, and user
  confirmation evidence, with identifier conflicts remaining hard blockers.
- Added conservative first-page local metadata fallback that fills only blank
  identification fields after provider failure and never synthesizes abstract
  or keyword catalogue values.
- Added tolerant journal-name variant comparison so expanded/indexed journal
  names produce traceable machine notes instead of false identity conflicts.
- Added 32 regression tests for confirmations, blocker retry semantics,
  identifier priority, evidence merging, local metadata fallback, journal
  variants, provisional upgrades, LOCAL ID preservation, and hard conflicts.

## 0.3.4 - 2026-07-15

- Added centralized publication-type canonicalization with a special-genre
  whitelist, deterministic priority, conflict and unrecognized warnings, and
  backward-compatible handling of historical compound strings.
- Separated provider `raw_publication_types` from the single canonical
  `publication_type` written to the catalogue; PubMed provenance retains all
  source values and Unpaywall `journal-article` maps to an ordinary article.
- Hardened standard filename generation so only canonical special genres are
  included and the 180-character limit truncates only the title portion.
- Added `lam repair-publication-types` dry-run/apply modes with catalogue
  backup, direct-Registered no-overwrite renames, operation journaling,
  race checks, consolidated blockers, change logging, and one final Workflow 1
  check.
- Added publication-type, merge, provider provenance, filename, migration,
  collision, dry-run, CLI, and Workflow 3 regression coverage.

## 0.3.3 - 2026-07-15

- Reworked Workflow 3 into a filename-first progressive identification flow:
  provider lookup, bounded `pypdf`, and first-page OCR are now invoked only as
  successive evidence levels.
- Added stable `LOCAL:` provisional catalogue rows for every unresolved Inbox
  PDF; reruns update the same row and later confirmed provider metadata upgrades
  it in place while preserving user-controlled fields and confirmations.
- Added explicit per-file registered, provisional, blocked, failed, and skipped
  result states plus candidate provenance, inspection level, provider attempts,
  canonical-title selection, and title-change provenance in reports/journals.
- Added tolerant comparison views for Unicode normalization, HTML entities,
  underscores, academic Greek-letter aliases, superscript charges, punctuation,
  and common filename separators without collapsing charge-sensitive meanings.
- Added centralized inspection decisions, provider-title precedence, duplicate
  provisional protection, stable review blockers, and expanded Workflow 3,
  title-selection, idempotency, and progressive-inspection tests.

## 0.3.2 - 2026-07-14

- Added opt-in/automatic first-page OCR fallback for Workflow 3 using
  `pdf2image`, Poppler, and EasyOCR while retaining `pypdf` as the primary path.
- Added OCR availability, configuration, text-block, and inspection models;
  bounding boxes, confidence, spatial ordering, title candidates, DOI/PMID,
  year extraction, and conservative corrected-DOI candidates are preserved.
- Added configurable language, DPI, CPU/GPU mode, image-size/file-count/time
  bounds, one GPU-to-CPU fallback, process-local Reader reuse, and deterministic
  no-download model initialization.
- Added fingerprinted OCR caching, parameter invalidation, cache-read-only dry
  runs, and temporary first-page image cleanup.
- Added `lam doctor` plus `register --ocr`, `--ocr-language`, `--ocr-dpi`, and
  `--ocr-gpu` options.
- Integrated OCR evidence into local matching and Workflow 2 verification
  without weakening identity thresholds; OCR conflicts and unsupported fuzzy or
  corrected evidence remain in `Inbox/` for review.
- Added mock OCR, PdfService, Workflow 3, doctor, cache, fallback, and safety
  tests plus an opt-in `ocr_live` test.

## 0.3.1 - 2026-07-14

- Added explicit `lam search --download` support for official arXiv PDF links
  and Unpaywall locations that provide an explicit `url_for_pdf`.
- Added dry-run download planning, provider selection, configurable size and
  timeout bounds, and credential-safe URL reporting.
- Added SSRF-resistant initial and redirect URL checks, bounded redirects,
  streamed `.part` downloads, PDF signature/structure/page validation, and
  bounded DOI or arXiv identity verification.
- Added full-hash idempotency and no-overwrite commits into `Inbox/` only;
  Workflow 2 never moves downloads into `Registered/` or topic folders.
- Added catalogue `inbox` status/path updates, recoverable staged journals,
  consolidated review blockers, change logging, and one final Workflow 1 check.
- Added offline mock download tests and an opt-in `live_download` test.

## 0.3.0 - 2026-07-14

- Added Workflow 2 and the `lam search` command.
- Added PubMed ESearch/EFetch, arXiv Atom, and Unpaywall v2 providers.
- Added provider-local synchronous rate limiting, bounded retries,
  `Retry-After` handling, response-size limits, and credential-safe logging.
- Added versioned positive and negative metadata caching with offline mode.
- Added normalized metadata, field provenance, conservative multi-source
  identity checks, and blocking identifier conflicts.
- Added safe catalogue row creation and blank-field completion using the
  existing backup, atomic-save, uncertainty, change-log, journal, lock, and
  final-check mechanisms.
- Connected Workflow 3 to the composite provider service without changing
  Workflow 4 or expanding snapshot scope.
- Added offline provider, HTTP, cache, merge, Workflow 2, and Workflow 3 tests,
  plus explicitly opt-in live provider tests.

## 0.2.0 - 2026-07-14

- Added Workflow 3 Inbox PDF inspection, local matching, safe registration,
  recoverable operation journals, and the manual review checkpoint.

## 0.1.0 - 2026-07-14

- Added Workflow 1 reconciliation and Workflow 4 catalogue-based filing.
