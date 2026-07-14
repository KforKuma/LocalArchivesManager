# LAM tools

LAM (Local Archives Manager) provides deterministic local maintenance for the
biomedical literature library in the parent directory. Phase 1 implements only
Workflow 1 (local catalogue/file reconciliation) and Workflow 4 (filing by the
user-controlled `topic_folder`). It never reads PDF contents and never accesses
`summary.md`.

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
lam file --root D:\ResearchLibrary --dry-run
lam file --root D:\ResearchLibrary --json
```

Both commands also work as `python -m lam ...`. A dry run may write a report and
debug log under `.library_state`, but it does not modify the catalogue, managed
PDFs, official snapshots, or `library_changes.md`.

Exit codes are `0` for success, `2` for completed work with review items, `3`
for no changes, `10` for configuration errors, `20` for catalogue errors, and
`30` for file-operation failures.

## Test

```powershell
python -m pytest
```

Tests use temporary fixture libraries and do not touch the real catalogue or
PDF collection.

