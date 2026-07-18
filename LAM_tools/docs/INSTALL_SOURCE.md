# Install LAM from source

LAM 0.6.1 uses Python 3.14. Run development and tests as an ordinary user.
Administrator privileges, ACL changes, and installation inside a real research
library are neither required nor supported.

## 1. Install Miniforge

Install the current Miniforge distribution for the local platform from its
official project release. Open a fresh Miniforge Prompt or initialize Conda for
PowerShell after installation.

## 2. Create the environment

From the standalone `LAM_tools` source directory:

```powershell
conda env create -f environment.yml
conda activate lam-dev
python --version
```

The version must be Python 3.14.x. `environment.yml` installs the package in
editable mode with its development dependencies. To refresh an existing
environment:

```powershell
conda env update -f environment.yml --prune
python -m pip install -e ".[dev]"
```

## 3. Isolated doctor and source smoke test

Never point a smoke test at the user's real library. Create a disposable
library below the operating-system temporary directory:

```powershell
$smoke = Join-Path $env:TEMP "lam-source-smoke"
if (Test-Path $smoke) { throw "Choose a new empty smoke-test path: $smoke" }
lam --root $smoke init --apply
lam --root $smoke doctor --json
lam --root $smoke status library --json
lam --root $smoke check --dry-run --json
```

Success means the commands return documented exit codes, `doctor` reports the
expected Python/Poppler capabilities, and no path refers to the real library.
Delete the disposable directory manually after inspection if desired.

## 4. Run tests

```powershell
python -m pytest
```

The default marker expression excludes live provider, live download, live OCR,
and other network-dependent tests. Test safety guards reject real library roots,
mask `LIBRARY_ROOT`, and require isolated pytest temporary directories. Run the
suite with normal permissions; do not use an elevated terminal.

The public corpus is documented in `docs/TEST_CORPUS.md`. Its `downloaded/`
directory is optional and is never required by the default offline test suite.

## Troubleshooting

- If `lam` is not found, reactivate `lam-dev` and repeat
  `python -m pip install -e ".[dev]"`.
- If Poppler is missing, update the Conda environment; it is declared in
  `environment.yml`.
- Do not solve temporary-directory errors by changing ACLs or running as
  administrator. Choose a normal writable temporary location instead.
- `lam doctor` may create runtime diagnostics in the root passed to `--root`;
  this is why the tutorial always uses the disposable smoke library.
