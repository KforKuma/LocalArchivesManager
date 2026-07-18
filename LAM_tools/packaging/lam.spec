# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 6 Windows onedir specification for LAM 0.6.1."""

from __future__ import annotations

import importlib.util
import runpy
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


PACKAGING_ROOT = Path(SPECPATH).resolve()
PROJECT_ROOT = PACKAGING_ROOT.parent
SOURCE_ROOT = PROJECT_ROOT / "src"
ENTRYPOINT = PACKAGING_ROOT / "entrypoint.py"

version_contract = runpy.run_path(str(SOURCE_ROOT / "lam" / "versions.py"))
PACKAGE_VERSION = version_contract["PACKAGE_VERSION"]

required_modules = (
    "easyocr",
    "filelock",
    "httpx",
    "openpyxl",
    "pdf2image",
    "PIL",
    "pypdf",
    "rapidfuzz",
    "torch",
    "torchvision",
    "cv2",
)
missing_modules = [
    name for name in required_modules if importlib.util.find_spec(name) is None
]
if missing_modules:
    raise RuntimeError(
        "The build environment is incomplete; missing modules: "
        + ", ".join(missing_modules)
    )

datas = []
resources_root = SOURCE_ROOT / "lam" / "resources"
if resources_root.is_dir():
    datas.append((str(resources_root), "lam/resources"))

for distribution in ("easyocr", "lam-tools"):
    try:
        datas += copy_metadata(distribution)
    except Exception as exc:
        print(f"LAM build warning: metadata unavailable for {distribution}: {exc}")

# Official PyInstaller and hooks-contrib hooks own torch, torchvision, cv2 and
# EasyOCR collection. The EasyOCR contrib hook is restricted to the language
# data used by LAM. Do not add hidden imports until a frozen test demonstrates
# a dynamic import not covered by those hooks.
a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={"easyocr": {"lang_codes": ["en"]}},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "_pytest",
        "torch.fx.passes.tests",
        "sklearn.datasets.tests",
    ],
    noarchive=False,
    optimize=0,
)

# Some upstream hooks include their own regression datasets as data even when
# the corresponding test modules are excluded. Remove only destination paths
# with an actual ``tests`` component; runtime package data remains untouched.
a.datas = [
    entry
    for entry in a.datas
    if "tests" not in entry[0].replace("\\", "/").split("/")
    and entry[0].replace("\\", "/").split("/")[-1].casefold()
    != "direct_url.json"
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=f"LAM-{PACKAGE_VERSION}",
)
