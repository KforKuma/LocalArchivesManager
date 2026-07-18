from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from lam.config import Settings
from lam.schema import CATALOGUE_FIELDS, DOCUMENT_FIELDS
from lam.workflows.publication_type_repair import PublicationTypeRepairWorkflow
from lam.workflows.topic_migration import TopicMigrationWorkflow


PAPER_UUID = "12345678-1234-4234-9234-123456789abc"


def _library(
    tmp_path: Path,
    *,
    relative_path: str,
    filename: str,
    publication_type: str = "",
) -> Path:
    root = tmp_path / "library"
    for name in ("Inbox", "Registered", "Topics"):
        (root / name).mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    catalogue = workbook.active
    catalogue.title = "Catalogue"
    catalogue.append(CATALOGUE_FIELDS)
    values = {
        "paper_uuid": PAPER_UUID,
        "title": "Example Paper",
        "authors": "Alice Smith",
        "year": "2025",
        "journal": "Example Journal",
        "journal_abbrev": "Example J",
        "publication_type": publication_type,
        "topic_folder": "Topic_A",
        "source": "pubmed",
        "date_added": "2026-01-01",
        "date_updated": "2026-01-01",
    }
    catalogue.append([values.get(field) for field in CATALOGUE_FIELDS])
    documents = workbook.create_sheet("Documents")
    documents.append(DOCUMENT_FIELDS)
    document = {
        "document_id": f"{PAPER_UUID}:main",
        "paper_uuid": PAPER_UUID,
        "document_type": "main",
        "filename": filename,
        "relative_path": relative_path,
        "extension": ".pdf",
        "file_status": "registered",
        "source": "local_pdf",
        "date_added": "2026-01-01",
        "date_updated": "2026-01-01",
    }
    documents.append([document.get(field) for field in DOCUMENT_FIELDS])
    workbook.save(root / "catalogue.xlsx")
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\nexample\n%%EOF\n")
    return root


def test_publication_type_repair_reads_filename_and_path_from_documents(tmp_path: Path):
    root = _library(
        tmp_path,
        relative_path="Registered/old.pdf",
        filename="old.pdf",
        publication_type="Review; Research Support, Non-U.S. Gov't",
    )
    before = (root / "catalogue.xlsx").read_bytes()

    result = PublicationTypeRepairWorkflow(Settings.from_root(root)).run(dry_run=True)

    assert result.status.value in {"success", "needs_review"}
    assert any(item.get("action") == "would_rename" for item in result.completed)
    assert (root / "catalogue.xlsx").read_bytes() == before


def test_topic_migration_plans_from_documents_relative_path(tmp_path: Path):
    root = _library(
        tmp_path,
        relative_path="Topic_A/paper.pdf",
        filename="paper.pdf",
    )
    before = (root / "catalogue.xlsx").read_bytes()

    result = TopicMigrationWorkflow(Settings.from_root(root)).run(dry_run=True)

    assert result.status.value == "success"
    assert result.details["planned_topic_directories"] == ["Topic_A"]
    assert any(
        update.get("sheet") == "Documents"
        and update.get("field") == "relative_path"
        and update.get("new") == "Topics/Topic_A/paper.pdf"
        for update in result.details["planned_catalogue_updates"]
    )
    assert (root / "catalogue.xlsx").read_bytes() == before
