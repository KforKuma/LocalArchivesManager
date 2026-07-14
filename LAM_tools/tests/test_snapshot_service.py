from __future__ import annotations

import os
from pathlib import Path

from lam.models import DiffType
from lam.services.snapshot_service import SnapshotService

from conftest import write_pdf


def test_scan_excludes_management_directories(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"one"})
    write_pdf(root / "LAM_tools" / "hidden.pdf", b"hidden")
    write_pdf(root / ".library_state" / "hidden.pdf", b"hidden")
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

