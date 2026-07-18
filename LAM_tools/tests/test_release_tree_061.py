from __future__ import annotations

from pathlib import Path
import shutil
import socket
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "LAM_tools"


def test_default_session_blocks_network_sockets():
    with pytest.raises(RuntimeError, match="may not open network sockets"):
        socket.create_connection(("example.invalid", 443), timeout=0.01)


def test_release_manifests_are_allowlisted_and_exclude_local_state():
    for path in (REPOSITORY_ROOT / "MANIFEST.in", SOURCE_ROOT / "MANIFEST.in"):
        text = path.read_text(encoding="utf-8")
        prefix = "LAM_tools/" if path.parent == REPOSITORY_ROOT else ""
        for required in (
            f"prune {prefix}dist",
            f"prune {prefix}tmp",
            f"prune {prefix}dev_local",
            f"prune {prefix}release_staging",
            "global-exclude .env",
            "summary.md",
        ):
            assert required in text
        assert "catalogue.xlsx" not in text
        assert "Inbox" not in text
        assert "Registered" not in text
        assert "Topics" not in text


def test_git_tracked_paths_contain_no_library_or_secret_files():
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git executable is unavailable; release audit runs this check separately")
    completed = subprocess.run(
        [git, "ls-files"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = {line.replace("\\", "/") for line in completed.stdout.splitlines()}
    forbidden_exact = {
        ".env",
        "LAM_tools/.env",
        "catalogue.xlsx",
        "library_changes.md",
    }
    forbidden_prefixes = (
        ".library_state/",
        "Inbox/",
        "Registered/",
        "Topics/",
        "Exports/",
        "LAM_tools/.build/",
        "LAM_tools/dist/",
    )
    assert not (tracked & forbidden_exact)
    assert not any(path.startswith(forbidden_prefixes) for path in tracked)
    unexpected_pdfs = {
        path
        for path in tracked
        if path.casefold().endswith(".pdf")
        and not path.startswith("LAM_tools/examples/test_corpus/included/")
    }
    assert unexpected_pdfs == set()


def test_legacy_fixture_imports_are_restricted_to_migration_or_recovery():
    offenders = []
    for path in (SOURCE_ROOT / "tests").glob("test_*.py"):
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        if "legacy_library_factory" not in text and "fixtures.legacy" not in text:
            continue
        if "migration" not in path.name and "recovery" not in path.name:
            offenders.append(path.name)
    assert offenders == []
    legacy_root = SOURCE_ROOT / "tests" / "fixtures" / "legacy"
    assert (legacy_root / "README.md").is_file()


def test_public_source_contains_no_developer_absolute_identity():
    forbidden = ("johnhsiung", "C:/Users/", "C:\\Users\\")
    candidates = [
        *SOURCE_ROOT.glob("*.toml"),
        *SOURCE_ROOT.glob("*.md"),
        *SOURCE_ROOT.rglob("*.py"),
        *SOURCE_ROOT.rglob("*.ps1"),
        *SOURCE_ROOT.rglob("*.spec"),
    ]
    for path in candidates:
        if path.resolve() == Path(__file__).resolve():
            continue
        if any(part in {".build", "dist", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not any(value in text for value in forbidden), path
