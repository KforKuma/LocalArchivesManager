from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from lam.config import Settings
from lam.exceptions import ConfigurationError
from lam.services.pdf_visual_service import PdfVisualService
from lam.services.run_workspace import RunWorkspace, temporary_inventory
from lam.workflows.cleanup import CleanupWorkflow


def _old(path: Path) -> None:
    timestamp = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_file_backed_visual_images_are_detached_closed_and_workspace_removed(
    current_library_factory,
):
    root = current_library_factory()
    source = root / "Inbox" / "image.pdf"
    source.write_bytes(b"image")

    def renderer(*_args, **kwargs):
        output = Path(kwargs["output_folder"]) / "page.png"
        Image.new("RGB", (200, 300), "white").save(output)
        return [Image.open(output)]

    settings = Settings.from_root(root)
    inspection = PdfVisualService(settings, renderer=renderer).inspect(
        source,
        native_text_chars=0,
        page_count=1,
        page_image_signals=[{"large_page_image": True}],
        run_id="windows-handle",
    )
    assert inspection.pdf_visual_type.value == "scanned_article_pdf"
    tmp = root / ".library_state" / "tmp"
    assert not tmp.exists() or not list(tmp.iterdir())


def test_run_workspace_retention_has_manifest_and_expiry(current_library_factory):
    root = current_library_factory()
    settings = Settings.from_root(root)
    workspace = RunWorkspace.create(
        settings,
        run_id="debug",
        workflow="ocr",
        artifact_type="ocr_debug_artifact",
        cleanup_policy="debug_retention",
    )
    (workspace.subdirectory("ocr") / "page.png").write_bytes(b"png")
    outcome = workspace.cleanup(status="failed", retain=True, retention_hours=2)
    assert outcome.retained is True
    manifest = (workspace.path / ".lam-temp.json").read_text(encoding="utf-8")
    assert '"artifact_type": "ocr_debug_artifact"' in manifest
    assert '"expires_at"' in manifest
    workspace.cleanup(status="test_cleanup")


def test_cleanup_explicitly_handles_strict_old_pytest_artifact_with_fixture_pdf(
    current_library_factory, monkeypatch
):
    root = current_library_factory()
    pytest_root = root / ".library_state" / "tmp" / "pytest-999-regression"
    library = pytest_root / "test_case" / "library"
    library.mkdir(parents=True)
    (library / "fixture.pdf").write_bytes(b"%PDF-1.4\nfixture")
    _old(pytest_root)
    settings = Settings.from_root(root)
    workflow = CleanupWorkflow(settings)
    monkeypatch.setattr(workflow, "_pytest_process_active", lambda: False)
    ordinary = workflow.run(dry_run=True)
    assert pytest_root.is_dir()
    assert any(
        item.get("action") == "skipped_requires_include_test_artifacts"
        for item in ordinary.skipped
    )
    preview = workflow.run(dry_run=True, include_test_artifacts=True)
    assert any(item.get("kind") == "test_temporary_artifact" for item in preview.completed)
    applied = workflow.run(dry_run=False, include_test_artifacts=True)
    assert not pytest_root.exists()
    assert applied.details["deleted"] == 1


def test_cleanup_partial_success_reports_committed_state(
    current_library_factory, monkeypatch
):
    root = current_library_factory()
    settings = Settings.from_root(root)
    workspaces = []
    for run_id in ("good", "blocked"):
        workspace = RunWorkspace.create(
            settings,
            run_id=run_id,
            workflow="test",
            artifact_type="failed_temporary_artifact",
        )
        (workspace.path / "artifact.tmp").write_bytes(run_id.encode())
        _old(workspace.path)
        workspaces.append(workspace)
    workflow = CleanupWorkflow(settings)
    original = workflow._delete_candidate

    def delete(candidate):
        if "blocked" in candidate.path.name:
            raise PermissionError("simulated Windows denial")
        original(candidate)

    monkeypatch.setattr(workflow, "_delete_candidate", delete)
    result = workflow.run(dry_run=False)
    assert result.status.value == "failed"
    assert result.state_committed is True
    assert result.details["partial_success"] is True
    assert result.details["deleted"] == 1
    assert result.details["failed"] == 1
    workspaces[1].cleanup(status="test_cleanup")


def test_status_inventory_exposes_unknown_and_manifested_temp(current_library_factory):
    root = current_library_factory()
    settings = Settings.from_root(root)
    workspace = RunWorkspace.create(
        settings,
        run_id="inventory",
        workflow="test",
        artifact_type="production_temporary_artifact",
    )
    (workspace.path / "one.tmp").write_bytes(b"1")
    unknown = root / ".library_state" / "tmp" / "legacy-unknown"
    unknown.mkdir(parents=True)
    inventory = temporary_inventory(settings)
    assert inventory["temporary_directories"] >= 2
    assert inventory["temporary_files"] >= 2
    assert inventory["unknown_temporary_artifacts"] == 1
    assert inventory["oldest_temporary_artifact"]
    workspace.cleanup(status="test_cleanup")
    unknown.rmdir()


def test_testing_mode_refuses_implicit_settings_root(monkeypatch):
    monkeypatch.setenv("LAM_TESTING", "1")
    monkeypatch.setenv("LAM_ALLOW_REAL_LIBRARY_TESTS", "false")
    monkeypatch.setenv("LIBRARY_ROOT", "D:/should-not-be-used")
    with pytest.raises(ConfigurationError, match="explicit isolated library root"):
        Settings.from_root(require_catalogue=False, allow_missing_root=True)


def test_testing_mode_refuses_explicit_real_library_root(monkeypatch):
    real_root = Path(os.environ["LAM_REAL_LIBRARY_ROOT"])
    monkeypatch.setenv("LAM_TESTING", "1")
    monkeypatch.setenv("LAM_ALLOW_REAL_LIBRARY_TESTS", "true")

    with pytest.raises(ConfigurationError, match="may not use the real library root"):
        Settings.from_root(real_root, require_catalogue=False)


def test_testing_mode_never_loads_project_dotenv(tmp_path, monkeypatch):
    root = tmp_path / "isolated-library"
    root.mkdir()
    monkeypatch.setenv("LAM_TESTING", "1")

    def forbidden(_project_root):
        raise AssertionError("test mode must not read the project .env")

    monkeypatch.setattr("lam.config._load_optional_dotenv", forbidden)
    settings = Settings.from_root(root, require_catalogue=False)

    assert settings.library_root == root.resolve()
