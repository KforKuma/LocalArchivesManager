from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date
from typing import Any

from ..config import Settings
from ..models import DiffType, FileSnapshot, PdfStatus, WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from ..utils.normalize import normalized_relative_path, normalized_text


class DailyCheckWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, dry_run: bool = False, final_check: bool = False) -> WorkflowResult:
        result = WorkflowResult("daily_check", dry_run=dry_run)
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        initial = not snapshots.initialized
        result.mode = "final_check" if final_check else ("initial" if initial else "incremental")

        previous_manifest = snapshots.load_manifest() if not initial else {}
        previous_catalogue = snapshots.load_catalogue_snapshot() if not initial else {}
        before_catalogue = catalogue.snapshot_payload()
        current_manifest = snapshots.scan(previous_manifest)
        file_diffs, unchanged_count = (
            ([], 0) if initial else snapshots.compare(previous_manifest, current_manifest)
        )
        catalogue_diffs = (
            [] if initial else snapshots.compare_catalogue(previous_catalogue, before_catalogue)
        )

        matched_paths = self._reconcile(catalogue, current_manifest, result)
        for key, item in current_manifest.items():
            if key not in matched_paths:
                result.needs_review.append(
                    {
                        "file": item.relative_path,
                        "issue": "unmatched_local_pdf",
                    }
                )

        changed_rows = {change.row_number for change in catalogue.changes}
        result.changed_rows = len(changed_rows)
        result.counts = {
            "catalogue_rows": len(records),
            "managed_pdfs": len(current_manifest),
            "unchanged_files": unchanged_count,
            "file_diffs": len(file_diffs),
            "catalogue_diffs": len(catalogue_diffs),
        }
        result.details = {
            "file_diffs": [self._diff_to_dict(item) for item in file_diffs],
            "catalogue_diffs": catalogue_diffs,
            "metadata_candidates": self._metadata_candidates(records),
            "final_check": final_check,
        }

        backup = None
        state_was_new_or_changed = initial or bool(file_diffs) or bool(catalogue_diffs) or bool(catalogue.changes)
        if not dry_run:
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            after_catalogue = catalogue.snapshot_payload()
            diff_payload = {
                "mode": result.mode,
                "changes_detected": bool(file_diffs or catalogue_diffs or catalogue.changes),
                "file_diffs": result.details["file_diffs"],
                "catalogue_diffs": catalogue_diffs,
                "catalogue_updates": [asdict(change) for change in catalogue.changes],
                "unchanged_count": unchanged_count,
            }
            if state_was_new_or_changed or not snapshots.last_diff_path.is_file():
                snapshots.commit(after_catalogue, current_manifest, diff_payload)
                result.state_committed = True
            if initial or catalogue.changes:
                append_change_log(
                    self.settings.changes_log_path,
                    workflow="Workflow 1",
                    action="Initial baseline" if initial else "Catalogue reconciliation",
                    files_changed=0,
                    catalogue_rows_changed=result.changed_rows,
                    reason="Observed filesystem state reconciled with catalogue",
                    uncertainty=f"{len(result.needs_review)} item(s) need review",
                )

        result.completed.extend(
            {
                "row": change.row_number,
                "field": change.field_name,
                "old": change.old_value,
                "new": change.new_value,
                "action": "would_update" if dry_run else "updated",
            }
            for change in catalogue.changes
        )
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _reconcile(
        self,
        catalogue: CatalogueService,
        manifest: dict[str, FileSnapshot],
        result: WorkflowResult,
    ) -> set[str]:
        by_filename: dict[str, list[tuple[str, FileSnapshot]]] = defaultdict(list)
        for key, item in manifest.items():
            by_filename[normalized_text(item.filename)].append((key, item))
        matched: set[str] = set()
        today = date.today().isoformat()

        for record in catalogue.records:
            path_key = normalized_relative_path(record.get("pdf_relative_path"))
            candidates: list[tuple[str, FileSnapshot]] = []
            if path_key and path_key in manifest:
                candidates = [(path_key, manifest[path_key])]
            else:
                filename_key = normalized_text(record.get("pdf_filename"))
                if filename_key:
                    candidates = by_filename.get(filename_key, [])

            if len(candidates) > 1:
                catalogue.add_uncertainty(
                    record,
                    "NEEDS_REVIEW:",
                    "pdf_file",
                    "Multiple local PDFs share the catalogue filename.",
                )
                self._objective_update(
                    catalogue,
                    record,
                    {"pdf_status": PdfStatus.UNCLEAR.value},
                    today,
                )
                result.needs_review.append(
                    {"row": record.row_number, "issue": "multiple_filename_matches"}
                )
                continue

            if not candidates:
                prior_status = normalized_text(record.get("pdf_status"))
                expects_file = bool(path_key or record.get("pdf_filename")) or prior_status in {
                    PdfStatus.INBOX.value,
                    PdfStatus.REGISTERED.value,
                    PdfStatus.FILED.value,
                    PdfStatus.MISSING.value,
                    PdfStatus.UNCLEAR.value,
                }
                if expects_file:
                    catalogue.add_uncertainty(
                        record,
                        "NEEDS_REVIEW:",
                        "pdf_file",
                        "Catalogue expects a PDF but no local file was found.",
                    )
                    self._objective_update(
                        catalogue,
                        record,
                        {"pdf_status": PdfStatus.MISSING.value},
                        today,
                    )
                    result.needs_review.append(
                        {"row": record.row_number, "issue": "expected_pdf_missing"}
                    )
                continue

            key, item = candidates[0]
            matched.add(key)
            status, mismatch = self._status_for(item.relative_path, record.get("topic_folder"))
            self._objective_update(
                catalogue,
                record,
                {
                    "pdf_status": status.value,
                    "pdf_filename": item.filename,
                    "pdf_relative_path": item.relative_path,
                },
                today,
            )
            if mismatch:
                catalogue.add_uncertainty(
                    record,
                    "NEEDS_REVIEW:",
                    "pdf_relative_path",
                    f"Observed location {item.relative_path!r} differs from topic_folder.",
                )
                result.needs_review.append(
                    {
                        "row": record.row_number,
                        "file": item.relative_path,
                        "issue": "topic_location_mismatch",
                    }
                )
        return matched

    @staticmethod
    def _objective_update(
        catalogue: CatalogueService,
        record: Any,
        updates: dict[str, Any],
        today: str,
    ) -> None:
        proposed = {
            key: value
            for key, value in updates.items()
            if key in catalogue.headers and record.get(key, None) != value
        }
        if proposed and "date_updated" in catalogue.headers:
            proposed["date_updated"] = today
        if proposed:
            catalogue.update_fields(record, proposed)

    @staticmethod
    def _status_for(relative_path: str, topic_folder: object) -> tuple[PdfStatus, bool]:
        parts = relative_path.replace("\\", "/").split("/")
        top = parts[0].casefold() if parts else ""
        topic = str(topic_folder or "").strip()
        if top == "inbox":
            return PdfStatus.INBOX, False
        if top == "registered":
            return PdfStatus.REGISTERED, False
        if topic and topic.casefold() == top:
            return PdfStatus.FILED, False
        if top not in {"inbox", "registered"}:
            return PdfStatus.UNCLEAR, bool(topic and topic.casefold() != top)
        return PdfStatus.UNCLEAR, False

    @staticmethod
    def _metadata_candidates(records: list[Any]) -> list[dict[str, Any]]:
        candidates = []
        for record in records:
            missing = [
                field
                for field in ("title", "authors", "abstract", "doi", "pmid")
                if field in record.values and not record.get(field)
            ]
            if missing:
                candidates.append(
                    {"row": record.row_number, "id": record.get("id"), "missing_fields": missing}
                )
        return candidates

    @staticmethod
    def _diff_to_dict(item: Any) -> dict[str, Any]:
        payload = asdict(item)
        payload["diff_type"] = item.diff_type.value
        return payload

