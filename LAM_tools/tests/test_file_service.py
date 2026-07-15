from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import os

import pytest

from lam.exceptions import FileOperationError
from lam.services.file_service import FileService
from lam.utils.filename import sanitize_filename


def test_filename_sanitization_and_length():
    result = sanitize_filename('Bad: title? with * characters.pdf', max_length=30)
    assert not any(character in result for character in ':?*')
    assert len(result) <= 30
    assert result.endswith(".pdf")


def test_rejects_path_escape(library_factory):
    root = library_factory([])
    service = FileService(root)
    with pytest.raises(FileOperationError):
        service.require_within_root(root.parent / "outside.pdf")


def test_rejects_suspicious_topic_typo(library_factory):
    root = library_factory([])
    (root / "T_cell").mkdir()
    service = FileService(root)
    with pytest.raises(FileOperationError, match="suspiciously similar"):
        service.validate_topic_folder("T_cells")


def test_collision_is_reported_without_overwrite(library_factory):
    root = library_factory(
        [],
        {
            "Registered/paper.pdf": b"source",
            "Topic_A/paper.pdf": b"different",
        },
    )
    service = FileService(root)
    operation = service.plan_move(
        root / "Registered" / "paper.pdf",
        root / "Topic_A",
        2,
        "test",
    )
    problems = service.validate_plan([operation])
    assert problems[0]["issue"] == "different_target_exists"
    assert (root / "Registered" / "paper.pdf").exists()


def test_all_operations_are_blocked_when_rows_share_a_target(library_factory):
    root = library_factory(
        [],
        {
            "Registered/paper.pdf": b"source",
            "Registered/other.pdf": b"other",
        },
    )
    service = FileService(root)
    target = root / "Topic_A"
    first = service.plan_move(root / "Registered" / "paper.pdf", target, 2, "test")
    other = service.plan_move(root / "Registered" / "other.pdf", target, 3, "test")
    operations = [first, replace(other, target=first.target)]
    problems = service.validate_plan(operations)
    assert {int(problem["row"]) for problem in problems} == {2, 3}
    assert {problem["issue"] for problem in problems} == {"multiple_rows_target_same_path"}


def test_workflow4_source_allows_registered_or_topic_pdf_only(library_factory):
    root = library_factory(
        [],
        {
            "Inbox/paper.pdf": b"inbox",
            "Registered/registered.pdf": b"registered",
            "Registered/notes.txt": b"not pdf",
            "Topic_A/filed.pdf": b"filed",
        },
    )
    service = FileService(root)
    assert service.workflow4_source_kind(root / "Registered" / "registered.pdf") == "registered"
    assert service.workflow4_source_kind(root / "Topic_A" / "filed.pdf") == "topic"
    with pytest.raises(FileOperationError, match="refuses Inbox"):
        service.plan_move(root / "Inbox" / "paper.pdf", root / "Topic_A", 2, "test")
    with pytest.raises(FileOperationError, match="not a PDF"):
        service.plan_move(root / "Registered" / "notes.txt", root / "Topic_A", 3, "test")


def test_target_created_after_planning_is_never_overwritten(library_factory):
    root = library_factory([], {"Registered/paper.pdf": b"source"})
    service = FileService(root)
    operation = service.plan_move(
        root / "Registered" / "paper.pdf", root / "Topic_A", 2, "test"
    )
    operation.target.parent.mkdir()
    operation.target.write_bytes(b"late target")
    with pytest.raises(FileOperationError, match="Refusing to overwrite"):
        service.apply_move(operation)
    assert operation.source.exists()
    assert operation.target.read_bytes() == b"late target"


def test_kernel_move_rejects_target_created_after_final_check(
    library_factory, monkeypatch
):
    root = library_factory([], {"Registered/paper.pdf": b"source"})
    service = FileService(root)
    operation = service.plan_move(
        root / "Registered" / "paper.pdf", root / "Topic_A", 2, "test"
    )
    real_rename = os.rename

    def race_rename(source, target):
        Path(target).write_bytes(b"racing target")
        return real_rename(source, target)

    monkeypatch.setattr("lam.services.file_service.os.rename", race_rename)
    with pytest.raises(FileOperationError, match="Cannot move"):
        service.apply_move(operation)
    assert operation.source.exists()
    assert operation.target.read_bytes() == b"racing target"
