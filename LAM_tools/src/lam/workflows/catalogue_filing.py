from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import FileOperation, PdfStatus, WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from .daily_check import DailyCheckWorkflow


class CatalogueFilingWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, dry_run: bool = False) -> WorkflowResult:
        result = WorkflowResult(
            "catalogue_filing",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous_catalogue = (
            snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        )
        catalogue.configure_review_state(previous_catalogue)
        files = FileService(self.settings.library_root, self.settings.max_filename_length)
        operations: list[FileOperation] = []
        source_kinds: dict[int, str] = {}

        for record in records:
            topic = str(record.get("topic_folder") or "").strip()
            relative = str(record.get("pdf_relative_path") or "").strip()
            if not topic or topic.casefold() == "unclassified":
                result.skipped.append(
                    {"row": record.row_number, "file": relative or None, "reason": "unclassified"}
                )
                continue
            if not relative:
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "pdf_file",
                    "Cannot file because pdf_relative_path is blank.",
                    issue_key="source_missing",
                )
                self._record_review(
                    result,
                    outcome,
                    {"row": record.row_number, "issue": "source_missing"},
                )
                continue
            try:
                target_folder = files.validate_topic_folder(topic)
            except FileOperationError as exc:
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "topic_folder",
                    str(exc),
                    issue_key="unsafe_target",
                    conflict_with_confirmation=True,
                )
                self._record_review(
                    result,
                    outcome,
                    {"row": record.row_number, "file": relative, "issue": "unsafe_target"},
                )
                continue
            try:
                source = files.require_within_root(self.settings.library_root / relative)
                if not source.is_file():
                    outcome = catalogue.ensure_review_blocker(
                        record,
                        "pdf_file",
                        f"Catalogue PDF path does not exist: {relative}",
                        issue_key="source_missing",
                    )
                    self._record_review(
                        result,
                        outcome,
                        {"row": record.row_number, "file": relative, "issue": "source_missing"},
                    )
                    continue
                if source.parent == target_folder:
                    catalogue.resolve_review_blockers(
                        record,
                        "pdf_relative_path",
                        {"source_not_registered", "topic_location_mismatch"},
                        resolution="Observed PDF is already in the confirmed topic folder.",
                    )
                    result.skipped.append(
                        {"row": record.row_number, "file": relative, "reason": "already_correct"}
                    )
                    continue
                source_kind = files.workflow4_source_kind(source)
                expected_filename = str(record.get("pdf_filename") or "").strip()
                if expected_filename and expected_filename != source.name:
                    raise FileOperationError(
                        "Catalogue pdf_filename does not match the observed source filename."
                    )
                operation = files.plan_move(
                    source,
                    target_folder,
                    record.row_number,
                    "confirmed topic_folder",
                )
                operations.append(operation)
                source_kinds[record.row_number] = source_kind
            except FileOperationError as exc:
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "pdf_file",
                    str(exc),
                    issue_key="unsafe_source",
                    conflict_with_confirmation=True,
                )
                self._record_review(
                    result,
                    outcome,
                    {"row": record.row_number, "file": relative, "issue": "unsafe_source"},
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
                issue_key="target_collision",
            )
            self._record_review(
                result,
                outcome,
                {**problem, "issue": "target_collision"},
            )
        safe_operations = [op for op in operations if op.catalogue_row not in blocked_rows]

        if dry_run:
            result.completed.extend(
                {
                    "action": (
                        "would_file_from_registered"
                        if source_kinds[operation.catalogue_row] == "registered"
                        else "would_refile_from_topic"
                    ),
                    **operation.to_dict(),
                }
                for operation in safe_operations
            )
            result.details["planned_operations"] = len(safe_operations)
            result.changed_rows = len({change.row_number for change in catalogue.changes})
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = self._create_journal(catalogue, records, safe_operations, source_kinds)
        moved: list[FileOperation] = []
        today = date.today().isoformat()
        for operation in safe_operations:
            try:
                files.apply_move(operation)
                moved.append(operation)
                record = next(row for row in records if row.row_number == operation.catalogue_row)
                if journal:
                    journal.set_operation_state(
                        record.row_number,
                        "file_moved",
                        record_uid=str(record.get("record_uid") or "") or None,
                    )
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
                catalogue.resolve_review_blockers(
                    record,
                    "pdf_relative_path",
                    {"source_not_registered", "topic_location_mismatch"},
                    resolution="PDF was moved to the confirmed topic folder.",
                )
                result.completed.append(
                    {
                        "row": operation.catalogue_row,
                        "action": (
                            "filed_from_registered"
                            if source_kinds[operation.catalogue_row] == "registered"
                            else "refiled_from_topic"
                        ),
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
                if journal:
                    journal.set_operation_state(
                        operation.catalogue_row,
                        "failed",
                        error=str(exc),
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

        if journal:
            for operation in moved:
                record = next(row for row in records if row.row_number == operation.catalogue_row)
                journal.set_operation_state(
                    record.row_number,
                    "catalogue_committed",
                    record_uid=str(record.get("record_uid") or "") or None,
                )

        removed_directories: list[str] = []
        for operation in moved:
            if source_kinds[operation.catalogue_row] != "topic":
                continue
            old_directory = operation.source.parent
            try:
                removed = files.remove_empty_topic_directory(old_directory)
            except OSError as exc:
                removed = False
                result.failures.append(
                    {
                        "row": operation.catalogue_row,
                        "issue": f"Cannot inspect or remove empty source directory: {exc}",
                    }
                )
            if removed:
                relative_directory = old_directory.relative_to(
                    self.settings.library_root
                ).as_posix()
                removed_directories.append(relative_directory)
                result.completed.append(
                    {
                        "row": operation.catalogue_row,
                        "action": "removed_empty_topic_directory",
                        "directory": relative_directory,
                    }
                )
                if journal:
                    journal.set_operation_state(
                        operation.catalogue_row,
                        "catalogue_committed",
                        old_directory_removed=True,
                        old_directory=relative_directory,
                    )

        result.changed_files = len(moved)
        result.changed_rows = len({change.row_number for change in catalogue.changes})
        if moved or catalogue.changes:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 4",
                action="Catalogue-based filing",
                files_changed=len(moved),
                catalogue_rows_changed=result.changed_rows,
                reason="Filed or refiled PDFs according to confirmed topic_folder values",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        result.details["removed_empty_directories"] = removed_directories
        result.counts = {
            "catalogue_rows": len(records),
            "filed_from_registered": sum(
                item.get("action") == "filed_from_registered" for item in result.completed
            ),
            "refiled_from_topic": sum(
                item.get("action") == "refiled_from_topic" for item in result.completed
            ),
            "already_correct": sum(
                item.get("reason") == "already_correct" for item in result.skipped
            ),
            "unclassified": sum(
                item.get("reason") == "unclassified" for item in result.skipped
            ),
            "empty_directories_removed": len(removed_directories),
        }

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
        if journal:
            journal.finish("final_check_committed")
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

    def _create_journal(
        self,
        catalogue: CatalogueService,
        records: list[Any],
        operations: list[FileOperation],
        source_kinds: dict[int, str],
    ) -> OperationJournal | None:
        if not operations:
            return None
        payload = []
        for operation in operations:
            record = next(row for row in records if row.row_number == operation.catalogue_row)
            payload.append(
                {
                    **operation.to_dict(),
                    "record_uid": str(record.get("record_uid") or "") or None,
                    "source_kind": source_kinds[operation.catalogue_row],
                    "planned_updates": {
                        "pdf_status": PdfStatus.FILED.value,
                        "pdf_filename": operation.target.name,
                        "pdf_relative_path": operation.target.relative_to(
                            self.settings.library_root
                        ).as_posix(),
                    },
                    "execution_state": "planned",
                }
            )
        return OperationJournal.create(
            self.settings.state_dir,
            payload,
            workflow="catalogue_filing",
            suffix="filing",
        )

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
