import json

import pytest

from openpyxl import load_workbook

from lam.config import Settings
from lam.exceptions import CatalogueError
from lam.models import WorkflowStatus
from lam.services.catalogue_service import CatalogueService
from lam.services.file_service import FileService
from lam.workflows.inbox_register import InboxRegisterWorkflow

from conftest import write_text_pdf


def test_filename_match_registers_updates_catalogue_and_final_checks(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "A Registered Biomedical Paper",
                "year": "2025",
                "journal_abbrev": "Test J",
                "pdf_status": "inbox",
                "pdf_filename": "download.pdf",
                "pdf_relative_path": "Inbox/download.pdf",
            }
        ]
    )
    write_text_pdf(root / "Inbox" / "download.pdf", ["A Registered Biomedical Paper\nAuthors\n2025"])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    target = root / "Registered" / "Test J, 2025 - A Registered Biomedical Paper.pdf"
    assert result.status == WorkflowStatus.SUCCESS
    assert target.exists()
    assert not (root / "Inbox" / "download.pdf").exists()
    workbook = load_workbook(root / "catalogue.xlsx")
    sheet = workbook["Catalogue"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    assert sheet.cell(2, headers["pdf_status"]).value == "registered"
    assert sheet.cell(2, headers["pdf_relative_path"]).value == (
        "Registered/Test J, 2025 - A Registered Biomedical Paper.pdf"
    )
    assert result.details["manual_checkpoint_required"] is True
    assert result.details["final_check"]["status"] in {"success", "no_changes"}
    journal = next((root / ".library_state" / "runs").glob("*/operation_journal.json"))
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "final_check_committed"
    second = InboxRegisterWorkflow(settings).run()
    assert second.status == WorkflowStatus.NO_CHANGES


def test_doi_match_registers_when_filename_is_unknown(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Identifier Matched Paper",
                "doi": "10.1000/matched.1",
                "year": "2024",
                "journal": "Identifier Journal",
            }
        ]
    )
    write_text_pdf(
        root / "Inbox" / "random.pdf",
        ["Identifier Matched Paper\nAuthors\ndoi:10.1000/matched.1\n2024"],
    )
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.SUCCESS
    assert (root / "Registered" / "Identifier Journal, 2024 - Identifier Matched Paper.pdf").exists()
    assert result.details["files"][0]["match_method"] == "doi"


def test_unavailable_metadata_blocks_without_creating_catalogue_row(library_factory):
    root = library_factory([])
    write_text_pdf(root / "Inbox" / "unknown.pdf", ["Unknown Local Document\nNo identifiers"])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert (root / "Inbox" / "unknown.pdf").exists()
    assert result.details["metadata_lookup_requests"] == 1
    assert result.details["files"][0]["issue_keys"] == ["metadata_lookup_unavailable"]
    workbook = load_workbook(root / "catalogue.xlsx")
    assert workbook["Catalogue"].max_row == 1


def test_no_text_blocks_known_row_and_does_not_move(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Blank PDF",
                "year": "2025",
                "journal": "Test Journal",
                "pdf_filename": "blank.pdf",
            }
        ]
    )
    write_text_pdf(root / "Inbox" / "blank.pdf", [""])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert (root / "Inbox" / "blank.pdf").exists()
    assert any(item.get("issue") == "pdf_text_unavailable" for item in result.needs_review)


def test_one_blocked_file_does_not_prevent_other_registration(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Ready Paper",
                "year": "2025",
                "journal": "Ready Journal",
                "pdf_filename": "ready.pdf",
            }
        ]
    )
    write_text_pdf(root / "Inbox" / "ready.pdf", ["Ready Paper\nAuthors\n2025"])
    write_text_pdf(root / "Inbox" / "unknown.pdf", ["Unknown Paper"])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert (root / "Registered" / "Ready Journal, 2025 - Ready Paper.pdf").exists()
    assert (root / "Inbox" / "unknown.pdf").exists()
    assert result.changed_files == 1


def test_register_dry_run_writes_no_catalogue_snapshot_or_journal(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Dry Run Paper",
                "year": "2025",
                "journal": "Dry Journal",
                "pdf_filename": "dry.pdf",
            }
        ]
    )
    write_text_pdf(root / "Inbox" / "dry.pdf", ["Dry Run Paper\nAuthors"])
    original = (root / "catalogue.xlsx").read_bytes()
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run(dry_run=True)
    assert result.completed[0]["action"] == "would_register"
    assert (root / "Inbox" / "dry.pdf").exists()
    assert (root / "catalogue.xlsx").read_bytes() == original
    assert not (root / ".library_state" / "snapshot_commit.json").exists()
    assert not (root / ".library_state" / "runs").exists()


def test_filename_only_parses_standard_filename_without_page_text(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Standard Filename Paper",
                "year": "2025",
                "journal_abbrev": "Std J",
            }
        ]
    )
    name = "Std J, 2025 - Standard Filename Paper.pdf"
    write_text_pdf(root / "Inbox" / name, [""])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run(dry_run=True, filename_only=True)
    assert result.completed[0]["action"] == "would_register"
    assert result.details["files"][0]["match_method"] == "standard_filename_title"


def test_supplement_is_kept_in_inbox_under_single_pdf_schema(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Main Paper",
                "doi": "10.1000/main",
                "year": "2025",
                "journal": "Test Journal",
            }
        ]
    )
    write_text_pdf(
        root / "Inbox" / "Main Paper - Supporting Information.pdf",
        ["Supporting Information\ndoi:10.1000/main"],
    )
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert (root / "Inbox" / "Main Paper - Supporting Information.pdf").exists()
    assert any(item.get("issue") == "supplement_parent_unknown" for item in result.needs_review)


def test_unknown_file_blocker_state_is_stable_across_runs(library_factory):
    root = library_factory([])
    write_text_pdf(root / "Inbox" / "unknown.pdf", ["Unknown Document"])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    InboxRegisterWorkflow(settings).run()
    blocker_path = root / ".library_state" / "inbox_blockers.json"
    first = blocker_path.read_bytes()
    InboxRegisterWorkflow(settings).run()
    assert blocker_path.read_bytes() == first
    payload = json.loads(first)
    assert len(payload["files"]) == 1
    assert payload["files"][0]["issue_keys"] == ["metadata_lookup_unavailable"]


def test_source_change_before_move_is_blocked_and_other_state_is_preserved(
    library_factory, monkeypatch
):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Changing Paper",
                "year": "2025",
                "journal": "Change Journal",
                "pdf_filename": "change.pdf",
            }
        ]
    )
    source = root / "Inbox" / "change.pdf"
    write_text_pdf(source, ["Changing Paper\nAuthors"])
    original_apply = FileService.apply_registration_move

    def change_then_apply(self, operation):
        operation.source.write_bytes(operation.source.read_bytes() + b"changed")
        return original_apply(self, operation)

    monkeypatch.setattr(FileService, "apply_registration_move", change_then_apply)
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert source.exists()
    assert any(item.get("issue") == "source_changed_during_run" for item in result.needs_review)


def test_catalogue_failure_leaves_recoverable_file_moved_journal(
    library_factory, monkeypatch
):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Journal Recovery Paper",
                "year": "2025",
                "journal": "Recovery Journal",
                "pdf_filename": "recovery.pdf",
            }
        ]
    )
    write_text_pdf(root / "Inbox" / "recovery.pdf", ["Journal Recovery Paper\nAuthors"])

    def fail_save(self):
        raise CatalogueError("simulated catalogue failure")

    monkeypatch.setattr(CatalogueService, "save_atomic", fail_save)
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    with pytest.raises(CatalogueError, match="simulated catalogue failure"):
        InboxRegisterWorkflow(settings).run()
    target = root / "Registered" / "Recovery Journal, 2025 - Journal Recovery Paper.pdf"
    assert target.exists()
    journal = next((root / ".library_state" / "runs").glob("*/operation_journal.json"))
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "file_moved"
    assert payload["operations"][0]["execution_state"] == "file_moved"


def test_user_confirmed_year_is_used_and_fills_blank_metadata(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Confirmed Year Paper",
                "journal": "Confirm Journal",
                "pdf_filename": "confirm.pdf",
                "uncertainty": (
                    "USER_CONFIRMED: field=publication_year; value=2025"
                ),
            }
        ]
    )
    write_text_pdf(root / "Inbox" / "confirm.pdf", ["Confirmed Year Paper\nAuthors"])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.SUCCESS
    assert (root / "Registered" / "Confirm Journal, 2025 - Confirmed Year Paper.pdf").exists()
    workbook = load_workbook(root / "catalogue.xlsx")
    sheet = workbook["Catalogue"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    assert str(sheet.cell(2, headers["year"]).value) == "2025"


def test_only_direct_visible_inbox_pdfs_are_candidates(library_factory):
    root = library_factory([])
    write_text_pdf(root / "Inbox" / ".hidden.pdf", ["Hidden"])
    write_text_pdf(root / "Inbox" / "subfolder" / "nested.pdf", ["Nested"])
    (root / "Inbox" / "notes.txt").write_text("notes", encoding="utf-8")
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    result = InboxRegisterWorkflow(settings).run(dry_run=True)
    assert result.counts["files_discovered"] == 0
    reasons = {item["reason"] for item in result.skipped}
    assert reasons == {"hidden_or_temporary", "inbox_subdirectory", "non_pdf"}


def test_registered_filename_collision_preserves_both_files(library_factory):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Collision Paper",
                "year": "2025",
                "journal": "Collision Journal",
                "pdf_filename": "incoming.pdf",
            }
        ]
    )
    source = root / "Inbox" / "incoming.pdf"
    target = root / "Registered" / "Collision Journal, 2025 - Collision Paper.pdf"
    write_text_pdf(source, ["Collision Paper\nSource"])
    write_text_pdf(target, ["Different existing content"])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    target_before = target.read_bytes()
    result = InboxRegisterWorkflow(settings).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert source.exists()
    assert target.read_bytes() == target_before
    assert any(item.get("issue") == "registered_filename_collision" for item in result.needs_review)
