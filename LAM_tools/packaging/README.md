# Windows PyInstaller packaging

`lam.spec` builds the current LAM package-version console-mode Windows onedir application. It is
relative to `SPECPATH`, uses PyInstaller's official hooks plus
`pyinstaller-hooks-contrib`, restricts EasyOCR language data to English, and
does not collect tests, local configuration, model caches or library data.

Model weights and Poppler binaries are deliberately separate ignored staging
assets:

```text
packaging/assets/easyocr-models/
packaging/vendor/poppler/
```

Their tracked integrity metadata is stored in `packaging/manifests/`. Prepare
the assets from explicit, reviewed sources:

```powershell
python scripts/prepare_release_assets.py `
  --easyocr-source C:\reviewed\EasyOCR\model `
  --poppler-prefix D:\LAM_build\asset-env `
  --poppler-package-cache D:\LAM_build\conda-pkgs
```

Build and stage without modifying PATH:

```powershell
.\scripts\build_onedir.ps1 -PythonExe C:\path\to\python.exe
.\scripts\stage_onedir.ps1 -PythonExe C:\path\to\python.exe
```

The clean PyInstaller tree is written to `D:\LAM_build\dist\LAM-<package-version>` and
the complete release staging tree to
`D:\LAM_build\release\LAM-<package-version>-windows-x64`. Frozen runtime resources remain
outside `_internal/` at `models/easyocr/` and `vendor/poppler/`; LAM forces the
former with downloads disabled and prefers the bundled Poppler directory.
