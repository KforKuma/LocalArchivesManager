from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_ROOT_FILES = (
    "lam.exe",
    "AGENTS.md",
    "Workflows.md",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    ".env.example",
    "setup-lam.bat",
    "open-lam-terminal.bat",
)
FORBIDDEN_NAMES = {
    ".env",
    ".library_state",
    "catalogue.xlsx",
    "library_changes.md",
    "summary.md",
    "Inbox",
    "Registered",
    "Topics",
    "Exports",
    "tests",
    ".pytest_cache",
    "__pycache__",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(root: Path, manifest_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "manifest": str(manifest_path),
        "ok": False,
        "checked": 0,
        "errors": [],
    }
    if not manifest_path.is_file():
        result["errors"].append("manifest_missing")
        return result
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = payload.get("files") or payload.get("models") or []
    for entry in entries:
        relative = entry.get("path") or entry.get("filename")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            result["errors"].append(f"unsafe_path:{relative}")
            continue
        if not candidate.is_file():
            result["errors"].append(f"missing:{relative}")
            continue
        if candidate.stat().st_size != int(entry["size_bytes"]):
            result["errors"].append(f"size:{relative}")
            continue
        if sha256(candidate) != str(entry["sha256"]).casefold():
            result["errors"].append(f"sha256:{relative}")
            continue
        result["checked"] += 1
    result["ok"] = bool(entries) and not result["errors"]
    return result


def verify_release(release: Path) -> dict[str, Any]:
    errors: list[str] = []
    if not release.is_dir():
        return {"ok": False, "errors": ["release_root_missing"]}
    for name in REQUIRED_ROOT_FILES:
        if not (release / name).is_file():
            errors.append(f"required_file_missing:{name}")
    if not (release / "_internal").is_dir():
        errors.append("required_directory_missing:_internal")
    for path in release.rglob("*"):
        relative = path.relative_to(release)
        if any(part in FORBIDDEN_NAMES for part in relative.parts):
            errors.append(f"forbidden_path:{relative.as_posix()}")
        if path.is_file() and path.suffix.casefold() == ".pdf":
            errors.append(f"unexpected_pdf:{relative.as_posix()}")
    model = verify_manifest(
        release / "models" / "easyocr",
        release / "models" / "easyocr" / "manifest.json",
    )
    poppler = verify_manifest(
        release / "vendor" / "poppler",
        release / "vendor" / "poppler" / "manifest.json",
    )
    if not model["ok"]:
        errors.append("easyocr_manifest_failed")
    if not poppler["ok"]:
        errors.append("poppler_manifest_failed")
    files = [path for path in release.rglob("*") if path.is_file()]
    return {
        "ok": not errors,
        "release_root": str(release),
        "file_count": len(files),
        "size_bytes": sum(path.stat().st_size for path in files),
        "easyocr": model,
        "poppler": poppler,
        "errors": sorted(set(errors)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a staged LAM onedir tree")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = verify_release(args.release_root.resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    print(text, end="")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text, encoding="utf-8", newline="\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
