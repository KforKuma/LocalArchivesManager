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
