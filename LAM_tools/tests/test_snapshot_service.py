from __future__ import annotations

import os
import json

import pytest
from pathlib import Path

from lam.models import DiffType
from lam.exceptions import FileOperationError
from lam.services.snapshot_service import SnapshotService

from conftest import write_pdf


def test_scan_excludes_management_directories(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"one"})
    write_pdf(root / "LAM_tools" / "hidden.pdf", b"hidden")
    write_pdf(root / ".library_state" / "hidden.pdf", b"hidden")
    write_pdf(root / ".idea" / "hidden.pdf", b"hidden")
    write_pdf(root / ".agents" / "hidden.pdf", b"hidden")
    write_pdf(root / "scripts" / "hidden.pdf", b"hidden")
    service = SnapshotService(root, root / ".library_state")
    manifest = service.scan()
    assert [item.relative_path for item in manifest.values()] == ["Registered/paper.pdf"]


def test_move_is_detected_by_quick_hash(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    (root / "Topic_A").mkdir()
    os.replace(root / "Registered" / "paper.pdf", root / "Topic_A" / "paper.pdf")
    current = service.scan(previous)
    diffs, unchanged = service.compare(previous, current)
    assert unchanged == 0
    assert len(diffs) == 1
    assert diffs[0].diff_type == DiffType.MOVED_OR_RENAMED
    assert diffs[0].old_path == "Registered/paper.pdf"
    assert diffs[0].new_path == "Topic_A/paper.pdf"


def test_same_path_content_change_is_modified(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"old"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    path = root / "Registered" / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\nnew content\n%%EOF\n")
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert [item.diff_type for item in diffs] == [DiffType.MODIFIED]


def test_same_filename_move_remains_identity_when_content_changes(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"old"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    (root / "Topic_A").mkdir()
    old = root / "Registered" / "paper.pdf"
    new = root / "Topic_A" / "paper.pdf"
    old.replace(new)
    new.write_bytes(b"%PDF-1.4\nchanged and larger\n%%EOF\n")
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert [item.diff_type for item in diffs] == [DiffType.MOVED_OR_RENAMED]
    assert diffs[0].details == {"matched_by": "filename", "content_changed": True}


def test_snapshot_generation_is_committed_with_one_marker(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"one"})
    service = SnapshotService(root, root / ".library_state")
    manifest = service.scan()
    service.commit({"sheet": "Catalogue", "rows": []}, manifest, {"mode": "initial"})
    marker = json.loads(service.commit_marker_path.read_text(encoding="utf-8"))
    generation = marker["generation_id"]
    for path in (
        service.catalogue_snapshot_path,
        service.file_manifest_path,
        service.last_diff_path,
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["_state"]["generation_id"] == generation
    assert service.load_manifest()


def test_incomplete_first_generation_is_never_accepted(library_factory, monkeypatch):
    root = library_factory([], {"Registered/paper.pdf": b"one"})
    service = SnapshotService(root, root / ".library_state")
    original = service._atomic_json

    def fail_commit_marker(path, payload):
        if path == service.commit_marker_path:
            raise FileOperationError("simulated marker failure")
        original(path, payload)

    monkeypatch.setattr(service, "_atomic_json", fail_commit_marker)
    with pytest.raises(FileOperationError, match="marker failure"):
        service.commit(
            {"sheet": "Catalogue", "rows": []},
            service.scan(),
            {"mode": "initial"},
        )
    assert service.initialized is False
    assert service.load_manifest() == {}


def test_failed_later_generation_keeps_previous_commit_active(library_factory, monkeypatch):
    root = library_factory([], {"Registered/one.pdf": b"one"})
    service = SnapshotService(root, root / ".library_state")
    first_manifest = service.scan()
    service.commit(
        {"sheet": "Catalogue", "rows": []}, first_manifest, {"mode": "initial"}
    )
    first_generation = json.loads(
        service.commit_marker_path.read_text(encoding="utf-8")
    )["generation_id"]
    write_pdf(root / "Registered" / "two.pdf", b"two")
    original = service._atomic_json

    def fail_new_marker(path, payload):
        if path == service.commit_marker_path:
            raise FileOperationError("simulated later marker failure")
        original(path, payload)

    monkeypatch.setattr(service, "_atomic_json", fail_new_marker)
    with pytest.raises(FileOperationError, match="later marker failure"):
        service.commit(
            {"sheet": "Catalogue", "rows": []},
            service.scan(first_manifest),
            {"mode": "incremental"},
        )
    assert service.initialized is True
    assert set(service.load_manifest()) == set(first_manifest)
    marker = json.loads(service.commit_marker_path.read_text(encoding="utf-8"))
    assert marker["generation_id"] == first_generation


def test_hash_only_rename_is_review_candidate_not_confirmed_move(library_factory):
    root = library_factory([], {"Registered/old.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    (root / "Registered" / "old.pdf").rename(root / "Registered" / "new.pdf")
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert [item.diff_type for item in diffs] == [DiffType.POSSIBLE_COLLISION]
    assert diffs[0].details["matched_by"] == "quick_hash_candidate"
