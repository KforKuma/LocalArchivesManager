from __future__ import annotations

import pytest

from lam.exceptions import FileOperationError
from lam.services.journal_service import OperationJournal


def _operations() -> list[dict[str, object]]:
    return [
        {
            "operation_id": "move-main",
            "document_id": "paper-uuid:main",
            "catalogue_row": 2,
            "record_uid": "paper-uuid",
            "execution_state": "planned",
        },
        {
            "operation_id": "move-table-01",
            "document_id": "paper-uuid:supp:table:01",
            "catalogue_row": 2,
            "record_uid": "paper-uuid",
            "execution_state": "planned",
        },
    ]


def test_precise_operation_and_document_selectors_update_only_one_entry(tmp_path):
    journal = OperationJournal.create(tmp_path, _operations())

    journal.set_operation_state(
        2,
        "file_moved",
        document_id="paper-uuid:supp:table:01",
    )
    assert [item["execution_state"] for item in journal.payload["operations"]] == [
        "planned",
        "file_moved",
    ]

    journal.set_operation_state(
        2,
        "catalogue_committed",
        operation_id="move-main",
    )
    assert [item["execution_state"] for item in journal.payload["operations"]] == [
        "catalogue_committed",
        "file_moved",
    ]


def test_precise_selector_does_not_fall_back_to_catalogue_row(tmp_path):
    journal = OperationJournal.create(tmp_path, _operations())

    with pytest.raises(FileOperationError, match="exactly one"):
        journal.set_operation_state(2, "file_moved", document_id="missing-document")

    assert {item["execution_state"] for item in journal.payload["operations"]} == {
        "planned"
    }
    assert journal.payload["status"] == "planned"


def test_legacy_catalogue_row_selector_still_updates_all_matching_entries(tmp_path):
    journal = OperationJournal.create(tmp_path, _operations())

    journal.set_operation_state(2, "file_moved")

    assert {item["execution_state"] for item in journal.payload["operations"]} == {
        "file_moved"
    }
