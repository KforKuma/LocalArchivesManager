from __future__ import annotations

from pathlib import Path

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
            "Other/paper.pdf": b"other",
        },
    )
    service = FileService(root)
    target = root / "Topic_A"
    operations = [
        service.plan_move(root / "Registered" / "paper.pdf", target, 2, "test"),
        service.plan_move(root / "Other" / "paper.pdf", target, 3, "test"),
    ]
    problems = service.validate_plan(operations)
    assert {int(problem["row"]) for problem in problems} == {2, 3}
    assert {problem["issue"] for problem in problems} == {"multiple_rows_target_same_path"}
