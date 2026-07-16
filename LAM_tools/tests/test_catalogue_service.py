from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from lam.exceptions import CatalogueError
from lam.services.catalogue_service import CatalogueService


def test_targeted_write_preserves_sheet_style_and_extra_content(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Example",
                "manual_tags": "keep",
                "topic_folder": "Topic_A",
                "pdf_status": "registered",
                "pdf_filename": "paper.pdf",
                "pdf_relative_path": "Registered/paper.pdf",
                "notes": "user note",
                "custom_column": "custom",
            }
        ]
    )
    service = CatalogueService(root / "catalogue.xlsx")
    record = service.load()[0]
    service.update_fields(record, {"pdf_status": "missing"})
    backup = service.save_atomic()

    assert backup and backup.exists()
    workbook = load_workbook(root / "catalogue.xlsx")
    sheet = workbook["Catalogue"]
    assert sheet["H2"].value == "missing"
    assert sheet["F2"].value == "keep"
    assert sheet["L2"].value == "user note"
    assert sheet["N2"].value == "custom"
    assert sheet["A1"].font.bold is True
    assert workbook["Other sheet"]["A1"].value == "preserve me"


def test_user_controlled_field_cannot_be_overwritten(library_factory):
    root = library_factory([{"id": "P1", "title": "Example", "topic_folder": "Topic_A"}])
    service = CatalogueService(root / "catalogue.xlsx")
    record = service.load()[0]
    with pytest.raises(CatalogueError):
        service.update_fields(record, {"topic_folder": "Topic_B"})


def test_uncertainty_is_deduplicated_and_confirmation_is_preserved(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Example",
                "topic_folder": "Topic_A",
                "uncertainty": (
                    "free user text\n"
                    "USER_CONFIRMED: field=topic_folder; value=Topic_A; note=keep"
                ),
            }
        ]
    )
    service = CatalogueService(root / "catalogue.xlsx")
    record = service.load()[0]
    assert service.add_uncertainty(
        record,
        "NEEDS_REVIEW:",
        "topic_folder",
        "classification questionable",
    ) is False
    assert service.add_uncertainty(
        record,
        "NEEDS_REVIEW:",
        "pdf_file",
        "PDF missing",
    ) is True
    assert service.add_uncertainty(
        record,
        "NEEDS_REVIEW:",
        "pdf_file",
        "PDF missing",
    ) is False
    value = record.get("uncertainty")
    assert "free user text" in value
    assert "USER_CONFIRMED:" in value
    assert value.count("PDF missing") == 1


def test_rapid_successive_writes_never_overwrite_a_backup(library_factory):
    root = library_factory(
        [{"id": "P1", "title": "Example", "topic_folder": "Topic_A"}]
    )
    first = CatalogueService(root / "catalogue.xlsx")
    first_record = first.load()[0]
    first.update_fields(first_record, {"pdf_status": "missing"})
    first_backup = first.save_atomic()

    second = CatalogueService(root / "catalogue.xlsx")
    second_record = second.load()[0]
    second.update_fields(second_record, {"pdf_status": "registered"})
    second_backup = second.save_atomic()

    assert first_backup != second_backup
    assert first_backup.exists()
    assert second_backup.exists()
    assert len(list(root.glob("catalogue.backup.*.xlsx"))) == 2


def test_one_active_review_per_field_and_empty_confirmation_resolves_it(library_factory):
    root = library_factory([{"id": "P1", "title": "Example", "topic_folder": "Topic_A"}])
    service = CatalogueService(root / "catalogue.xlsx")
    record = service.load()[0]
    first = service.ensure_review_blocker(
        record, "pdf_file", "PDF missing", issue_key="missing"
    )
    second = service.ensure_review_blocker(
        record, "pdf_file", "A newer missing message", issue_key="missing_new"
    )
    assert first == "added"
    assert second == "existing"
    assert str(record.get("uncertainty")).count("NEEDS_REVIEW:") == 1

    service.update_fields(
        record,
        {
            "uncertainty": (
                str(record.get("uncertainty"))
                + "\nUSER_CONFIRMED: field=pdf_file; value="
            )
        },
    )
    outcome = service.ensure_review_blocker(
        record, "pdf_file", "PDF missing", issue_key="missing"
    )
    assert outcome == "confirmed"
    assert "NEEDS_REVIEW:" not in str(record.get("uncertainty"))
    assert "USER_CONFIRMED:" in str(record.get("uncertainty"))


def test_user_cleared_review_is_remembered_by_snapshot(library_factory):
    root = library_factory([{"id": "P1", "title": "Example", "topic_folder": "Topic_A"}])
    first = CatalogueService(root / "catalogue.xlsx")
    record = first.load()[0]
    first.ensure_review_blocker(record, "pdf_file", "PDF missing", issue_key="missing")
    previous = first.snapshot_payload()
    first.save_atomic()

    workbook = load_workbook(root / "catalogue.xlsx")
    workbook["Catalogue"]["M2"] = None
    workbook.save(root / "catalogue.xlsx")

    second = CatalogueService(root / "catalogue.xlsx")
    record = second.load()[0]
    second.configure_review_state(previous)
    outcome = second.ensure_review_blocker(
        record, "pdf_file", "PDF missing", issue_key="missing"
    )
    assert outcome == "cleared"
    assert not record.get("uncertainty")


def test_blank_bibliographic_field_can_be_filled_but_not_overwritten(library_factory):
    root = library_factory([{"id": "P1", "title": "", "topic_folder": "Topic_A"}])
    service = CatalogueService(root / "catalogue.xlsx")
    record = service.load()[0]
    service.update_fields(record, {"title": "Confirmed Title"})
    with pytest.raises(CatalogueError, match="non-empty bibliographic"):
        service.update_fields(record, {"title": "Conflicting Title"})


def test_backup_retention_failure_does_not_rollback_valid_save(
    library_factory, monkeypatch
):
    root = library_factory(
        [{"id": "P1", "title": "Example", "topic_folder": "Topic_A"}]
    )
    service = CatalogueService(root / "catalogue.xlsx")
    record = service.load()[0]
    service.update_fields(record, {"pdf_status": "registered"})

    def fail_retention(*, keep: int) -> None:
        raise OSError(f"retention failed for keep={keep}")

    monkeypatch.setattr(service, "_prune_valid_backups", fail_retention)
    backup = service.save_atomic()

    assert backup is not None and backup.exists()
    reloaded = CatalogueService(root / "catalogue.xlsx")
    assert reloaded.load()[0].get("pdf_status") == "registered"
    assert any(
        item.get("action") == "catalogue_backup_cleanup_failed"
        for item in service.maintenance_actions
    )
