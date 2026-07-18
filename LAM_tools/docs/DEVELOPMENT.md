# Development guide

## Safety boundary

Develop inside `LAM_tools`, not inside a real library. Default tests must remain
offline and isolated. Never set pytest basetemp, `LIBRARY_ROOT`, fixtures, or
smoke commands to a production Catalogue. Run with ordinary user permissions
and do not change ACLs to make a test pass.

Session startup clears `LIBRARY_ROOT`, refuses a basetemp or explicit test root
inside the real library, refuses an elevated Windows token, skips project
`.env` loading, disables OCR/model downloads, and blocks network sockets for
all non-live tests. These controls have no environment-variable bypass in the
default suite.

## Typical loop

```powershell
conda activate lam-dev
python -m pip install -e ".[dev]"
python -m pytest tests/test_test_corpus_061.py
python scripts/generate_cli_docs.py --check
python scripts/sync_package_templates.py --check
python -m pytest
git diff --check
```

Use `rg` for source discovery and add regression tests beside every behavior
change. Provider/network tests remain explicitly opt-in. This tutorial does not
build PyInstaller artifacts; frozen builds belong to a separate release step.

## Corpus maintenance

1. Included material must be synthetic, explicitly redistributable, or a small
   necessary text fixture.
2. Generate PDFs with `scripts/generate_test_corpus.py`.
3. Render every changed PDF to `tmp/pdfs/` and inspect it visually.
4. Update `manifest.json` only after calculating the final SHA-256.
5. Run `tests/test_test_corpus_061.py` and then the full suite.
6. Never commit anything under `examples/test_corpus/downloaded/`.

To add an external `download_only` case, obtain the direct lawful URL and file
first, calculate its hash independently, document its license/terms and testing
purpose, then add the manifest entry. The fetch script is a reproducibility
tool, not a discovery or access-control bypass tool.

## Manifest changes

The manifest is a public compatibility artifact. New required fields require a
schema-version change. Keep `case_id` stable, filenames as plain basenames, and
expected behavior deterministic. Do not include absolute paths, timestamps,
credentials, signed URLs, cookies, or provider response bodies.

## Source validation

The minimum handoff is:

- corpus schema and hashes validate;
- included files are allowlisted and small;
- native/image text-layer expectations pass;
- generator output is reproducible;
- download failures are clear and never overwrite a file;
- default full pytest passes;
- source smoke test uses a disposable initialized library.

## Release tree

`MANIFEST.in` is an allowlist for the source distribution. It includes source,
tests, development documentation, packaging scripts, package policy templates,
reference-text fixtures, and only the four synthetic corpus PDFs. It prunes
build/dist/tmp, local reports, downloaded corpus files, local test roots and
developer staging areas, while excluding `.env`, caches, logs, partial files,
and every `summary.md`.

Before a release, run `tests/test_release_tree_061.py`, build the sdist in an
isolated temporary copy, and inspect the archive member list. Source tests and
tutorials are public development-only content; PyInstaller binary releases do
not include them.
