from __future__ import annotations

import re
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import (
    CatalogueRecord,
    FileOperation,
    MatchStatus,
    MetadataLookupRequest,
    MetadataLookupStatus,
    PdfStatus,
    WorkflowResult,
)
from ..providers.base import MetadataLookupService
from ..providers.unavailable import UnavailableMetadataService
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal, incomplete_journals
from ..services.matching_service import MatchingService
from ..services.pdf_service import PdfService
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from ..utils.filename import standard_pdf_filename
from ..utils.normalize import normalized_relative_path
from .daily_check import DailyCheckWorkflow


class InboxRegisterWorkflow:
    def __init__(
        self,
        settings: Settings,
        metadata_service: MetadataLookupService | None = None,
    ):
        self.settings = settings
        self.metadata_service = metadata_service or UnavailableMetadataService()

    def run(
        self,
        *,
        dry_run: bool = False,
        max_files: int | None = None,
        filename_only: bool = False,
        skip_pdf_text: bool = False,
    ) -> WorkflowResult:
        result = WorkflowResult(
            "inbox_register", dry_run=dry_run, mode="dry_run" if dry_run else "apply"
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous_catalogue = (
            snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        )
        catalogue.configure_review_state(previous_catalogue)

        for journal in incomplete_journals(self.settings.state_dir):
            result.needs_review.append(
                {**journal, "issue": "catalogue_write_incomplete"}
            )

        files = FileService(self.settings.library_root, self.settings.max_filename_length)
        matcher = MatchingService()
        pdfs = PdfService(self.settings)
        discovered, skipped = self._discover_inbox()
        result.skipped.extend(skipped)
        if max_files is not None:
            discovered = discovered[:max_files]

        planned: list[dict[str, Any]] = []
        file_results: list[dict[str, Any]] = []
        metadata_requests = 0
        for source in discovered:
            relative = source.relative_to(self.settings.library_root).as_posix()
            file_result: dict[str, Any] = {
                "source_path": relative,
                "inspection_status": "not_started",
                "match_status": "not_found",
                "matched_catalogue_id": None,
                "match_method": "none",
                "target_filename": None,
                "target_path": None,
                "action": "blocked",
                "issue_keys": [],
            }
            file_results.append(file_result)
            initial_match = matcher.match(
                records,
                relative_path=relative,
                filename=source.name,
            )
            try:
                inspection = pdfs.inspect(
                    source,
                    extract_text=not (filename_only or skip_pdf_text),
                )
            except (OSError, ValueError) as exc:
                self._block(
                    catalogue,
                    self._record_for_match(records, initial_match.matched_row_id),
                    result,
                    file_result,
                    "source_changed_during_run",
                    f"Inbox PDF became unavailable before inspection: {exc}",
                    file=relative,
                )
                continue
            file_result["inspection_status"] = (
                "readable" if inspection.is_readable else "unreadable"
            )
            file_result["inspection"] = inspection.report_summary()

            known_record = self._record_for_match(records, initial_match.matched_row_id)
            try:
                current_stat = source.stat()
            except OSError:
                self._block(
                    catalogue,
                    known_record,
                    result,
                    file_result,
                    "source_changed_during_run",
                    "Inbox PDF disappeared while it was being inspected.",
                    file=relative,
                )
                continue
            if (
                current_stat.st_size != inspection.size
                or current_stat.st_mtime_ns != inspection.mtime_ns
            ):
                self._block(
                    catalogue,
                    known_record,
                    result,
                    file_result,
                    "source_changed_during_run",
                    "Inbox PDF changed while it was being inspected.",
                    file=relative,
                )
                continue
            if not inspection.is_readable:
                issue_key = (
                    "pdf_encrypted"
                    if "pdf_encrypted" in inspection.errors
                    else "pdf_unreadable"
                )
                self._block(
                    catalogue,
                    known_record,
                    result,
                    file_result,
                    issue_key,
                    "PDF cannot be read safely.",
                    file=relative,
                )
                continue
            if (
                not filename_only
                and not skip_pdf_text
                and "text_unavailable" in inspection.warnings
            ):
                self._block(
                    catalogue,
                    known_record,
                    result,
                    file_result,
                    "pdf_text_unavailable",
                    "PDF has no extractable text layer.",
                    file=relative,
                )
                continue

            match = initial_match if filename_only else matcher.match(
                records,
                relative_path=relative,
                filename=source.name,
                inspection=inspection,
            )
            if match.status in {MatchStatus.AMBIGUOUS, MatchStatus.CONFLICT}:
                confirmed_id = self._confirmed_identity(records, match.candidate_rows)
                if confirmed_id:
                    match = matcher.match(
                        records,
                        relative_path=relative,
                        filename=source.name,
                        inspection=inspection,
                        confirmed_catalogue_id=confirmed_id,
                    )

            file_result["match_status"] = match.status.value
            file_result["matched_catalogue_id"] = match.matched_catalogue_id
            file_result["match_method"] = match.method
            record = self._record_for_match(records, match.matched_row_id)
            if match.status not in {MatchStatus.EXACT, MatchStatus.HIGH_CONFIDENCE}:
                issue_key = match.issue_key or "paper_identity_not_found"
                if match.requires_metadata_lookup:
                    metadata_requests += 1
                    lookup = self.metadata_service.lookup(
                        self._lookup_request(inspection, relative)
                    )
                    file_result["metadata_lookup_status"] = lookup.status.value
                    if lookup.status == MetadataLookupStatus.UNAVAILABLE:
                        issue_key = "metadata_lookup_unavailable"
                self._block(
                    catalogue,
                    record,
                    result,
                    file_result,
                    issue_key,
                    "; ".join(match.conflicts) or "Paper identity requires metadata lookup.",
                    file=relative,
                )
                continue

            assert record is not None
            if inspection.is_probable_supplement:
                self._block(
                    catalogue,
                    record,
                    result,
                    file_result,
                    "supplement_parent_unknown",
                    "Phase 2 does not store supplementary files in the single main-PDF fields.",
                    file=relative,
                )
                continue
            current_path = normalized_relative_path(record.get("pdf_relative_path"))
            if current_path and current_path != normalized_relative_path(relative):
                issue_key = (
                    "multiple_local_files_for_single_row"
                    if not inspection.is_probable_supplement
                    else "supplement_parent_unknown"
                )
                self._block(
                    catalogue,
                    record,
                    result,
                    file_result,
                    issue_key,
                    "Catalogue row already points to another local PDF.",
                    file=relative,
                )
                continue

            confirmed_title = self._confirmed_value(record, "title")
            confirmed_year = self._confirmed_value(record, "publication_year")
            confirmed_journal = self._confirmed_value(record, "journal")
            naming_title = record.get("title") or confirmed_title
            naming_year = record.get("year") or confirmed_year
            naming_journal = record.get("journal") or confirmed_journal
            target_filename = standard_pdf_filename(
                title=naming_title,
                year=naming_year,
                journal_abbrev=record.get("journal_abbrev"),
                journal=naming_journal,
                publication_type=record.get("publication_type"),
                max_length=self.settings.max_filename_length,
            )
            if not target_filename:
                missing_field = (
                    "title"
                    if not naming_title
                    else "publication_year"
                    if not naming_year
                    else "journal"
                )
                self._block(
                    catalogue,
                    record,
                    result,
                    file_result,
                    "required_naming_metadata_missing",
                    "Registration requires title, year, and journal or journal_abbrev.",
                    file=relative,
                    field_name=missing_field,
                )
                continue
            try:
                operation = files.plan_registration_move(
                    source,
                    target_filename,
                    record.row_number,
                    "high-confidence local catalogue match",
                )
            except FileOperationError as exc:
                self._block(
                    catalogue,
                    record,
                    result,
                    file_result,
                    "source_changed_during_run",
                    str(exc),
                    file=relative,
                )
                continue
            updates = {
                "pdf_status": PdfStatus.REGISTERED.value,
                "pdf_filename": target_filename,
                "pdf_relative_path": operation.target.relative_to(
                    self.settings.library_root
                ).as_posix(),
            }
            if not record.get("title") and confirmed_title:
                updates["title"] = confirmed_title
            if not record.get("year") and confirmed_year:
                updates["year"] = confirmed_year
            if not record.get("journal") and confirmed_journal:
                updates["journal"] = confirmed_journal
            file_result.update(
                {
                    "target_filename": target_filename,
                    "target_path": updates["pdf_relative_path"],
                    "action": "planned",
                }
            )
            planned.append(
                {
                    "source": source,
                    "record": record,
                    "operation": operation,
                    "updates": updates,
                    "file_result": file_result,
                }
            )

        problems = files.validate_plan([item["operation"] for item in planned])
        blocked_rows = {int(problem["row"]) for problem in problems}
        for problem in problems:
            item = next(
                entry for entry in planned if entry["record"].row_number == int(problem["row"])
            )
            self._block(
                catalogue,
                item["record"],
                result,
                item["file_result"],
                "registered_filename_collision",
                f"Registration collision: {problem['issue']} at {problem['target']}",
                file=item["file_result"]["source_path"],
            )
        ready = [
            item for item in planned if item["record"].row_number not in blocked_rows
        ]

        result.counts = {
            "files_discovered": len(discovered),
            "ready": len(ready),
            "blocked": len(result.needs_review),
            "metadata_lookup_requests": metadata_requests,
        }
        result.details.update(
            {
                "files": file_results,
                "metadata_lookup_requests": metadata_requests,
                "manual_checkpoint_required": False,
            }
        )
        if dry_run:
            result.completed.extend(
                {
                    "action": "would_register",
                    "row": item["record"].row_number,
                    "source": item["file_result"]["source_path"],
                    "target": item["file_result"]["target_path"],
                    "planned_updates": item["updates"],
                }
                for item in ready
            )
            result.changed_rows = len({change.row_number for change in catalogue.changes})
            result.details["remaining_in_inbox"] = [
                path.relative_to(self.settings.library_root).as_posix()
                for path in self._eligible_pdf_files()
            ]
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = self._create_journal(ready) if ready else None
        moved: list[dict[str, Any]] = []
        today = date.today().isoformat()
        for item in ready:
            operation: FileOperation = item["operation"]
            record: CatalogueRecord = item["record"]
            try:
                files.apply_registration_move(operation)
                moved.append(item)
                updates = {
                    key: value
                    for key, value in item["updates"].items()
                    if key in catalogue.headers
                }
                if "date_updated" in catalogue.headers:
                    updates["date_updated"] = today
                catalogue.update_fields(record, updates)
                item["file_result"]["action"] = "registered"
                result.completed.append(
                    {
                        "action": "registered",
                        "row": record.row_number,
                        "source": item["file_result"]["source_path"],
                        "target": item["file_result"]["target_path"],
                    }
                )
                if journal:
                    journal.set_operation_state(record.row_number, "file_moved")
            except FileOperationError as exc:
                issue_key = (
                    "target_appeared_during_run"
                    if operation.target.exists()
                    else "source_changed_during_run"
                )
                self._block(
                    catalogue,
                    record,
                    result,
                    item["file_result"],
                    issue_key,
                    str(exc),
                    file=item["file_result"]["source_path"],
                )
                if journal:
                    journal.set_operation_state(
                        record.row_number, "failed", error=str(exc)
                    )

        backup = catalogue.save_atomic()
        if backup:
            result.catalogue_backup = str(backup)
        if journal:
            for item in moved:
                journal.set_operation_state(
                    item["record"].row_number, "catalogue_committed"
                )
        self._commit_file_blockers(file_results)

        result.changed_files = len(moved)
        result.changed_rows = len({change.row_number for change in catalogue.changes})
        if moved or catalogue.changes:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 3",
                action="Inbox identification and registration",
                files_changed=len(moved),
                catalogue_rows_changed=result.changed_rows,
                reason="Registered high-confidence Inbox PDFs",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        final_check = DailyCheckWorkflow(self.settings).run(
            dry_run=False, final_check=True
        )
        result.details["final_check"] = {
            "status": final_check.status.value,
            "report": final_check.report_path,
        }
        result.state_committed = final_check.state_committed
        for review in final_check.needs_review:
            if review not in result.needs_review:
                result.needs_review.append(review)
        for failure in final_check.failures:
            if failure not in result.failures:
                result.failures.append(failure)
        if journal:
            journal.finish("final_check_committed")

        result.details["manual_checkpoint_required"] = bool(moved)
        result.details["manual_checkpoint"] = (
            "Please review catalogue.xlsx before running Workflow 4."
            if moved
            else None
        )
        result.details["remaining_in_inbox"] = [
            path.relative_to(self.settings.library_root).as_posix()
            for path in self._eligible_pdf_files()
        ]
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _discover_inbox(self) -> tuple[list[Path], list[dict[str, Any]]]:
        eligible: list[Path] = []
        skipped: list[dict[str, Any]] = []
        if not self.settings.inbox_dir.is_dir():
            return eligible, skipped
        for path in sorted(self.settings.inbox_dir.iterdir(), key=lambda item: item.name.casefold()):
            relative = path.relative_to(self.settings.library_root).as_posix()
            if path.is_dir():
                skipped.append({"file": relative, "reason": "inbox_subdirectory"})
                continue
            if path.name.startswith((".", "~")):
                skipped.append({"file": relative, "reason": "hidden_or_temporary"})
                continue
            if path.is_symlink() or self._is_reparse_point(path):
                skipped.append({"file": relative, "reason": "symlink_or_reparse_point"})
                continue
            if path.suffix.casefold() != ".pdf":
                skipped.append({"file": relative, "reason": "non_pdf"})
                continue
            eligible.append(path)
        return eligible, skipped

    def _eligible_pdf_files(self) -> list[Path]:
        return self._discover_inbox()[0]

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)

    @staticmethod
    def _record_for_match(
        records: list[CatalogueRecord], row_number: int | None
    ) -> CatalogueRecord | None:
        if row_number is None:
            return None
        return next((row for row in records if row.row_number == row_number), None)

    @staticmethod
    def _confirmed_identity(
        records: list[CatalogueRecord], candidate_rows: list[int]
    ) -> str | None:
        confirmed: list[str] = []
        for record in records:
            if record.row_number not in candidate_rows:
                continue
            match = re.search(
                r"^USER_CONFIRMED:\s*field=paper_identity;\s*value=([^;\r\n]*)",
                str(record.get("uncertainty") or ""),
                re.I | re.M,
            )
            if match and match.group(1).strip():
                confirmed.append(match.group(1).strip())
        return confirmed[0] if len(set(confirmed)) == 1 else None

    @staticmethod
    def _lookup_request(inspection, relative: str) -> MetadataLookupRequest:
        return MetadataLookupRequest(
            doi=inspection.doi_candidates[0].value if inspection.doi_candidates else None,
            pmid=inspection.pmid_candidates[0].value if inspection.pmid_candidates else None,
            title=inspection.title_candidates[0].value if inspection.title_candidates else None,
            authors=inspection.metadata_author or None,
            year=inspection.year_candidates[0] if inspection.year_candidates else None,
            journal=inspection.journal_candidates[0] if inspection.journal_candidates else None,
            source_pdf=relative,
        )

    @staticmethod
    def _block(
        catalogue: CatalogueService,
        record: CatalogueRecord | None,
        result: WorkflowResult,
        file_result: dict[str, Any],
        issue_key: str,
        issue: str,
        *,
        file: str,
        field_name: str | None = None,
    ) -> None:
        item = {"file": file, "issue": issue_key, "details": issue}
        file_result["action"] = "blocked"
        if issue_key not in file_result["issue_keys"]:
            file_result["issue_keys"].append(issue_key)
        if record is None:
            if item not in result.needs_review:
                result.needs_review.append(item)
            return
        outcome = catalogue.ensure_review_blocker(
            record,
            field_name
            or ("paper_identity" if "identity" in issue_key else "pdf_file"),
            issue,
            issue_key=issue_key,
            conflict_with_confirmation=issue_key in {"identifier_conflict"},
        )
        item["row"] = record.row_number
        if outcome in {"added", "existing"}:
            if item not in result.needs_review:
                result.needs_review.append(item)
        else:
            result.skipped.append({**item, "reason": f"review_{outcome}"})

    def _create_journal(self, ready: list[dict[str, Any]]) -> OperationJournal:
        operations = []
        for item in ready:
            operation = item["operation"]
            operations.append(
                {
                    **operation.to_dict(),
                    "source_fingerprint": {
                        "size": operation.expected_size,
                        "mtime_ns": operation.expected_mtime_ns,
                    },
                    "planned_updates": item["updates"],
                    "execution_state": "planned",
                }
            )
        return OperationJournal.create(self.settings.state_dir, operations)

    @staticmethod
    def _confirmed_value(record: CatalogueRecord, field_name: str) -> str:
        match = re.search(
            rf"^USER_CONFIRMED:\s*field={re.escape(field_name)};\s*value=([^;\r\n]*)",
            str(record.get("uncertainty") or ""),
            re.I | re.M,
        )
        return match.group(1).strip() if match else ""

    def _commit_file_blockers(self, file_results: list[dict[str, Any]]) -> None:
        path = self.settings.state_dir / "inbox_blockers.json"
        blockers = []
        for item in file_results:
            if item.get("action") != "blocked" or not item.get("issue_keys"):
                continue
            inspection = item.get("inspection") or {}
            blockers.append(
                {
                    "stable_file_id": "|".join(
                        (
                            str(item.get("source_path") or ""),
                            str(inspection.get("size") or ""),
                            str(inspection.get("mtime_ns") or ""),
                        )
                    ),
                    "source_path": item.get("source_path"),
                    "size": inspection.get("size"),
                    "mtime_ns": inspection.get("mtime_ns"),
                    "issue_keys": sorted(set(item.get("issue_keys") or [])),
                }
            )
        payload = {"version": 1, "files": sorted(blockers, key=lambda row: row["stable_file_id"])}
        if path.is_file():
            try:
                if json.loads(path.read_text(encoding="utf-8")) == payload:
                    return
            except Exception:
                pass
        elif not blockers:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise FileOperationError(f"Cannot commit Inbox blocker state: {path}") from exc
