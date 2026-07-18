from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from openpyxl import Workbook

from lam.exceptions import CatalogueError
from lam.services.catalogue_service import CatalogueService
from lam.services.catalogue_preflight_service import (
    DOCUMENT_REQUIRED_FIELDS,
    DUAL_CATALOGUE_REQUIRED_FIELDS,
    CataloguePreflightService,
)


def _write_workbook(
    path: Path,
    catalogue_headers: list[str],
    documents_headers: list[str] | None = None,
) -> None:
    workbook = Workbook()
    catalogue = workbook.active
    catalogue.title = "Catalogue"
    catalogue.append(catalogue_headers)
    if documents_headers is not None:
        documents = workbook.create_sheet("Documents")
        documents.append(documents_headers)
    workbook.save(path)
    workbook.close()


def test_dual_schema_is_recognized(tmp_path: Path):
    path = tmp_path / "catalogue.xlsx"
    _write_workbook(
        path,
        sorted(DUAL_CATALOGUE_REQUIRED_FIELDS),
        sorted(DOCUMENT_REQUIRED_FIELDS),
    )

    result = CataloguePreflightService(path).before_modification()

    assert result.schema_mode == "dual"


def test_strict_dual_schema_loads_and_accepts_new_paper_without_legacy_columns(
    tmp_path: Path,
):
    path = tmp_path / "catalogue.xlsx"
    catalogue_headers = sorted(DUAL_CATALOGUE_REQUIRED_FIELDS)
    document_headers = sorted(DOCUMENT_REQUIRED_FIELDS)
    _write_workbook(path, catalogue_headers, document_headers)

    service = CatalogueService(path)
    assert service.load() == []
    record = service.add_record(
        {
            "title": "Strict dual paper",
            "doi": "10.1000/strict-dual",
            "arxiv_id": "2607.00001",
            "source": "local_pdf",
        }
    )

    assert uuid.UUID(str(record.get("paper_uuid"))).version == 4
    assert record.get("arxiv_id") == "2607.00001"
    service.save_atomic()
    reloaded = CatalogueService(path)
    rows = reloaded.load()
    assert len(rows) == 1
    assert rows[0].get("title") == "Strict dual paper"


def test_missing_catalogue_is_rejected(tmp_path: Path):
    with pytest.raises(CatalogueError, match="missing"):
        CataloguePreflightService(tmp_path / "catalogue.xlsx").before_modification()


def test_excel_owner_file_is_rejected_without_platform_specific_file_lock(
    library_factory,
):
    root = library_factory([])
    owner_file = root / "~$catalogue.xlsx"
    owner_file.write_text("Excel owner marker", encoding="utf-8")

    with pytest.raises(CatalogueError, match="open in Excel"):
        CataloguePreflightService(root / "catalogue.xlsx").before_modification()


def test_partial_dual_schema_is_rejected(tmp_path: Path):
    path = tmp_path / "catalogue.xlsx"
    _write_workbook(path, sorted(DUAL_CATALOGUE_REQUIRED_FIELDS))

    with pytest.raises(CatalogueError, match="Partial dual-sheet migration"):
        CataloguePreflightService(path).before_modification()


def test_invalid_documents_schema_is_rejected(tmp_path: Path):
    path = tmp_path / "catalogue.xlsx"
    _write_workbook(path, sorted(DUAL_CATALOGUE_REQUIRED_FIELDS), ["document_id"])

    with pytest.raises(CatalogueError, match="Invalid dual-sheet schema"):
        CataloguePreflightService(path).before_modification()


def test_commit_rejects_changed_stat_token(library_factory):
    root = library_factory([])
    path = root / "catalogue.xlsx"
    service = CataloguePreflightService(path)
    initial = service.before_modification()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    with pytest.raises(CatalogueError, match="changed after preflight"):
        service.before_commit(initial.token)


def test_temporary_copy_failure_is_reported_and_probe_is_cleaned(
    library_factory, monkeypatch
):
    root = library_factory([])

    def fail_copy(*args, **kwargs):
        raise PermissionError("blocked")

    monkeypatch.setattr(
        "lam.services.catalogue_preflight_service.shutil.copy2", fail_copy
    )
    with pytest.raises(CatalogueError, match="temporary catalogue workbook"):
        CataloguePreflightService(root / "catalogue.xlsx").before_modification()
    assert not list(root.glob(".*.preflight-*.tmp.xlsx"))
