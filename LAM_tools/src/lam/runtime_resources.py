from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    is_frozen: bool
    bundle_root: Path | None
    easyocr_models: Path | None
    poppler_bin: Path | None


def runtime_layout() -> RuntimeLayout:
    frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return RuntimeLayout(False, None, None, None)
    bundle_root = Path(sys.executable).resolve().parent
    return RuntimeLayout(
        True,
        bundle_root,
        bundle_root / "models" / "easyocr",
        bundle_root / "vendor" / "poppler" / "bin",
    )


def resolve_poppler_bin(explicit: Path | None) -> tuple[Path | None, str]:
    """Resolve Poppler without mutating PATH.

    A present frozen resource wins over configuration and PATH. Source mode and
    an incomplete frozen staging may still use an explicit directory, then the
    process PATH. Doctor reports the selected source so release verification can
    require the frozen resource.
    """

    layout = runtime_layout()
    if layout.poppler_bin is not None and _contains_poppler(layout.poppler_bin):
        return layout.poppler_bin, "frozen_bundle"
    if explicit is not None and _contains_poppler(explicit):
        return explicit, "explicit"
    found = shutil.which("pdftoppm") or shutil.which("pdftocairo")
    if found:
        return Path(found).resolve().parent, "path"
    return None, "unavailable"


def frozen_easyocr_configuration(
    explicit_model_dir: Path | None,
    download_enabled: bool,
) -> tuple[Path | None, bool, str]:
    layout = runtime_layout()
    if layout.is_frozen:
        # Deliberately return the bundle path even when it is missing. This
        # prevents EasyOCR from falling back to ~/.EasyOCR in a frozen build.
        return layout.easyocr_models, False, "frozen_bundle"
    return explicit_model_dir, download_enabled, (
        "explicit" if explicit_model_dir is not None else "easyocr_default"
    )


def verify_asset_manifest(
    asset_root: Path | None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "asset_root": str(asset_root) if asset_root else None,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "files_checked": 0,
        "errors": [],
    }
    if asset_root is None or not asset_root.is_dir():
        result["errors"].append("asset_root_missing")
        return result
    selected_manifest = manifest_path or asset_root / "manifest.json"
    result["manifest_path"] = str(selected_manifest)
    if not selected_manifest.is_file():
        result["errors"].append("manifest_missing")
        return result
    try:
        payload = json.loads(selected_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["errors"].append("manifest_unreadable")
        return result
    entries = payload.get("files") or payload.get("models")
    if not isinstance(entries, list) or not entries:
        result["errors"].append("manifest_files_missing")
        return result
    for entry in entries:
        relative = entry.get("path") or entry.get("filename")
        expected_size = entry.get("size_bytes")
        expected_hash = str(entry.get("sha256") or "").casefold()
        if not relative or expected_size is None or len(expected_hash) != 64:
            result["errors"].append(f"invalid_manifest_entry:{relative!s}")
            continue
        candidate = (asset_root / str(relative)).resolve()
        try:
            candidate.relative_to(asset_root.resolve())
        except ValueError:
            result["errors"].append(f"unsafe_manifest_path:{relative}")
            continue
        if not candidate.is_file():
            result["errors"].append(f"missing:{relative}")
            continue
        if candidate.stat().st_size != int(expected_size):
            result["errors"].append(f"size_mismatch:{relative}")
            continue
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().casefold() != expected_hash:
            result["errors"].append(f"sha256_mismatch:{relative}")
            continue
        result["files_checked"] += 1
    result["ok"] = not result["errors"] and result["files_checked"] == len(entries)
    return result


def _contains_poppler(path: Path) -> bool:
    return any((path / name).is_file() for name in ("pdftoppm.exe", "pdftocairo.exe", "pdftoppm", "pdftocairo"))
