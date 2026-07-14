from __future__ import annotations

from openpyxl import load_workbook

from lam.config import Settings
from lam.models import WorkflowStatus
from lam.workflows.catalogue_filing import CatalogueFilingWorkflow


def test_registered_pdf_is_filed_and_final_checked(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Example",
                "topic_folder": "Topic_A",
                "pdf_status": "registered",
                "pdf_filename": "paper.pdf",
                "pdf_relative_path": "Registered/paper.pdf",
            }
        ],
        {"Registered/paper.pdf": b"paper"},
    )
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = CatalogueFilingWorkflow(settings).run()
    workbook = load_workbook(root / "catalogue.xlsx")
    sheet = workbook["Catalogue"]
    assert result.status == WorkflowStatus.SUCCESS
    assert not (root / "Registered" / "paper.pdf").exists()
    assert (root / "Topic_A" / "paper.pdf").exists()
    assert sheet["H2"].value == "filed"
    assert sheet["J2"].value == "Topic_A/paper.pdf"
    assert result.details["final_check"]["status"] in {"success", "no_changes"}


def test_dry_run_plans_move_without_changes(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Example",
                "topic_folder": "Topic_A",
                "pdf_status": "registered",
                "pdf_filename": "paper.pdf",
                "pdf_relative_path": "Registered/paper.pdf",
            }
        ],
        {"Registered/paper.pdf": b"paper"},
    )
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = CatalogueFilingWorkflow(settings).run(dry_run=True)
    assert result.status == WorkflowStatus.SUCCESS
    assert result.completed[0]["action"] == "would_move"
    assert (root / "Registered" / "paper.pdf").exists()
    assert not (root / "Topic_A").exists()
    assert not list(root.glob("catalogue.backup.*.xlsx"))
    assert not (root / ".library_state" / "file_manifest.json").exists()


def test_unclassified_and_collision_are_left_in_place(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Unclassified",
                "topic_folder": "Unclassified",
                "pdf_status": "registered",
                "pdf_filename": "one.pdf",
                "pdf_relative_path": "Registered/one.pdf",
            },
            {
                "id": "P2",
                "title": "Collision",
                "topic_folder": "Topic_A",
                "pdf_status": "registered",
                "pdf_filename": "two.pdf",
                "pdf_relative_path": "Registered/two.pdf",
            },
        ],
        {
            "Registered/one.pdf": b"one",
            "Registered/two.pdf": b"source",
            "Topic_A/two.pdf": b"different",
        },
    )
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = CatalogueFilingWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert (root / "Registered" / "one.pdf").exists()
    assert (root / "Registered" / "two.pdf").exists()
    assert (root / "Topic_A" / "two.pdf").read_bytes().find(b"different") >= 0
