# LAM 0.6.1 Windows frozen build report

Build date: 2026-07-18  
Target: Windows x64, PyInstaller onedir, console  
Result: **eligible for internal technical release-candidate review; not publish-ready**

## Build environment

| Component | Version / value |
|---|---|
| Operating system | Windows 11, build 10.0.26200 |
| Python | 3.14.6 (Conda, `lam_agent`) |
| PyInstaller | 6.21.0 |
| pyinstaller-hooks-contrib | 2026.6 |
| EasyOCR | 1.7.2, language `en` |
| Torch / torchvision | 2.13.0 / 0.28.0 |
| Poppler | 26.07.0, conda-forge Windows x64 |
| Clean build output | `D:\LAM_build\dist\LAM-0.6.1` |
| Release staging | `D:\LAM_build\release\LAM-0.6.1-windows-x64` |
| Source state | Current working tree; not yet a clean commit/tag |

The build and smoke-test roots are outside the real library. No system PATH,
system package, ACL, real Catalogue, real PDF, or real `.library_state` was
used or modified.

## Frozen resource behavior

- Frozen EasyOCR uses `models/easyocr`, language `en`, and forces
  `download_enabled=false`. It does not fall back to the user EasyOCR cache.
- Poppler resolution order is frozen bundle, explicit user path, then PATH.
  Frozen execution selected `vendor/poppler/bin` despite deliberately invalid
  explicit and PATH test values.
- `lam doctor` reported `is_frozen=true`, the bundle root, successful EasyOCR
  import, valid model hashes, model download disabled, both Poppler executables,
  package templates, and writable isolated runtime paths.

## Resource manifests

### EasyOCR

Manifest: `LAM_tools/packaging/manifests/easyocr-models.json`

| File | Role | Size | SHA-256 |
|---|---|---:|---|
| `craft_mlt_25k.pth` | detection | 83,152,330 | `4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17` |
| `english_g2.pth` | English recognition | 15,143,997 | `e2272681d9d67a04e2dff396b6e95077bc19001f8f6d3593c307b9852e1c29e8` |

The model staging directory is ignored by Git. Sources and license metadata are
recorded in the manifest.

### Poppler

Manifest: `LAM_tools/packaging/manifests/poppler-windows.json`

- Poppler 26.07.0, 41 conda-forge package records.
- 195 staged files, each with size and SHA-256.
- Includes required executables, DLLs, data, fonts, and package license files.
- Vendor staging is ignored by Git.

## Release tree

- Verification: passed.
- Files: 7,303.
- Size: 999,412,913 bytes (about 953.1 MiB).
- EasyOCR manifest verification: 2/2 files passed.
- Poppler manifest verification: 195/195 files passed.
- Required root documents and launch scripts: present.
- Forbidden library data, tests, cache, `.env`, and runtime state: not found.

## Test results

| Test group | Result | Notes |
|---|---|---|
| Source pytest suite | 480 passed, 4 deselected | Python 3.14.6; complete non-live suite |
| Release/reference regression subset | 49 passed | packaging, doctor, release tree, provider and reference-text paths |
| Release-tree verifier | passed | 7,303 files; manifests and exclusions checked |
| Frozen smoke | 27 passed, 0 failed | about 85 seconds; every Agent call used `--caller agent` |
| Version / commands JSON | passed | version `0.6.1` and registry JSON available |
| Doctor | passed, exit 0 | EasyOCR import and all frozen diagnostics passed |
| Init / check | passed | dry-run-first; isolated libraries below `D:\LAM_build\smoke` |
| Native PDF | passed | native extraction path exercised |
| Image-only PDF | passed | actual `ocr_status=success` required |
| Screenshot-wrapped PDF | passed | actual `ocr_status=success` required |
| Reference text (offline) | passed | 1 file and 6 references parsed; provider resolution intentionally unavailable offline |
| No-identifier reference regression | passed | 4 candidates, 4 `registered_new`, 0 unresolved; `--refresh --no-cache-write` |
| File / cleanup | passed | dry-run-first followed by the public default/apply contract |
| Zotero export | passed as guarded preview | empty Catalogue correctly returned `needs_review`; Agent did not apply |

OCR isolation assertions all passed: no system Poppler dependency, no files in
the fake user EasyOCR cache, no model mutation or download, and frozen download
disabled. Network proxies were deliberately invalid during smoke testing.
The no-identifier provider regression alone permitted HTTPS and disabled cache
writes.

## PyInstaller warnings

| Category | Classification | Disposition |
|---|---|---|
| Conda pseudo-package `__win` not found | benign environment metadata | ignored; runtime tests passed |
| Optional TensorBoard imports | optional dependency surface | ignored; LAM does not use TensorBoard and frozen OCR passed |
| POSIX and Java modules | optional/cross-platform imports | ignored on Windows after runtime smoke |
| Broad optional transformers/test/helper modules | upstream EasyOCR/Torch dependency surface | retained for monitoring; no tested runtime path failed |

The earlier `_cdflib` and Linux `libgomp` warnings were not emitted by this
clean rebuild. No new hidden import was added.

No warning was promoted to a release-blocking failure. The full warning file is
`D:\LAM_build\work\pyinstaller\lam\warn-lam.txt`.

## Unresolved risks

1. The onedir is approximately 953 MiB. Official EasyOCR/Torch hooks also
   collect broad optional dependency surfaces; size reduction should be a
   separate, evidence-driven task.
2. Poppler is marked GPL-2.0-or-later and its dependency set contains additional
   licenses. Redistribution must retain all notices and satisfy corresponding
   source/offer obligations before public delivery.
3. EasyOCR model weights are attributed to the EasyOCR release artifacts and
   Apache-2.0 in the manifest. Confirm the redistribution provenance for the
   exact weight artifacts before public delivery.
4. A no-identifier citation containing `Clin Sci (London Engl 1979) (2015)` is
   currently parsed with year 1979 and is conservatively rejected against the
   correct 2015 provider record. This parser boundary is separate from the
   fixed title-search defect and remains unresolved.
5. Pure title-only input without author/year/journal support remains ambiguous
   by design; the public `refs2.txt` title-list fixture is therefore not treated
   as four automatically confirmed papers.
6. The build was produced from a dirty working tree. Commit or otherwise freeze
   every intended source/resource file and reproduce the build from that exact
   revision before publication.
7. Tests prove Windows x64 CPU OCR on this build host. A second clean Windows
   machine should still run the staged onedir before signing or publishing.

## Release-candidate decision

The staging tree **can enter internal technical release-candidate review** and
contains the corrected no-identifier reference-search implementation. It
should not be published or signed yet. Publication remains gated by a clean
source commit/tag, third-party license review, and a second-machine smoke test.
No publication action was performed.
