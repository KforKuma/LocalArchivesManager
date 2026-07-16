from __future__ import annotations

import os
import json

import pytest
from pathlib import Path

from lam.models import DiffType
from lam.exceptions import FileOperationError
from lam.services.snapshot_service import SnapshotService
from lam.services.journal_service import OperationJournal

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


def test_scan_includes_supported_documents_and_keeps_scope_exclusions(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"paper"})
    (root / "Inbox" / "supp.XLSX").write_bytes(b"xlsx")
    (root / "Registered" / "data.CsV").write_bytes(b"csv")
    (root / "Registered" / "ignored.txt").write_bytes(b"text")
    (root / "Registered" / ".hidden.xls").write_bytes(b"hidden")
    (root / "Inbox" / "nested").mkdir()
    (root / "Inbox" / "nested" / "nested.csv").write_bytes(b"nested")
    (root / "Topics" / "Topic_A" / "Nested").mkdir(parents=True)
    (root / "Topics" / "Topic_A" / "table.xls").write_bytes(b"xls")
    (root / "Topics" / "Topic_A" / "Nested" / "figure.PDF").write_bytes(b"pdf")
    (root / "Topics" / "Topic_A" / ".hidden.csv").write_bytes(b"hidden")
    (root / "Topics" / ".hidden" / "hidden.xlsx").parent.mkdir()
    (root / "Topics" / ".hidden" / "hidden.xlsx").write_bytes(b"hidden")
    (root / "root_table.csv").write_bytes(b"root")

    manifest = SnapshotService(root, root / ".library_state").scan()

    assert {item.relative_path for item in manifest.values()} == {
        "Inbox/supp.XLSX",
        "Registered/data.CsV",
        "Registered/paper.pdf",
        "Topics/Topic_A/Nested/figure.PDF",
        "Topics/Topic_A/table.xls",
    }


def test_move_is_detected_by_quick_hash(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    (root / "Topics" / "Topic_A").mkdir()
    os.replace(root / "Registered" / "paper.pdf", root / "Topics" / "Topic_A" / "paper.pdf")
    current = service.scan(previous)
    diffs, unchanged = service.compare(previous, current)
    assert unchanged == 0
    assert len(diffs) == 1
    assert diffs[0].diff_type == DiffType.MOVED_OR_RENAMED
    assert diffs[0].old_path == "Registered/paper.pdf"
    assert diffs[0].new_path == "Topics/Topic_A/paper.pdf"


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
    (root / "Topics" / "Topic_A").mkdir()
    old = root / "Registered" / "paper.pdf"
    new = root / "Topics" / "Topic_A" / "paper.pdf"
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
    manifest_payload = json.loads(service.file_manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["version"] == 2
    assert service.load_manifest()


def test_load_manifest_accepts_legacy_version_one(library_factory):
    root = library_factory([])
    service = SnapshotService(root, root / ".library_state")
    service.state_dir.mkdir(parents=True)
    service.file_manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "relative_path": "Registered/legacy.pdf",
                        "filename": "legacy.pdf",
                        "size": 12,
                        "mtime_ns": 34,
                        "quick_hash": "legacy-quick-hash",
                        "full_hash": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = service.load_manifest()

    assert list(manifest) == ["registered/legacy.pdf"]
    assert manifest["registered/legacy.pdf"].filename == "legacy.pdf"


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


def test_hash_only_rename_stays_internal_candidate(library_factory):
    root = library_factory([], {"Registered/old.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    (root / "Registered" / "old.pdf").rename(root / "Registered" / "new.pdf")
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert [item.diff_type for item in diffs] == [DiffType.QUICK_HASH_CANDIDATE]
    assert diffs[0].details["matched_by"] == "quick_hash_candidate"


def test_committed_journal_move_is_expected_not_collision(library_factory):
    root = library_factory([], {"Registered/old.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    source = root / "Registered" / "old.pdf"
    target = root / "Topics" / "Topic_A" / "new.pdf"
    journal = OperationJournal.create(
        root / ".library_state",
        [
            {
                "operation_type": "move",
                "source": str(source),
                "target": str(target),
                "catalogue_row": 2,
                "execution_state": "planned",
            }
        ],
        workflow="catalogue_filing",
        suffix="filing",
    )
    target.parent.mkdir()
    source.rename(target)
    journal.set_operation_state(2, "file_moved")
    journal.set_operation_state(2, "catalogue_committed")
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert [item.diff_type for item in diffs] == [DiffType.EXPECTED_MOVE_OR_RENAME]
    assert diffs[0].details["matched_by"] == "operation_journal"


def test_mtime_only_change_is_modified(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    path = root / "Registered" / "paper.pdf"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert [item.diff_type for item in diffs] == [DiffType.MODIFIED]


def test_coexisting_identical_files_are_possible_collision(library_factory):
    root = library_factory([], {"Registered/one.pdf": b"same"})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    write_pdf(root / "Registered" / "two.pdf", b"same")
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert DiffType.POSSIBLE_COLLISION in [item.diff_type for item in diffs]
    collision = next(item for item in diffs if item.diff_type == DiffType.POSSIBLE_COLLISION)
    assert collision.details["matched_by"] == "coexisting_files"


def test_same_quick_hash_with_different_full_hash_is_not_collision(library_factory):
    edge = b"A" * (64 * 1024)
    first = b"%PDF-1.4\n" + edge + (b"X" * (64 * 1024)) + edge + b"\n%%EOF\n"
    second = b"%PDF-1.4\n" + edge + (b"Y" * (64 * 1024)) + edge + b"\n%%EOF\n"
    root = library_factory([], {"Registered/one.pdf": first})
    service = SnapshotService(root, root / ".library_state")
    previous = service.scan()
    (root / "Registered" / "two.pdf").write_bytes(second)
    current = service.scan(previous)
    diffs, _ = service.compare(previous, current)
    assert DiffType.POSSIBLE_COLLISION not in [item.diff_type for item in diffs]
    assert DiffType.ADDED in [item.diff_type for item in diffs]


def test_compare_catalogue_matches_documents_by_document_id():
    previous = {
        "rows": [],
        "documents": [
            {
                "row_number": 2,
                "document_id": "paper-uuid:main",
                "paper_uuid": "paper-uuid",
                "fields": {
                    "document_id": "paper-uuid:main",
                    "paper_uuid": "paper-uuid",
                    "filename": "old.pdf",
                },
            },
            {
                "row_number": 3,
                "document_id": "paper-uuid:supp:table:01",
                "paper_uuid": "paper-uuid",
                "fields": {
                    "document_id": "paper-uuid:supp:table:01",
                    "paper_uuid": "paper-uuid",
                    "filename": "table01.csv",
                },
            },
        ],
    }
    current = {
        "rows": [],
        "documents": [
            {
                "row_number": 8,
                "document_id": "paper-uuid:main",
                "paper_uuid": "paper-uuid",
                "fields": {
                    "document_id": "paper-uuid:main",
                    "paper_uuid": "paper-uuid",
                    "filename": "new.pdf",
                },
            },
            {
                "row_number": 9,
                "document_id": "paper-uuid:supp:figure:01",
                "paper_uuid": "paper-uuid",
                "fields": {
                    "document_id": "paper-uuid:supp:figure:01",
                    "paper_uuid": "paper-uuid",
                    "filename": "figure01.pdf",
                },
            },
        ],
    }

    changes = SnapshotService.compare_catalogue(previous, current)

    assert changes == [
        {
            "sheet": "Documents",
            "row_number": 8,
            "document_id": "paper-uuid:main",
            "paper_uuid": "paper-uuid",
            "change": "field_changed",
            "field": "filename",
            "old": "old.pdf",
            "new": "new.pdf",
        },
        {
            "sheet": "Documents",
            "row_number": 3,
            "document_id": "paper-uuid:supp:table:01",
            "paper_uuid": "paper-uuid",
            "change": "row_missing",
        },
        {
            "sheet": "Documents",
            "row_number": 9,
            "document_id": "paper-uuid:supp:figure:01",
            "paper_uuid": "paper-uuid",
            "change": "row_added",
        },
    ]


def test_compare_catalogue_keeps_record_uid_matching_compatibility():
    previous = {
        "rows": [
            {
                "row_number": 2,
                "record_uid": "stable-record-uid",
                "fields": {"record_uid": "stable-record-uid", "title": "Old"},
            }
        ]
    }
    current = {
        "rows": [
            {
                "row_number": 20,
                "record_uid": "stable-record-uid",
                "fields": {"record_uid": "stable-record-uid", "title": "New"},
            }
        ]
    }

    assert SnapshotService.compare_catalogue(previous, current) == [
        {
            "row_number": 20,
            "record_uid": "stable-record-uid",
            "change": "field_changed",
            "field": "title",
            "old": "Old",
            "new": "New",
        }
    ]
