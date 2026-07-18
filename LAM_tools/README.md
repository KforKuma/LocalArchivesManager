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
Canonical library behavior is defined by the repository-level `AGENTS.md` and
`Workflows.md`; byte-identical package templates are shipped under
`src/lam/resources/` and checked for drift by the release tests.

## Developer documentation

- [Install from source](docs/INSTALL_SOURCE.md)
- [Public test corpus](docs/TEST_CORPUS.md)
- [Development guide](docs/DEVELOPMENT.md)
- [Generated public CLI reference](docs/CLI_COMMANDS.md)

The public synthetic fixtures and their manifest are under
`examples/test_corpus/`. Optional externally fetched files are always local and
ignored under `examples/test_corpus/downloaded/`.
