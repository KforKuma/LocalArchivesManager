from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import FileOperation, OperationType, PdfStatus, WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from ..utils.supplementary import is_supported_document_extension
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
        files = FileService(
            self.settings.library_root,
            self.settings.max_filename_length,
            self.settings.reserved_root_directories,
        )
        return self._run_documents(catalogue, records, files, result)
    def _run_documents(
        self,
        catalogue: CatalogueService,
        records: list[Any],
        files: FileService,
        result: WorkflowResult,
    ) -> WorkflowResult:
        """File every physical document for one paper as an atomic group."""
        record_by_uuid = {
            str(record.get("paper_uuid") or "").strip(): record
            for record in records
            if str(record.get("paper_uuid") or "").strip()
        }
        planned: dict[str, list[tuple[Any, FileOperation, str]]] = {}
        already_correct: dict[str, list[Any]] = {}
        blocked: set[str] = set()
        target_entries: dict[str, list[tuple[str, Any, FileOperation]]] = {}

        for paper_uuid, record in record_by_uuid.items():
            documents = catalogue.documents_for_paper(paper_uuid)
            topic = str(record.get("topic_folder") or "").strip()
            if not topic or topic.casefold() == "unclassified":
                result.skipped.append(
                    {
                        "row": record.row_number,
                        "paper_uuid": paper_uuid,
                        "reason": "unclassified",
                    }
                )
                continue
            if not documents:
                blocked.add(paper_uuid)
                result.needs_review.append(
                    {
                        "row": record.row_number,
                        "paper_uuid": paper_uuid,
                        "issue": "paper_has_no_documents",
                    }
                )
                continue
            try:
                target_folder = files.validate_topic_folder(topic)
            except FileOperationError as exc:
                blocked.add(paper_uuid)
                result.needs_review.append(
                    {
                        "row": record.row_number,
                        "paper_uuid": paper_uuid,
                        "issue": "unsafe_target",
                        "detail": str(exc),
                    }
                )
                if not result.dry_run:
                    for document in documents:
                        self._set_document_issue(
                            catalogue, document, "document_unsafe_target"
                        )
                continue

            group: list[tuple[Any, FileOperation, str]] = []
            correct: list[Any] = []
            for document in documents:
                document_id = str(document.get("document_id") or "")
                relative = str(document.get("relative_path") or "").strip()
                try:
                    if not relative:
                        raise FileOperationError("Document relative_path is blank")
                    source = files.require_within_root(
                        self.settings.library_root / relative
                    )
                    if not source.is_file():
                        raise FileOperationError(
                            f"Document path does not exist: {relative}"
                        )
                    source_kind = self._document_source_kind(files, source)
                    expected_filename = str(document.get("filename") or "").strip()
                    if expected_filename and expected_filename != source.name:
                        raise FileOperationError(
                            "Documents filename does not match the observed source filename"
                        )
                    if source.parent == target_folder:
                        correct.append(document)
                        continue
                    target = files.require_within_root(target_folder / source.name)
                    stat = source.stat()
                    operation = FileOperation(
                        OperationType.MOVE,
                        source,
                        target,
                        record.row_number,
                        "confirmed topic_folder for paper documents",
                        expected_size=stat.st_size,
                        expected_mtime_ns=stat.st_mtime_ns,
                    )
                    group.append((document, operation, source_kind))
                    target_entries.setdefault(str(target).casefold(), []).append(
                        (paper_uuid, document, operation)
                    )
                except FileOperationError as exc:
                    blocked.add(paper_uuid)
                    issue = (
                        "document_file_missing"
                        if "does not exist" in str(exc) or "blank" in str(exc)
                        else "document_unsafe_source"
                    )
                    result.needs_review.append(
                        {
                            "row": record.row_number,
                            "paper_uuid": paper_uuid,
                            "document_id": document_id,
                            "file": relative or None,
                            "issue": issue,
                            "detail": str(exc),
                        }
                    )
                    if not result.dry_run:
                        updates: dict[str, Any] = {}
                        if issue == "document_file_missing":
                            updates["file_status"] = PdfStatus.MISSING.value
                        if updates:
                            self._update_document(catalogue, document, updates)
                        self._set_document_issue(catalogue, document, issue)
            planned[paper_uuid] = group
            already_correct[paper_uuid] = correct

        for target_key, entries in target_entries.items():
            target = entries[0][2].target
            if len(entries) > 1 or target.exists():
                for paper_uuid, document, _operation in entries:
                    blocked.add(paper_uuid)
                issue = (
                    "multiple_documents_target_same_path"
                    if len(entries) > 1
                    else "document_target_collision"
                )
                for paper_uuid, document, _operation in entries:
                    item = {
                        "row": record_by_uuid[paper_uuid].row_number,
                        "paper_uuid": paper_uuid,
                        "document_id": document.get("document_id"),
                        "target": target.as_posix(),
                        "issue": "target_collision",
                        "detail": issue,
                    }
                    if item not in result.needs_review:
                        result.needs_review.append(item)
                    if not result.dry_run:
                        self._set_document_issue(
                            catalogue, document, "document_target_collision"
                        )

        safe_groups = {
            paper_uuid: group
            for paper_uuid, group in planned.items()
            if paper_uuid not in blocked
        }
        safe_operations = [entry for group in safe_groups.values() for entry in group]
        result.details["planned_operations"] = len(safe_operations)
        result.details["blocked_paper_groups"] = sorted(blocked)

        if result.dry_run:
            for paper_uuid, group in safe_groups.items():
                if not group:
                    result.skipped.append(
                        {
                            "row": record_by_uuid[paper_uuid].row_number,
                            "paper_uuid": paper_uuid,
                            "reason": "already_correct",
                        }
                    )
                    continue
                for document, operation, source_kind in group:
                    result.completed.append(
                        {
                            "action": (
                                "would_file_from_registered"
                                if source_kind == "registered"
                                else "would_refile_from_topic"
                            ),
                            "paper_uuid": paper_uuid,
                            "document_id": document.get("document_id"),
                            **operation.to_dict(),
                        }
                    )
            result.changed_rows = 0
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = self._create_document_journal(safe_groups)
        moved: list[tuple[str, Any, FileOperation, str]] = []
        successful_groups: set[str] = set()
        today = date.today().isoformat()
        for paper_uuid, group in safe_groups.items():
            group_moved: list[tuple[str, Any, FileOperation, str]] = []
            try:
                for document, operation, source_kind in group:
                    self._apply_document_move(files, operation)
                    item = (paper_uuid, document, operation, source_kind)
                    moved.append(item)
                    group_moved.append(item)
                    if journal:
                        journal.set_operation_state(
                            operation.catalogue_row,
                            "file_moved",
                            document_id=str(document.get("document_id") or ""),
                        )
            except FileOperationError as exc:
                for item in reversed(group_moved):
                    try:
                        files.rollback_move(item[2])
                        moved.remove(item)
                    except FileOperationError as rollback_exc:
                        result.failures.append(
                            {
                                "paper_uuid": paper_uuid,
                                "document_id": item[1].get("document_id"),
                                "issue": str(rollback_exc),
                            }
                        )
                result.failures.append(
                    {
                        "paper_uuid": paper_uuid,
                        "issue": "document_group_move_failed",
                        "detail": str(exc),
                    }
                )
                if journal:
                    for document, operation, _source_kind in group:
                        journal.set_operation_state(
                            operation.catalogue_row,
                            "failed",
                            document_id=str(document.get("document_id") or ""),
                            error=str(exc),
                        )
                continue

            successful_groups.add(paper_uuid)
            for document in already_correct.get(paper_uuid, []):
                self._update_document(
                    catalogue,
                    document,
                    {"file_status": PdfStatus.FILED.value, "date_updated": today},
                )
                self._clear_document_filing_issues(catalogue, document)
            for document, operation, source_kind in group:
                relative = operation.target.relative_to(
                    self.settings.library_root
                ).as_posix()
                self._update_document(
                    catalogue,
                    document,
                    {
                        "filename": operation.target.name,
                        "relative_path": relative,
                        "file_status": PdfStatus.FILED.value,
                        "date_updated": today,
                    },
                )
                self._clear_document_filing_issues(catalogue, document)
                result.completed.append(
                    {
                        "row": operation.catalogue_row,
                        "paper_uuid": paper_uuid,
                        "document_id": document.get("document_id"),
                        "action": (
                            "filed_from_registered"
                            if source_kind == "registered"
                            else "refiled_from_topic"
                        ),
                        "source": operation.source.relative_to(
                            self.settings.library_root
                        ).as_posix(),
                        "target": relative,
                    }
                )
            if not group:
                result.skipped.append(
                    {
                        "row": record_by_uuid[paper_uuid].row_number,
                        "paper_uuid": paper_uuid,
                        "reason": "already_correct",
                    }
                )

        try:
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            if catalogue.maintenance_actions:
                result.details["backup_maintenance"] = list(
                    catalogue.maintenance_actions
                )
        except CatalogueError:
            rollback_failures = []
            for _paper_uuid, _document, operation, _source_kind in reversed(moved):
                try:
                    files.rollback_move(operation)
                except FileOperationError as exc:
                    rollback_failures.append(str(exc))
            result.failures.extend({"issue": text} for text in rollback_failures)
            raise

        if journal:
            for _paper_uuid, document, operation, _source_kind in moved:
                journal.set_operation_state(
                    operation.catalogue_row,
                    "catalogue_committed",
                    document_id=str(document.get("document_id") or ""),
                )

        removed_directories: list[str] = []
        old_directories = {
            operation.source.parent
            for _paper_uuid, _document, operation, source_kind in moved
            if source_kind == "topic"
        }
        for old_directory in sorted(
            old_directories, key=lambda item: len(item.parts), reverse=True
        ):
            try:
                removed = files.remove_empty_topic_directory(old_directory)
            except OSError as exc:
                removed = False
                result.failures.append(
                    {
                        "directory": old_directory.as_posix(),
                        "issue": f"Cannot inspect or remove empty source directory: {exc}",
                    }
                )
            if removed:
                relative_directory = old_directory.relative_to(
                    self.settings.library_root
                ).as_posix()
                removed_directories.append(relative_directory)
                if journal:
                    for _paper_uuid, document, operation, _source_kind in moved:
                        if operation.source.parent == old_directory:
                            journal.set_operation_state(
                                operation.catalogue_row,
                                "catalogue_committed",
                                document_id=str(document.get("document_id") or ""),
                                old_directory_removed=True,
                                old_directory=relative_directory,
                            )
                result.completed.append(
                    {
                        "action": "removed_empty_topic_directory",
                        "directory": relative_directory,
                    }
                )

        result.changed_files = len(moved)
        result.changed_rows = len(
            {change.row_number for change in catalogue.document_changes}
        )
        if moved or catalogue.document_changes:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 4",
                action="Document-based filing",
                files_changed=len(moved),
                catalogue_rows_changed=result.changed_rows,
                reason="Filed every document for each confirmed paper topic",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        result.details["removed_empty_directories"] = removed_directories
        result.counts = {
            "catalogue_rows": len(records),
            "document_rows": len(catalogue.documents),
            "paper_groups_filed": len(successful_groups),
            "paper_groups_blocked": len(blocked),
            "documents_moved": len(moved),
            "already_correct": sum(
                item.get("reason") == "already_correct" for item in result.skipped
            ),
            "unclassified": sum(
                item.get("reason") == "unclassified" for item in result.skipped
            ),
            "empty_directories_removed": len(removed_directories),
        }

        final_check = DailyCheckWorkflow(self.settings).run(
            dry_run=False, final_check=True
        )
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
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _document_source_kind(self, files: FileService, source: Path) -> str:
        source = files.require_within_root(source)
        if not source.is_file() or not is_supported_document_extension(source.suffix):
            raise FileOperationError(
                f"Workflow 4 source is not a supported document: {source}"
            )
        if source.is_symlink() or files._is_reparse_point(source):
            raise FileOperationError(
                f"Workflow 4 refuses symlinks or reparse points: {source}"
            )
        if source.parent == files.registered_dir:
            return "registered"
        try:
            relative = source.relative_to(files.topics_dir)
        except ValueError as exc:
            raise FileOperationError(
                f"Workflow 4 only accepts Registered or Topics documents: {source}"
            ) from exc
        if len(relative.parts) < 2:
            raise FileOperationError(
                f"Workflow 4 requires a topic below Topics/: {source}"
            )
        for parent in source.parents:
            if parent == files.topics_dir:
                break
            if (
                parent.name.startswith(".")
                or parent.is_symlink()
                or files._is_reparse_point(parent)
            ):
                raise FileOperationError(
                    f"Workflow 4 source directory is managed or unsafe: {parent}"
                )
        return "topic"

    def _apply_document_move(
        self, files: FileService, operation: FileOperation
    ) -> None:
        source = operation.source
        assert source is not None
        self._document_source_kind(files, source)
        files.require_within_root(operation.target)
        relative_topic = files.policy.relative_topic_for_path(operation.target.parent)
        if not relative_topic:
            raise FileOperationError(
                f"Workflow 4 target is outside Topics/: {operation.target}"
            )
        files.validate_topic_folder(relative_topic)
        if not is_supported_document_extension(operation.target.suffix):
            raise FileOperationError(
                f"Workflow 4 target extension is unsupported: {operation.target.suffix}"
            )
        files._apply_no_replace(operation, "document filing")

    @staticmethod
    def _update_document(
        catalogue: CatalogueService, document: Any, updates: dict[str, Any]
    ) -> None:
        proposed = {
            key: value
            for key, value in updates.items()
            if key in catalogue.document_headers and document.get(key, None) != value
        }
        if proposed:
            catalogue.update_document_fields(document, proposed)

    @staticmethod
    def _set_document_issue(
        catalogue: CatalogueService, document: Any, issue: str
    ) -> None:
        lines = [
            line.strip()
            for line in str(document.get("uncertainty") or "").splitlines()
            if line.strip()
        ]
        if issue not in lines:
            lines.append(issue)
            catalogue.update_document_fields(
                document, {"uncertainty": "\n".join(lines)}
            )

    @staticmethod
    def _clear_document_filing_issues(
        catalogue: CatalogueService, document: Any
    ) -> None:
        filing_issues = {
            "document_file_missing",
            "document_target_collision",
            "document_unsafe_source",
            "document_unsafe_target",
            "document_group_move_failed",
        }
        current = str(document.get("uncertainty") or "")
        retained = [
            line.strip()
            for line in current.splitlines()
            if line.strip() and line.strip() not in filing_issues
        ]
        updated = "\n".join(retained)
        if updated != current:
            catalogue.update_document_fields(document, {"uncertainty": updated})

    def _create_document_journal(
        self,
        groups: dict[str, list[tuple[Any, FileOperation, str]]],
    ) -> OperationJournal | None:
        payload: list[dict[str, Any]] = []
        for paper_uuid, group in groups.items():
            for document, operation, source_kind in group:
                payload.append(
                    {
                        **operation.to_dict(),
                        "paper_uuid": paper_uuid,
                        "document_id": str(document.get("document_id") or ""),
                        "source_kind": source_kind,
                        "planned_updates": {
                            "filename": operation.target.name,
                            "relative_path": operation.target.relative_to(
                                self.settings.library_root
                            ).as_posix(),
                            "file_status": PdfStatus.FILED.value,
                        },
                        "execution_state": "planned",
                    }
                )
        if not payload:
            return None
        return OperationJournal.create(
            self.settings.state_dir,
            payload,
            workflow="catalogue_filing",
            suffix="filing",
        )
