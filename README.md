# LAM — Local Archives Manager

LAM provides deterministic local maintenance for a biomedical literature
library stored alongside the source repository. Version 0.3.0 implements
Workflow 1 (local reconciliation), Workflow 2 (network metadata lookup),
Workflow 3 (Inbox identification and registration), and Workflow 4 (filing by
the user-controlled `topic_folder`).
It never accesses `summary.md`.

Workflow 2 queries PubMed, arXiv, and Unpaywall through synchronous,
provider-limited clients. Responses are versioned and cached under
`.library_state/metadata_cache`; exact identifiers take priority over titles,
and conflicting or ambiguous identities are never written silently.

## Install

From the repository root, using the `lam_agent` Conda environment:

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
lam search --root D:\ResearchLibrary --pmid 34265844 --dry-run
lam search --root D:\ResearchLibrary --doi 10.1038/s41586-021-03819-2 --dry-run
lam search --root D:\ResearchLibrary --arxiv-id 1706.03762 --dry-run
lam search --root D:\ResearchLibrary --row 25
lam search --root D:\ResearchLibrary --missing-metadata --max-records 25
lam search --root D:\ResearchLibrary --doi 10.1000/example --offline
lam file --root D:\ResearchLibrary --dry-run
lam file --root D:\ResearchLibrary --json
```

`search --dry-run` performs real provider queries and may update the metadata
cache, but does not modify the catalogue, snapshots, files, change log, or
operation journal. Add `--no-cache-write` for a fully read-only query, or
`--offline` to use only valid cached responses.

## Network configuration

Copy `LAM_tools/.env.example` to `LAM_tools/.env` and configure at least:

```text
NCBI_EMAIL=you@example.org
NCBI_TOOL=LAM
NCBI_API_KEY=
UNPAYWALL_EMAIL=you@example.org
HTTP_USER_AGENT=LAM/0.3.0
```

LAM never prints these values. PubMed uses a minimum interval of 0.36 seconds
without an API key and 0.11 seconds with a key; arXiv uses one synchronous
connection and at least 3.2 seconds between requests; Unpaywall uses a local
0.25-second interval and a persistent daily counter. Temporary failures use
bounded retries and `Retry-After` when supplied.

All commands also work as `python -m lam ...`. `register` processes only direct
PDF children of `Inbox/`, moves successful high-confidence matches only to
`Registered/`, writes a recoverable operation journal, and runs one final
Workflow 1 check. It never runs Workflow 4 automatically.

A dry run may write a report and
debug log under `.library_state`, but it does not modify the catalogue, managed
PDFs, official snapshots, operation journals, or `library_changes.md`.

Exit codes are `0` for success, `2` for completed work with review items, `3`
for no changes, `10` for configuration errors, `20` for catalogue errors,
`30` for file-operation failures, and `40` for provider/network failures.

## Test

```powershell
python -m pytest
python -m pytest -m live
```

Tests use temporary fixture libraries and do not touch the real catalogue or
PDF collection. Live provider tests are excluded by default and make only a
few exact, public queries when explicitly selected.

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
