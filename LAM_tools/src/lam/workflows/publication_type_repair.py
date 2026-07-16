from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import CatalogueRecord, FileOperation, PdfStatus, WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal, incomplete_journals
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from ..utils.filename import StandardFilenameResult, standard_pdf_filename_result
from ..utils.normalize import normalized_relative_path
from ..utils.publication_type import CanonicalTypeResult, canonicalize_publication_type
from .daily_check import DailyCheckWorkflow


class PublicationTypeRepairWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, dry_run: bool = False) -> WorkflowResult:
        result = WorkflowResult(
            "publication_type_repair",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous_catalogue = snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        catalogue.configure_review_state(previous_catalogue)
        unfinished = incomplete_journals(self.settings.state_dir)
        if unfinished:
            result.needs_review.extend(
                {**item, "issue": "catalogue_write_incomplete"} for item in unfinished
            )
            result.details = {"repairs": [], "blocked_by_incomplete_journal": True}
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        files = FileService(self.settings.library_root, self.settings.max_filename_length)
        planned: list[dict[str, Any]] = []
        repair_rows: list[dict[str, Any]] = []
        today = date.today().isoformat()

        for record in records:
            main_documents = [
                document
                for document in catalogue.documents_for_paper(record.get("paper_uuid"))
                if str(document.get("document_type") or "").casefold() == "main"
            ]
            document = main_documents[0] if len(main_documents) == 1 else None
            old_type = record.get("publication_type", None)
            type_result = canonicalize_publication_type(old_type)
            row_report = self._row_report(record, document, old_type, type_result)
            repair_rows.append(row_report)

            catalogue.repair_publication_type(record, old_type)
            type_warning = self._record_type_warning(
                catalogue, record, type_result, result, row_report
            )
            if not type_warning:
                self._clear_resolved_type_review(catalogue, record)

            relative = str(document.get("relative_path") or "").strip() if document else ""
            status = str(document.get("file_status") or "").strip().casefold() if document else ""
            filename_result = self._filename_result(record, type_result)
            row_report.update(
                {
                    "new_filename": filename_result.filename,
                    "new_title_truncated": filename_result.title_truncated,
                    "title_truncation_changed": (
                        row_report["old_title_truncated"] != filename_result.title_truncated
                    ),
                }
            )

            if filename_result.filename is None:
                if status == PdfStatus.REGISTERED.value:
                    self._add_review(
                        catalogue,
                        record,
                        result,
                        row_report,
                        "publication_type_file_missing",
                        "Standard filename cannot be generated because naming metadata is incomplete.",
                    )
                continue

            path_key = normalized_relative_path(relative)
            source = self.settings.library_root / Path(relative) if relative else None
            if status == PdfStatus.REGISTERED.value:
                if (
                    not path_key.startswith("registered/")
                    or source is None
                    or source.parent.resolve() != self.settings.registered_dir.resolve()
                    or not source.is_file()
                ):
                    self._add_review(
                        catalogue,
                        record,
                        result,
                        row_report,
                        "publication_type_file_missing",
                        "Registered catalogue path is missing or is not a direct Registered PDF.",
                    )
                    continue
                old_filename = source.name
                row_report["old_filename"] = old_filename
                row_report["old_title_truncated"] = len(old_filename) >= self.settings.max_filename_length
                row_report["title_truncation_changed"] = (
                    row_report["old_title_truncated"] != filename_result.title_truncated
                )
                if old_filename == filename_result.filename:
                    row_report["action"] = "type_only" if old_type != type_result.canonical_type else "unchanged"
                    continue
                try:
                    operation = files.plan_registered_rename(
                        source,
                        filename_result.filename,
                        record.row_number,
                        "normalize publication type and restore canonical title budget",
                    )
                except FileOperationError as exc:
                    self._add_review(
                        catalogue,
                        record,
                        result,
                        row_report,
                        "publication_type_file_missing",
                        str(exc),
                    )
                    continue
                row_report["action"] = "would_rename" if dry_run else "planned"
                planned.append(
                    {
                        "record": record,
                        "document": document,
                        "operation": operation,
                        "filename_result": filename_result,
                        "row_report": row_report,
                    }
                )
            elif relative and source is not None and source.is_file():
                try:
                    top = source.resolve().relative_to(self.settings.library_root.resolve()).parts[0]
                except (ValueError, IndexError):
                    top = ""
                if top.casefold() not in {"inbox", "registered"} and source.name != filename_result.filename:
                    row_report["action"] = "topic_directory_deferred"
                    item = {
                        "row": record.row_number,
                        "file": relative,
                        "issue": "topic_directory_rename_deferred",
                        "proposed_filename": filename_result.filename,
                    }
                    if item not in result.needs_review:
                        result.needs_review.append(item)

        problems = files.validate_plan([item["operation"] for item in planned])
        blocked_rows = {int(problem["row"]) for problem in problems}
        by_row = {record.row_number: record for record in records}
        report_by_row = {int(item["row"]): item for item in repair_rows}
        for problem in problems:
            row_number = int(problem["row"])
            self._add_review(
                catalogue,
                by_row[row_number],
                result,
                report_by_row[row_number],
                "publication_type_repair_collision",
                f"Repair target collision: {problem['issue']} at {problem['target']}",
            )
        ready = [item for item in planned if item["record"].row_number not in blocked_rows]

        planned_rows = {change.row_number for change in catalogue.changes}
        result.details = {
            "repairs": repair_rows,
            "planned_renames": len(ready),
            "blocked_renames": len(blocked_rows),
            "final_check": {},
        }
        if dry_run:
            result.completed.extend(
                {
                    "row": item["record"].row_number,
                    "action": "would_rename",
                    "source": item["operation"].source.name,
                    "target": item["operation"].target.name,
                }
                for item in ready
            )
            result.changed_files = len(ready)
            result.changed_rows = len(planned_rows)
            result.counts = self._counts(records, repair_rows, ready, blocked_rows)
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = self._create_journal(ready) if ready else None
        moved: list[dict[str, Any]] = []
        for item in ready:
            operation = item["operation"]
            record = item["record"]
            try:
                files.apply_registered_rename(operation)
                moved.append(item)
                if journal:
                    journal.set_operation_state(record.row_number, "file_moved")
                relative = operation.target.relative_to(self.settings.library_root).as_posix()
                catalogue.update_document_fields(
                    item["document"],
                    {
                        "filename": operation.target.name,
                        "relative_path": relative,
                        "date_updated": today,
                    },
                )
                item["row_report"]["action"] = "renamed"
                result.completed.append(
                    {
                        "row": record.row_number,
                        "action": "renamed",
                        "source": operation.source.name,
                        "target": operation.target.name,
                    }
                )
            except (FileOperationError, CatalogueError) as exc:
                if journal:
                    journal.set_operation_state(record.row_number, "failed", error=str(exc))
                self._add_review(
                    catalogue,
                    record,
                    result,
                    item["row_report"],
                    "publication_type_repair_collision",
                    str(exc),
                )

        changed_rows = {change.row_number for change in catalogue.changes}
        for record in records:
            if record.row_number in changed_rows and "date_updated" in catalogue.headers:
                if record.get("date_updated") != today:
                    catalogue.update_fields(record, {"date_updated": today})
        try:
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            if catalogue.maintenance_actions:
                result.details["backup_maintenance"] = list(
                    catalogue.maintenance_actions
                )
        except CatalogueError:
            for item in reversed(moved):
                files.rollback_move(item["operation"])
            raise

        if journal:
            for item in moved:
                journal.set_operation_state(item["record"].row_number, "catalogue_committed")
        result.changed_files = len(moved)
        result.changed_rows = len({change.row_number for change in catalogue.changes})
        result.counts = self._counts(records, repair_rows, moved, blocked_rows)
        if catalogue.changes or moved:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Publication type repair",
                action="Normalize publication types and repair Registered filenames",
                files_changed=len(moved),
                catalogue_rows_changed=result.changed_rows,
                reason="Canonicalized article genres and removed provider/index labels from filenames",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        final_check = DailyCheckWorkflow(self.settings).run(dry_run=False, final_check=True)
        result.details["final_check"] = {
            "status": final_check.status.value,
            "report": final_check.report_path,
            "changed_rows": final_check.changed_rows,
        }
        result.state_committed = final_check.state_committed
        if journal:
            journal.finish("final_check_committed")
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _filename_result(
        self, record: CatalogueRecord, type_result: CanonicalTypeResult
    ) -> StandardFilenameResult:
        return standard_pdf_filename_result(
            title=record.get("title"),
            year=record.get("year"),
            journal_abbrev=record.get("journal_abbrev"),
            journal=record.get("journal"),
            publication_type=type_result.canonical_type,
            max_length=self.settings.max_filename_length,
        )

    def _row_report(
        self,
        record: CatalogueRecord,
        document: Any,
        old_type: Any,
        type_result: CanonicalTypeResult,
    ) -> dict[str, Any]:
        old_filename = str(document.get("filename") or "") if document else ""
        return {
            "row": record.row_number,
            "paper_uuid": record.get("paper_uuid"),
            "old_publication_type": old_type,
            "new_publication_type": type_result.canonical_type,
            "raw_publication_types": list(type_result.raw_types),
            "old_filename": old_filename or None,
            "new_filename": None,
            "old_title_truncated": len(old_filename) >= self.settings.max_filename_length,
            "new_title_truncated": False,
            "title_truncation_changed": False,
            "warnings": list(type_result.warnings),
            "blockers": [],
            "action": "unchanged",
        }

    def _record_type_warning(
        self,
        catalogue: CatalogueService,
        record: CatalogueRecord,
        type_result: CanonicalTypeResult,
        result: WorkflowResult,
        row_report: dict[str, Any],
    ) -> bool:
        warning = next(
            (
                item
                for item in type_result.warnings
                if item in {"publication_type_conflict", "publication_type_unrecognized"}
            ),
            None,
        )
        if not warning:
            return False
        issue = (
            "Multiple equally ranked publication genres conflict."
            if warning == "publication_type_conflict"
            else "Publication type contains an unrecognized value."
        )
        self._add_review(catalogue, record, result, row_report, warning, issue)
        return True

    @staticmethod
    def _clear_resolved_type_review(
        catalogue: CatalogueService, record: CatalogueRecord
    ) -> None:
        current = str(record.get("uncertainty") or "")
        if not current:
            return
        lines = [line for line in current.splitlines() if line.strip()]
        retained = [
            line
            for line in lines
            if not (
                line.lstrip().upper().startswith("NEEDS_REVIEW:")
                and re.search(r"\bfield=publication_type(?:;|$)", line, re.I)
            )
        ]
        if retained != lines:
            catalogue.update_fields(record, {"uncertainty": "\n".join(retained)})

    @staticmethod
    def _add_review(
        catalogue: CatalogueService,
        record: CatalogueRecord,
        result: WorkflowResult,
        row_report: dict[str, Any],
        issue_key: str,
        issue: str,
    ) -> None:
        field_name = (
            "publication_type"
            if issue_key in {"publication_type_conflict", "publication_type_unrecognized"}
            else "pdf_file"
        )
        outcome = catalogue.ensure_review_blocker(
            record,
            field_name,
            issue,
            issue_key=issue_key,
        )
        blocker = {"issue": issue_key, "details": issue, "outcome": outcome}
        if blocker not in row_report["blockers"]:
            row_report["blockers"].append(blocker)
        item = {"row": record.row_number, "issue": issue_key, "details": issue}
        if outcome in {"added", "existing"} and item not in result.needs_review:
            result.needs_review.append(item)

    def _create_journal(self, ready: list[dict[str, Any]]) -> OperationJournal:
        operations = [
            {
                **item["operation"].to_dict(),
                "source_fingerprint": {
                    "size": item["operation"].expected_size,
                    "mtime_ns": item["operation"].expected_mtime_ns,
                },
                "planned_updates": {
                    "publication_type": item["filename_result"].publication_type,
                    "filename": item["operation"].target.name,
                    "relative_path": item["operation"].target.relative_to(
                        self.settings.library_root
                    ).as_posix(),
                },
                "execution_state": "planned",
            }
            for item in ready
        ]
        return OperationJournal.create(
            self.settings.state_dir,
            operations,
            workflow="publication_type_repair",
            suffix="publication-types",
        )

    @staticmethod
    def _counts(
        records: list[CatalogueRecord],
        repair_rows: list[dict[str, Any]],
        operations: list[Any],
        blocked_rows: set[int],
    ) -> dict[str, int]:
        return {
            "catalogue_rows": len(records),
            "types_changed": sum(
                item["old_publication_type"] != item["new_publication_type"]
                for item in repair_rows
            ),
            "files_renamed_or_planned": len(operations),
            "title_truncation_changes": sum(
                bool(item["title_truncation_changed"]) for item in repair_rows
            ),
            "blocked": len(blocked_rows)
            + sum(bool(item["blockers"]) for item in repair_rows if item["row"] not in blocked_rows),
        }
