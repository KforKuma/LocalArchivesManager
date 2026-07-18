from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType
import sys

from lam.config import Settings
from lam.runtime_resources import verify_asset_manifest
from lam.services.ocr_service import OcrService


def test_pyinstaller_spec_is_relative_and_onedir():
    source_root = Path(__file__).resolve().parents[1]
    spec_path = source_root / "packaging" / "lam.spec"
    spec = spec_path.read_text(encoding="utf-8")

    compile(spec, str(spec_path), "exec")
    assert "PACKAGING_ROOT = Path(SPECPATH).resolve()" in spec
    assert "PROJECT_ROOT = PACKAGING_ROOT.parent" in spec
    assert "exclude_binaries=True" in spec
    assert "COLLECT(" in spec
    assert 'contents_directory="_internal"' in spec
    assert 'hooksconfig={"easyocr": {"lang_codes": ["en"]}}' in spec
    assert "collect_all" not in spec
    assert "hookspath=[]" in spec
    assert "D:\\ResearchLibrary" not in spec


def test_build_script_uses_independent_build_root():
    source_root = Path(__file__).resolve().parents[1]
    script = (source_root / "scripts" / "build_onedir.ps1").read_text(
        encoding="utf-8"
    )

    assert '"D:\\LAM_build"' in script
    assert '"work\\pyinstaller"' in script
    assert '"dist"' in script
    assert '"--workpath"' in script
    assert '"--distpath"' in script
    assert "-m\", \"PyInstaller" in script
    assert "D:\\ResearchLibrary must never be used" in script


def test_frozen_easyocr_probe_does_not_relaunch_lam_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "easyocr", ModuleType("easyocr"))
    monkeypatch.setattr(OcrService, "_easyocr_import_available", None)

    service = object.__new__(OcrService)

    assert service._probe_easyocr_import() is True


def test_frozen_settings_force_bundle_assets_and_disable_downloads(
    library_factory, monkeypatch, tmp_path
):
    bundle = tmp_path / "release"
    executable = bundle / "lam.exe"
    models = bundle / "models" / "easyocr"
    poppler = bundle / "vendor" / "poppler" / "bin"
    models.mkdir(parents=True)
    poppler.mkdir(parents=True)
    executable.write_bytes(b"")
    (poppler / "pdftoppm.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("OCR_DOWNLOAD_ENABLED", "true")
    monkeypatch.setenv("OCR_MODEL_STORAGE_DIR", str(tmp_path / "user-models"))
    monkeypatch.setenv("POPPLER_PATH", str(tmp_path / "user-poppler"))

    settings = Settings.from_root(library_factory([]))

    assert settings.ocr.model_storage_dir == models.resolve()
    assert settings.ocr.download_enabled is False
    assert settings.ocr.poppler_path == poppler.resolve()


def test_asset_manifest_integrity_detects_mutation(tmp_path):
    asset = tmp_path / "asset"
    asset.mkdir()
    model = asset / "model.pth"
    model.write_bytes(b"stable-model")
    manifest = {
        "models": [
            {
                "filename": model.name,
                "size_bytes": model.stat().st_size,
                "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            }
        ]
    }
    (asset / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_asset_manifest(asset)["ok"] is True
    model.write_bytes(b"changed")
    result = verify_asset_manifest(asset)
    assert result["ok"] is False
    assert result["errors"] == ["size_mismatch:model.pth"]
