from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path
from typing import Any


EXPECTED_EXIT_CODES = {0, 2, 3}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    executable: Path,
    args: list[str],
    env: dict[str, str],
    results: list[dict[str, Any]],
    name: str,
    *,
    json_output: bool = True,
) -> dict[str, Any] | str:
    completed = subprocess.run(
        [str(executable), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        env=env,
        cwd=executable.parent,
    )
    item = {
        "name": name,
        "args": args,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
        "ok": completed.returncode in EXPECTED_EXIT_CODES,
    }
    results.append(item)
    if not item["ok"]:
        raise RuntimeError(
            f"Frozen smoke test failed: {name}; exit={completed.returncode}; "
            f"stdout={completed.stdout[-2000:]}; stderr={completed.stderr[-1000:]}"
        )
    if not json_output:
        return completed.stdout.strip()
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(f"Frozen command did not emit JSON: {name}") from exc


def init_library(
    executable: Path,
    root: Path,
    env: dict[str, str],
    results: list[dict[str, Any]],
    name: str,
) -> None:
    dry_run_then_apply(
        executable,
        ["--root", str(root), "--caller", "agent", "--json", "init"],
        env,
        results,
        name,
    )


def dry_run_then_apply(
    executable: Path,
    args: list[str],
    env: dict[str, str],
    results: list[dict[str, Any]],
    name: str,
    *,
    explicit_apply: bool = True,
) -> None:
    preview = run(
        executable,
        [*args, "--dry-run"],
        env,
        results,
        f"{name}_dry_run",
    )
    assert isinstance(preview, dict)
    if preview.get("status") not in {"success", "no_changes"}:
        raise RuntimeError(
            f"Frozen smoke apply blocked by dry-run status for {name}: "
            f"{preview.get('status')}"
        )
    run(
        executable,
        [*args, "--apply"] if explicit_apply else args,
        env,
        results,
        f"{name}_apply",
    )


def nested_details(payload: dict[str, Any]) -> dict[str, Any]:
    workflow = payload.get("details") or {}
    return workflow.get("details") or {}


def find_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                found.append(current_value)
            found.extend(find_values(current_value, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(find_values(item, key))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated LAM frozen smoke tests")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, default=Path(r"D:\LAM_build"))
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    release = args.release_root.resolve()
    source = args.source_root.resolve()
    build_root = args.build_root.resolve()
    executable = release / "lam.exe"
    if not executable.is_file():
        raise FileNotFoundError(executable)
    smoke_root = (build_root / "smoke").resolve()
    smoke_root.relative_to(build_root)
    if smoke_root.exists():
        shutil.rmtree(smoke_root)
    smoke_root.mkdir(parents=True)

    fake_home = smoke_root / "isolated-home"
    fake_home.mkdir()
    forbidden_poppler = smoke_root / "not-poppler"
    forbidden_poppler.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
            "EASYOCR_MODULE_PATH": str(fake_home / "forbidden-easyocr-cache"),
            "OCR_MODEL_STORAGE_DIR": str(fake_home / "forbidden-models"),
            "OCR_DOWNLOAD_ENABLED": "true",
            "POPPLER_PATH": str(forbidden_poppler),
            "PATH": os.pathsep.join(
                value
                for value in (
                    os.environ.get("SystemRoot", r"C:\Windows") + r"\System32",
                    os.environ.get("SystemRoot", r"C:\Windows"),
                )
            ),
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    model_files = sorted((release / "models" / "easyocr").glob("*.pth"))
    model_hashes_before = {path.name: sha256(path) for path in model_files}
    results: list[dict[str, Any]] = []
    expected_version = runpy.run_path(str(source / "src" / "lam" / "versions.py"))[
        "PACKAGE_VERSION"
    ]

    version = run(
        executable,
        ["--caller", "agent", "--version"],
        env,
        results,
        "version",
        json_output=False,
    )
    if expected_version not in version:
        raise RuntimeError(f"Unexpected frozen version output: {version}")
    commands_root = smoke_root / "commands-root"
    commands = run(
        executable,
        ["--root", str(commands_root), "--caller", "agent", "--json", "commands"],
        env,
        results,
        "commands_json",
    )
    if commands.get("status") not in {"success", "no_changes"}:
        raise RuntimeError("commands --json did not return a normal status")

    base = smoke_root / "base-library"
    init_library(executable, base, env, results, "init")
    doctor = run(
        executable,
        ["--root", str(base), "--caller", "agent", "--json", "doctor"],
        env,
        results,
        "doctor",
    )
    diagnostics = nested_details(doctor)
    if doctor.get("status") != "success":
        raise RuntimeError(f"frozen doctor did not succeed: {doctor.get('status')}")
    if diagnostics.get("is_frozen") is not True:
        raise RuntimeError("doctor did not report is_frozen=true")
    if diagnostics.get("easyocr_import") is not True:
        raise RuntimeError("frozen doctor could not import EasyOCR")
    if diagnostics.get("model_download_disabled") is not True:
        raise RuntimeError("frozen doctor did not force model downloads off")
    if not (diagnostics.get("model_integrity") or {}).get("ok"):
        raise RuntimeError("frozen EasyOCR model integrity failed")
    executions = diagnostics.get("poppler_executables") or {}
    if not all((executions.get(name) or {}).get("execution_ok") for name in ("pdftoppm", "pdftocairo")):
        raise RuntimeError("bundled pdftoppm/pdftocairo execution failed")
    expected_model_root = (release / "models" / "easyocr").resolve()
    if Path(diagnostics["model_path"]).resolve() != expected_model_root:
        raise RuntimeError("frozen model path did not override the user path")
    if not Path(diagnostics["poppler_path"]).resolve().is_relative_to(release):
        raise RuntimeError("frozen Poppler path did not override configuration/PATH")

    dry_run_then_apply(
        executable,
        ["--root", str(base), "--caller", "agent", "--json", "check"],
        env,
        results,
        "check",
        explicit_apply=False,
    )
    fixtures = source / "examples" / "test_corpus" / "included"
    for name, filename, ocr_mode in (
        ("native_pdf", "native_text.pdf", "never"),
        ("image_only_ocr", "image_only.pdf", "always"),
        ("screenshot_wrapped", "screenshot_wrapped.pdf", "always"),
    ):
        library = smoke_root / f"{name}-library"
        init_library(executable, library, env, results, f"{name}_init")
        shutil.copy2(fixtures / filename, library / "Inbox" / filename)
        payload = run(
            executable,
            [
                "--root",
                str(library),
                "--caller",
                "agent",
                "--json",
                "register",
                "--dry-run",
                "--offline",
                "--no-cache-write",
                "--ocr",
                ocr_mode,
                "--max-files",
                "1",
            ],
            env,
            results,
            name,
        )
        if payload.get("status") == "failed":
            raise RuntimeError(f"{name} returned failed")
        if ocr_mode == "always" and "success" not in find_values(payload, "ocr_status"):
            raise RuntimeError(f"{name} did not complete EasyOCR successfully")

    reference_library = smoke_root / "reference-library"
    init_library(executable, reference_library, env, results, "reference_init")
    reference = reference_library / "Inbox" / "refs1.txt"
    shutil.copy2(
        source / "examples" / "test_corpus" / "reference_text" / "refs1.txt",
        reference,
    )
    reference_payload = run(
        executable,
        [
            "--root",
            str(reference_library),
            "--caller",
            "agent",
            "--json",
            "register",
            "--dry-run",
            "--reference-text",
            "only",
            "--reference-file",
            str(reference),
            "--offline",
            "--no-cache-write",
        ],
        env,
        results,
        "reference_text",
    )
    reference_counts = (reference_payload.get("details") or {}).get("counts") or {}
    if reference_counts.get("reference_files", 0) < 1 or reference_counts.get("references", 0) < 1:
        raise RuntimeError("reference-text smoke did not parse the fixture")

    # This live, no-cache-write dry run is the frozen regression for the 0.6.1
    # no-identifier reference-search defect. It deliberately retains the fake
    # HOME and frozen OCR/Poppler overrides, but permits provider HTTPS.
    reference2_library = smoke_root / "reference2-library"
    init_library(executable, reference2_library, env, results, "reference2_init")
    reference2 = reference2_library / "Inbox" / "refs2.txt"
    public_citations = [
        line.strip()
        for line in (
            source / "examples" / "test_corpus" / "reference_text" / "refs1.txt"
        ).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if len(public_citations) < 5:
        raise RuntimeError("Public reference fixture contains fewer than five citations")
    no_identifier_citations = [
        citation.split(" doi:", maxsplit=1)[0].rstrip(" .") + "."
        for citation in public_citations[1:5]
    ]
    reference2.write_text(
        "\n".join(no_identifier_citations) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    provider_env = env.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        provider_env.pop(name, None)
        provider_env.pop(name.casefold(), None)
    reference2_payload = run(
        executable,
        [
            "--root",
            str(reference2_library),
            "--caller",
            "agent",
            "--json",
            "register",
            "--dry-run",
            "--reference-text",
            "only",
            "--reference-file",
            str(reference2),
            "--refresh",
            "--no-cache-write",
        ],
        provider_env,
        results,
        "reference_text_no_identifier_live",
    )
    reference2_counts = (reference2_payload.get("details") or {}).get("counts") or {}
    if reference2_counts.get("references") != 4:
        raise RuntimeError(
            "refs2 frozen regression did not parse exactly four references: "
            f"{reference2_counts}"
        )
    if reference2_counts.get("registered_new") != 4 or reference2_counts.get("unresolved") != 0:
        raise RuntimeError(
            "refs2 frozen regression did not resolve all four references: "
            f"{reference2_counts}"
        )
    dry_run_then_apply(
        executable,
        ["--root", str(base), "--caller", "agent", "--json", "file"],
        env,
        results,
        "file",
        explicit_apply=False,
    )
    run(
        executable,
        [
            "--root",
            str(base),
            "--caller",
            "agent",
            "--json",
            "export",
            "zotero",
            "--all",
            "--dry-run",
            "--offline",
            "--no-cache-write",
        ],
        env,
        results,
        "zotero_export_dry_run",
    )
    dry_run_then_apply(
        executable,
        ["--root", str(base), "--caller", "agent", "--json", "cleanup"],
        env,
        results,
        "cleanup",
    )

    model_hashes_after = {path.name: sha256(path) for path in model_files}
    cache_root = fake_home / "forbidden-easyocr-cache"
    cache_files = list(cache_root.rglob("*")) if cache_root.exists() else []
    assertions = {
        "no_system_poppler_dependency": True,
        "no_user_easyocr_cache_files": not any(path.is_file() for path in cache_files),
        "no_model_mutation_or_download": model_hashes_before == model_hashes_after,
        "frozen_model_download_disabled": diagnostics.get("model_download_disabled") is True,
    }
    if not all(assertions.values()):
        raise RuntimeError(f"Frozen isolation assertions failed: {assertions}")
    report = {
        "schema_version": "1",
        "release_root": str(release),
        "smoke_root": str(smoke_root),
        "tests": results,
        "assertions": assertions,
        "passed": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
    }
    output = args.json_output or build_root / "reports" / "frozen-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
