from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
from pathlib import Path
from typing import Any


MODEL_CONTRACT = (
    {
        "role": "detection",
        "language": "multilingual",
        "filename": "craft_mlt_25k.pth",
        "md5": "2f8227d2def4037cdb3b34389dcf9ec1",
        "source": "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip",
        "license": "Apache-2.0",
    },
    {
        "role": "recognition",
        "language": "en",
        "filename": "english_g2.pth",
        "md5": "5864788e1821be9e454ec108d61b887d",
        "source": "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip",
        "license": "Apache-2.0",
    },
)


def digest(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def file_entry(path: Path, root: Path, *, role: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "role": role,
        "size_bytes": path.stat().st_size,
        "sha256": digest(path),
    }


def safe_reset(path: Path, packaging_root: Path) -> None:
    resolved = path.resolve()
    resolved.relative_to(packaging_root.resolve())
    if resolved == packaging_root.resolve():
        raise RuntimeError("Refusing to reset the packaging root")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def prepare_easyocr(
    source: Path,
    target: Path,
    manifest_path: Path,
    packaging_root: Path,
) -> dict[str, Any]:
    safe_reset(target, packaging_root)
    models: list[dict[str, Any]] = []
    for contract in MODEL_CONTRACT:
        candidate = source / contract["filename"]
        if not candidate.is_file():
            raise FileNotFoundError(f"Required EasyOCR model is missing: {candidate}")
        if digest(candidate, "md5") != contract["md5"]:
            raise RuntimeError(f"EasyOCR upstream MD5 mismatch: {candidate.name}")
        destination = target / candidate.name
        shutil.copy2(candidate, destination)
        models.append(
            {
                **contract,
                "size_bytes": destination.stat().st_size,
                "sha256": digest(destination),
                "license_source": "https://github.com/JaidedAI/EasyOCR/blob/master/LICENSE",
            }
        )
    easyocr_distribution = importlib.metadata.distribution("easyocr")
    license_source = Path(easyocr_distribution.locate_file("easyocr-1.7.2.dist-info/LICENSE"))
    if license_source.is_file():
        shutil.copy2(license_source, target / "LICENSE-EasyOCR.txt")
    payload = {
        "schema_version": "1",
        "asset": "easyocr-models",
        "easyocr_version": importlib.metadata.version("easyocr"),
        "languages": ["en"],
        "download_enabled": False,
        "default_git_tracking": False,
        "models": models,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def prepare_poppler(
    prefix: Path,
    package_cache: Path,
    target: Path,
    manifest_path: Path,
    packaging_root: Path,
) -> dict[str, Any]:
    source_bin = prefix / "Library" / "bin"
    if not source_bin.is_dir():
        raise FileNotFoundError(f"Conda Poppler bin directory is missing: {source_bin}")
    for required in ("pdftoppm.exe", "pdftocairo.exe"):
        if not (source_bin / required).is_file():
            raise FileNotFoundError(f"Required Poppler executable is missing: {required}")
    safe_reset(target, packaging_root)
    target_bin = target / "bin"
    target_bin.mkdir(parents=True)
    for source in sorted(source_bin.glob("*.dll")):
        shutil.copy2(source, target_bin / source.name)
    for name in ("pdftoppm.exe", "pdftocairo.exe", "pdfinfo.exe"):
        source = source_bin / name
        if source.is_file():
            shutil.copy2(source, target_bin / name)
    for relative in (
        Path("Library/share/poppler"),
        Path("Library/share/fonts"),
        Path("Library/etc/fonts"),
    ):
        source = prefix / relative
        if source.is_dir():
            destination = target / Path(*relative.parts[1:])
            shutil.copytree(source, destination, dirs_exist_ok=True)

    # Conda's generated fontconfig README embeds the absolute preparation
    # prefix. Normalize only that byte sequence before hashing so reviewed
    # manifests and release candidates never disclose the local build root.
    fontconfig_readme = target / "etc" / "fonts" / "conf.d" / "README"
    if fontconfig_readme.is_file():
        content = fontconfig_readme.read_bytes()
        sanitized = content.replace(
            prefix.as_posix().encode("utf-8"),
            b"C:/LAM_Build/asset-env",
        )
        if sanitized != content:
            fontconfig_readme.write_bytes(sanitized)

    packages: list[dict[str, Any]] = []
    licenses_root = target / "licenses"
    for record_path in sorted((prefix / "conda-meta").glob("*.json")):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        name = str(record.get("name") or "")
        version = str(record.get("version") or "")
        build = str(record.get("build") or "")
        if not name:
            continue
        packages.append(
            {
                "name": name,
                "version": version,
                "build": build,
                "license": record.get("license"),
                "channel": "conda-forge",
                "url": _conda_forge_url(record),
            }
        )
        cached = package_cache / f"{name}-{version}-{build}" / "info" / "licenses"
        if cached.is_dir():
            shutil.copytree(
                cached,
                licenses_root / name,
                dirs_exist_ok=True,
            )

    files = [
        file_entry(path, target, role=_poppler_role(path, target))
        for path in sorted(item for item in target.rglob("*") if item.is_file())
    ]
    poppler_record = next(
        (item for item in packages if item["name"] == "poppler"),
        None,
    )
    if poppler_record is None:
        raise RuntimeError("The isolated prefix does not contain a Poppler package record")
    payload = {
        "schema_version": "1",
        "asset": "poppler-windows",
        "platform": "windows-x64",
        "poppler_version": poppler_record["version"],
        "source": poppler_record["url"],
        "license": poppler_record.get("license") or "GPL-2.0-or-later",
        "default_git_tracking": False,
        "packages": packages,
        "files": files,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def _poppler_role(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    if relative.parts[0] == "bin":
        return "executable" if path.suffix.casefold() == ".exe" else "runtime_dll"
    if relative.parts[0] == "licenses":
        return "license"
    return "runtime_data"


def _conda_forge_url(record: dict[str, Any]) -> str:
    name = str(record.get("name"))
    version = str(record.get("version"))
    build = str(record.get("build"))
    subdir = str(record.get("subdir") or "noarch")
    archive = f"{name}-{version}-{build}.conda"
    return (
        f"https://anaconda.org/conda-forge/{name}/{version}/download/"
        f"{subdir}/{archive}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare fixed, ignored EasyOCR and Poppler release assets"
    )
    parser.add_argument("--easyocr-source", type=Path, required=True)
    parser.add_argument("--poppler-prefix", type=Path, required=True)
    parser.add_argument("--poppler-package-cache", type=Path, required=True)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    packaging_root = project_root / "packaging"
    manifests = packaging_root / "manifests"
    easyocr = prepare_easyocr(
        args.easyocr_source.resolve(),
        packaging_root / "assets" / "easyocr-models",
        manifests / "easyocr-models.json",
        packaging_root,
    )
    poppler = prepare_poppler(
        args.poppler_prefix.resolve(),
        args.poppler_package_cache.resolve(),
        packaging_root / "vendor" / "poppler",
        manifests / "poppler-windows.json",
        packaging_root,
    )
    print(
        json.dumps(
            {
                "easyocr_models": len(easyocr["models"]),
                "poppler_files": len(poppler["files"]),
                "project_root": str(project_root),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
