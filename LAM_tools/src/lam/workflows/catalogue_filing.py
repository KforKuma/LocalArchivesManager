from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import FileOperation, PdfStatus, WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from .daily_check import DailyCheckWorkflow


class CatalogueFilingWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, dry_run: bool = False) -> WorkflowResult:
        result = WorkflowResult("catalogue_filing", dry_run=dry_run, mode="dry_run" if dry_run else "apply")
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous_catalogue = (
            snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        )
        catalogue.configure_review_state(previous_catalogue)
        files = FileService(self.settings.library_root, self.settings.max_filename_length)
        operations: list[FileOperation] = []

        for record in records:
            topic = str(record.get("topic_folder") or "").strip()
            relative = str(record.get("pdf_relative_path") or "").strip()
            if not topic or topic.casefold() == "unclassified":
                result.skipped.append(
                    {"row": record.row_number, "file": relative or None, "reason": "topic_unclassified"}
                )
                continue
            if not relative:
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "pdf_file",
                    "Cannot file because pdf_relative_path is blank.",
                    issue_key="pdf_relative_path_missing",
                )
                self._record_review(
                    result,
                    outcome,
                    {"row": record.row_number, "issue": "pdf_relative_path_missing"},
                )
                continue
            try:
                target_folder = files.validate_topic_folder(topic)
                source = files.require_within_root(self.settings.library_root / relative)
                if not source.is_file():
                    issue = f"Catalogue PDF path does not exist: {relative}"
                    outcome = catalogue.ensure_review_blocker(
                        record,
                        "pdf_file",
                        issue,
                        issue_key="filing_source_missing",
                    )
                    self._record_review(
                        result,
                        outcome,
                        {"row": record.row_number, "file": relative, "issue": "filing_source_missing"},
                    )
                    continue
                if source.parent == target_folder:
                    result.skipped.append(
                        {"row": record.row_number, "file": relative, "reason": "already_filed"}
                    )
                    continue
                if source.parent != self.settings.registered_dir.resolve():
                    issue = "Workflow 4 only accepts PDFs directly from Registered."
                    outcome = catalogue.ensure_review_blocker(
                        record,
                        "pdf_relative_path",
                        issue,
                        issue_key="source_not_registered",
                        conflict_with_confirmation=True,
                    )
                    self._record_review(
                        result,
                        outcome,
                        {"row": record.row_number, "file": relative, "issue": "source_not_registered"},
                    )
                    continue
                if source.suffix.casefold() != ".pdf":
                    issue = "Workflow 4 only accepts PDF files from Registered."
                    outcome = catalogue.ensure_review_blocker(
                        record,
                        "pdf_file",
                        issue,
                        issue_key="source_not_pdf",
                        conflict_with_confirmation=True,
                    )
                    self._record_review(
                        result,
                        outcome,
                        {"row": record.row_number, "file": relative, "issue": "source_not_pdf"},
                    )
                    continue
                operation = files.plan_move(
                    source,
                    target_folder,
                    record.row_number,
                    "confirmed topic_folder",
                )
                operations.append(operation)
            except FileOperationError as exc:
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "topic_folder",
                    str(exc),
                    issue_key="unsafe_filing_target",
                    conflict_with_confirmation=True,
                )
                self._record_review(
                    result,
                    outcome,
                    {"row": record.row_number, "file": relative, "issue": str(exc)},
                )

        problems = files.validate_plan(operations)
        blocked_rows = {int(problem["row"]) for problem in problems}
        for problem in problems:
            row = next(record for record in records if record.row_number == int(problem["row"]))
            issue = f"Filing collision: {problem['issue']} at {problem['target']}"
            outcome = catalogue.ensure_review_blocker(
                row,
                "pdf_file",
                issue,
                issue_key="filing_collision",
            )
            self._record_review(result, outcome, problem)
        safe_operations = [op for op in operations if op.catalogue_row not in blocked_rows]

        if dry_run:
            result.completed.extend(
                {"action": "would_move", **operation.to_dict()} for operation in safe_operations
            )
            result.details["planned_operations"] = len(safe_operations)
            result.changed_rows = len({change.row_number for change in catalogue.changes})
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        moved: list[FileOperation] = []
        today = date.today().isoformat()
        for operation in safe_operations:
            try:
                files.apply_move(operation)
                moved.append(operation)
                record = next(row for row in records if row.row_number == operation.catalogue_row)
                relative = operation.target.relative_to(self.settings.library_root).as_posix()
                updates: dict[str, Any] = {
                    "pdf_status": PdfStatus.FILED.value,
                    "pdf_filename": operation.target.name,
                    "pdf_relative_path": relative,
                }
                updates = {key: value for key, value in updates.items() if key in catalogue.headers}
                if "date_updated" in catalogue.headers:
                    updates["date_updated"] = today
                catalogue.update_fields(record, updates)
                result.completed.append(
                    {
                        "row": operation.catalogue_row,
                        "action": "moved",
                        "source": str(operation.source.relative_to(self.settings.library_root)),
                        "target": relative,
                    }
                )
            except (FileOperationError, CatalogueError) as exc:
                if operation in moved and operation.target.exists():
                    try:
                        files.rollback_move(operation)
                        moved.remove(operation)
                    except FileOperationError as rollback_exc:
                        result.failures.append(
                            {"row": operation.catalogue_row, "issue": str(rollback_exc)}
                        )
                result.failures.append(
                    {"row": operation.catalogue_row, "issue": str(exc)}
                )

        try:
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
        except CatalogueError:
            rollback_failures = []
            for operation in reversed(moved):
                try:
                    files.rollback_move(operation)
                except FileOperationError as exc:
                    rollback_failures.append(str(exc))
            if rollback_failures:
                result.failures.extend({"issue": text} for text in rollback_failures)
            raise

        result.changed_files = len(moved)
        result.changed_rows = len({change.row_number for change in catalogue.changes})
        if moved or catalogue.changes:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 4",
                action="Catalogue-based filing",
                files_changed=len(moved),
                catalogue_rows_changed=result.changed_rows,
                reason="Moved PDFs according to confirmed topic_folder values",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        final_check = DailyCheckWorkflow(self.settings).run(dry_run=False, final_check=True)
        result.details["final_check"] = {
            "status": final_check.status.value,
            "report": final_check.report_path,
        }
        result.state_committed = final_check.state_committed
        for item in final_check.needs_review:
            if item not in result.needs_review:
                result.needs_review.append(item)
        for item in final_check.failures:
            if item not in result.failures:
                result.failures.append(item)
        affected_rows = {change.row_number for change in catalogue.changes}
        affected_rows.update(
            item["row"]
            for item in final_check.completed
            if isinstance(item.get("row"), int)
        )
        result.changed_rows = len(affected_rows)
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    @staticmethod
    def _record_review(
        result: WorkflowResult,
        outcome: str,
        item: dict[str, Any],
    ) -> None:
        if outcome in {"added", "existing"}:
            if item not in result.needs_review:
                result.needs_review.append(item)
        else:
            acknowledged = {**item, "reason": f"review_{outcome}"}
            if acknowledged not in result.skipped:
                result.skipped.append(acknowledged)
