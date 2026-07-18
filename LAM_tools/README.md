# LAM — Local Archives Manager

This directory is the standalone source root for LAM 0.6.1 development. The
official source and frozen build baseline is Python 3.14. It is
deliberately separate from the user literature library and must never contain
`catalogue.xlsx`, managed documents, `.library_state`, or local credentials.

## Development environment

```powershell
conda env create -f environment.yml
conda activate lam-dev
python --version  # Python 3.14.x
python -m lam --version
lam commands --json
```

LAM never infers a literature library from the source or executable location.
Pass a root explicitly or configure `LIBRARY_ROOT`:

```powershell
lam --root D:\ResearchLibrary status library --json
```

The current release work is tracked in the repository-level `CHANGELOG.md`.
Canonical library behavior remains defined by `AGENTS.md` and `Workflows.md`
until the synchronized package-resource templates are added in Milestone 3.
