from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone

from lam.config import Settings
from lam.models import WorkflowStatus
from lam.workflows.cleanup import CleanupWorkflow


def _set_age(path, days: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def _make_backups(root, count: int = 11) -> list:
    backups = []
    for index in range(count):
        path = root / f"catalogue.backup.202501{index + 1:02d}-010101.xlsx"
        shutil.copy2(root / "catalogue.xlsx", path)
        _set_age(path, 60 + count - index)
        backups.append(path)
    return backups


def test_cleanup_dry_run_does_not_delete(library_factory):
    root = library_factory([])
    backups = _make_backups(root)
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = CleanupWorkflow(settings).run(dry_run=True)
    assert result.status == WorkflowStatus.SUCCESS
    assert len(result.completed) == 1
    assert result.completed[0]["action"] == "would_delete"
    assert all(path.exists() for path in backups)


def test_cleanup_apply_deletes_only_allowlisted_and_counts_bytes(library_factory):
    root = library_factory([])
    backups = _make_backups(root)
    protected_pdf = root / "ordinary.pdf"
    protected_pdf.write_bytes(b"pdf")
    protected_summary = root / "summary.md"
    protected_summary.write_text("keep", encoding="utf-8")
    ordinary = root / "ordinary.txt"
    ordinary.write_text("keep", encoding="utf-8")
    deleted_size = backups[0].stat().st_size
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = CleanupWorkflow(settings).run(dry_run=False)
    assert result.status == WorkflowStatus.SUCCESS
    assert not backups[0].exists()
    assert all(path.exists() for path in backups[1:])
    assert protected_pdf.exists()
    assert protected_summary.exists()
    assert ordinary.exists()
    assert result.counts["released_bytes"] == deleted_size


def test_cleanup_keeps_every_valid_backup_from_last_30_days(library_factory):
    root = library_factory([])
    backups = _make_backups(root, count=12)
    for path in backups[:2]:
        _set_age(path, 10)
    result = CleanupWorkflow(Settings.from_root(root)).run(dry_run=True)
    selected = {item["path"] for item in result.completed}
    assert backups[0].name not in selected
    assert backups[1].name not in selected


def test_cleanup_preserves_unfinished_journal_and_removes_old_completed(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    runs = settings.state_dir / "runs"
    unfinished = runs / "unfinished"
    completed = runs / "completed"
    unfinished.mkdir(parents=True)
    completed.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    (unfinished / "operation_journal.json").write_text(
        json.dumps({"status": "catalogue_committed", "created_at": old}),
        encoding="utf-8",
    )
    (completed / "operation_journal.json").write_text(
        json.dumps({"status": "final_check_committed", "finished_at": old}),
        encoding="utf-8",
    )
    result = CleanupWorkflow(settings).run(dry_run=False)
    assert result.status == WorkflowStatus.SUCCESS
    assert unfinished.exists()
    assert not completed.exists()


def test_cleanup_preserves_backup_referenced_by_unfinished_journal(library_factory):
    root = library_factory([])
    backups = _make_backups(root, count=12)
    protected = backups[0]
    settings = Settings.from_root(root)
    run = settings.state_dir / "runs" / "unfinished-backup"
    run.mkdir(parents=True)
    (run / "operation_journal.json").write_text(
        json.dumps(
            {
                "status": "catalogue_committed",
                "catalogue_backup": str(protected),
            }
        ),
        encoding="utf-8",
    )
    result = CleanupWorkflow(settings).run(dry_run=True)
    selected = {item["path"] for item in result.completed}
    assert protected.name not in selected


def test_cleanup_skips_protected_content_inside_tmp(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    stale = settings.state_dir / "tmp" / "stale-run"
    stale.mkdir(parents=True)
    protected = stale / "summary.md"
    protected.write_text("keep", encoding="utf-8")
    _set_age(stale, 2)
    result = CleanupWorkflow(settings).run(dry_run=False)
    assert result.status == WorkflowStatus.NO_CHANGES
    assert protected.exists()


def test_cleanup_removes_only_expired_metadata_cache(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    cache = settings.metadata_cache_dir / "pubmed"
    cache.mkdir(parents=True)
    expired = cache / "expired.json"
    valid = cache / "valid.json"
    now = datetime.now(timezone.utc)
    expired.write_text(
        json.dumps({"expires_at": (now - timedelta(days=1)).isoformat()}),
        encoding="utf-8",
    )
    valid.write_text(
        json.dumps({"expires_at": (now + timedelta(days=1)).isoformat()}),
        encoding="utf-8",
    )
    CleanupWorkflow(settings).run(dry_run=False)
    assert not expired.exists()
    assert valid.exists()


def test_cleanup_keeps_active_log_and_five_rotations(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    logs = settings.logs_dir
    active = logs / "lam.log"
    active.write_text("active", encoding="utf-8")
    rotations = []
    for index in range(1, 7):
        path = logs / f"lam.log.{index}"
        path.write_text(str(index), encoding="utf-8")
        _set_age(path, index)
        rotations.append(path)
    result = CleanupWorkflow(settings).run(dry_run=False)
    assert result.status == WorkflowStatus.SUCCESS
    assert active.exists()
    assert all(path.exists() for path in rotations[:5])
    assert not rotations[5].exists()
