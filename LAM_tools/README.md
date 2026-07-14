# LAM tools

LAM (Local Archives Manager) provides deterministic local maintenance for the
biomedical literature library in the parent directory. It implements Workflow 1
(local catalogue/file reconciliation), Workflow 3 (Inbox identification and
registration), and Workflow 4 (filing by the user-controlled `topic_folder`).
It never accesses `summary.md`.

Phase 2 uses `pypdf` to inspect at most a small configured page sample. It does
not perform OCR or network metadata queries. When local evidence is insufficient,
the unavailable Workflow 2 provider produces a stable review item instead of
inventing metadata.

## Install

From the `lam_agent` Conda environment:

```powershell
conda activate lam_agent
python -m pip install -e ".[dev]"
```

## Commands

```powershell
lam check --root D:\ResearchLibrary --dry-run
lam check --root D:\ResearchLibrary --json
lam register --root D:\ResearchLibrary --dry-run
lam register --root D:\ResearchLibrary --max-files 5
lam register --root D:\ResearchLibrary --filename-only
lam file --root D:\ResearchLibrary --dry-run
lam file --root D:\ResearchLibrary --json
```

All commands also work as `python -m lam ...`. `register` processes only direct
PDF children of `Inbox/`, moves successful high-confidence matches only to
`Registered/`, writes a recoverable operation journal, and runs one final
Workflow 1 check. It never runs Workflow 4 automatically.

A dry run may write a report and
debug log under `.library_state`, but it does not modify the catalogue, managed
PDFs, official snapshots, operation journals, or `library_changes.md`.

Exit codes are `0` for success, `2` for completed work with review items, `3`
for no changes, `10` for configuration errors, `20` for catalogue errors, and
`30` for file-operation failures.

## Test

```powershell
python -m pytest
```

Tests use temporary fixture libraries and do not touch the real catalogue or
PDF collection.
