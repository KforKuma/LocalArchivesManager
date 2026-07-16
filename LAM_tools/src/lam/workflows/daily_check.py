from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..directory_policy import DirectoryPolicy, RootDirectoryKind
from ..exceptions import FileOperationError
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
        snapshots = SnapshotService(
            self.settings.library_root,
            self.settings.state_dir,
            self.settings.reserved_root_directories,
        )
        initial = not snapshots.initialized
        result.mode = "final_check" if final_check else ("initial" if initial else "incremental")

        previous_manifest = snapshots.load_manifest() if not initial else {}
        previous_catalogue = snapshots.load_catalogue_snapshot() if not initial else {}
        catalogue.configure_review_state(previous_catalogue)
        before_catalogue = catalogue.snapshot_payload()
        current_manifest = snapshots.scan(previous_manifest)
        file_diffs, unchanged_count = (
            ([], 0) if initial else snapshots.compare(previous_manifest, current_manifest)
        )
        catalogue_diffs = (
            [] if initial else snapshots.compare_catalogue(previous_catalogue, before_catalogue)
        )

        referenced_legacy_roots = self._referenced_legacy_roots(records)
        root_items = snapshots.root_items(referenced_legacy_roots)
        matched_paths = (
            self._reconcile_documents(catalogue, current_manifest, result)
            if catalogue.has_documents_sheet
            else self._reconcile(catalogue, current_manifest, result)
        )
        for key, item in current_manifest.items():
            if key not in matched_paths:
                result.needs_review.append(
                    {
                        "file": item.relative_path,
                        "issue": (
                            "unmatched_local_document"
                            if catalogue.has_documents_sheet
                            else "unmatched_local_pdf"
                        ),
                    }
                )

        changed_rows = {
            ("Catalogue", change.row_number) for change in catalogue.changes
        } | {
            ("Documents", change.row_number) for change in catalogue.document_changes
        }
        result.changed_rows = len(changed_rows)
        result.counts = {
            "catalogue_rows": len(records),
            "managed_pdfs": len(current_manifest),
            "managed_documents": len(current_manifest),
            "unchanged_files": unchanged_count,
            "file_diffs": len(file_diffs),
            "catalogue_diffs": len(catalogue_diffs),
        }
        result.details = {
            "file_diffs": [self._diff_to_dict(item) for item in file_diffs],
            "catalogue_diffs": catalogue_diffs,
            "metadata_candidates": self._metadata_candidates(records),
            "unmanaged_items": root_items,
            "final_check": final_check,
        }

        backup = None
        state_was_new_or_changed = initial or bool(file_diffs) or bool(catalogue_diffs) or bool(catalogue.changes)
        if not dry_run:
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            if catalogue.maintenance_actions:
                result.details["backup_maintenance"] = list(
                    catalogue.maintenance_actions
                )
            after_catalogue = catalogue.snapshot_payload()
            diff_payload = {
                "mode": result.mode,
                "changes_detected": bool(file_diffs or catalogue_diffs or catalogue.changes),
                "file_diffs": result.details["file_diffs"],
                "catalogue_diffs": catalogue_diffs,
                "catalogue_updates": [asdict(change) for change in catalogue.changes],
                "document_updates": [
                    asdict(change) for change in catalogue.document_changes
                ],
                "unchanged_count": unchanged_count,
            }
            if state_was_new_or_changed or not snapshots.last_diff_path.is_file():
                snapshots.commit(after_catalogue, current_manifest, diff_payload)
                result.state_committed = True
            if initial or catalogue.changes or catalogue.document_changes:
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
        result.completed.extend(
            {
                "sheet": "Documents",
                "row": change.row_number,
                "field": change.field_name,
                "old": change.old_value,
                "new": change.new_value,
                "action": "would_update" if dry_run else "updated",
            }
            for change in catalogue.document_changes
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
            filename_key = normalized_text(record.get("pdf_filename"))
            filename_candidates = by_filename.get(filename_key, []) if filename_key else []
            if len(filename_candidates) == 1:
                candidates = filename_candidates
            elif len(filename_candidates) > 1:
                path_matches = [item for item in filename_candidates if item[0] == path_key]
                candidates = path_matches if len(path_matches) == 1 else filename_candidates
            elif path_key and path_key in manifest:
                candidates = [(path_key, manifest[path_key])]

            if len(candidates) > 1:
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "pdf_file",
                    "Multiple local PDFs share the catalogue filename.",
                    issue_key="multiple_filename_matches",
                )
                self._objective_update(
                    catalogue,
                    record,
                    {"pdf_status": PdfStatus.UNCLEAR.value},
                    today,
                )
                self._record_review(
                    result,
                    outcome,
                    {"row": record.row_number, "issue": "multiple_filename_matches"},
                )
                continue

            if not candidates:
                if self._record_legacy_location(catalogue, record, result, today):
                    continue
                prior_status = normalized_text(record.get("pdf_status"))
                expects_file = bool(path_key or record.get("pdf_filename")) or prior_status in {
                    PdfStatus.INBOX.value,
                    PdfStatus.REGISTERED.value,
                    PdfStatus.FILED.value,
                    PdfStatus.MISSING.value,
                    PdfStatus.UNCLEAR.value,
                }
                if expects_file:
                    outcome = catalogue.ensure_review_blocker(
                        record,
                        "pdf_file",
                        "Catalogue expects a PDF but no local file was found.",
                        issue_key="expected_pdf_missing",
                    )
                    self._objective_update(
                        catalogue,
                        record,
                        {"pdf_status": PdfStatus.MISSING.value},
                        today,
                    )
                    self._record_review(
                        result,
                        outcome,
                        {"row": record.row_number, "issue": "expected_pdf_missing"},
                    )
                continue

            key, item = candidates[0]
            matched.add(key)
            status, mismatch = self._status_for(
                item.relative_path, record.get("topic_folder")
            )
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
                issue = f"Observed location {item.relative_path!r} differs from topic_folder."
                outcome = catalogue.ensure_review_blocker(
                    record,
                    "pdf_relative_path",
                    issue,
                    issue_key="topic_location_mismatch",
                )
                self._record_review(
                    result,
                    outcome,
                    {
                        "row": record.row_number,
                        "file": item.relative_path,
                        "issue": "topic_location_mismatch",
                    },
                )
        return matched

    def _reconcile_documents(
        self,
        catalogue: CatalogueService,
        manifest: dict[str, FileSnapshot],
        result: WorkflowResult,
    ) -> set[str]:
        by_filename: dict[str, list[tuple[str, FileSnapshot]]] = defaultdict(list)
        for key, item in manifest.items():
            by_filename[normalized_text(item.filename)].append((key, item))
        papers = {
            normalized_text(record.get("paper_uuid")): record
            for record in catalogue.records
            if normalized_text(record.get("paper_uuid"))
        }
        matched: set[str] = set()
        today = date.today().isoformat()
        for document in catalogue.documents:
            path_key = normalized_relative_path(document.get("relative_path"))
            candidates: list[tuple[str, FileSnapshot]] = []
            if path_key and path_key in manifest:
                candidates = [(path_key, manifest[path_key])]
            else:
                filename_key = normalized_text(document.get("filename"))
                if filename_key:
                    candidates = by_filename.get(filename_key, [])
            if len(candidates) > 1:
                self._set_document_issue(
                    catalogue, document, "multiple_filename_matches"
                )
                self._update_document(
                    catalogue,
                    document,
                    {"file_status": PdfStatus.UNCLEAR.value},
                    today,
                )
                result.needs_review.append(
                    {
                        "sheet": "Documents",
                        "row": document.row_number,
                        "document_id": document.get("document_id"),
                        "issue": "multiple_filename_matches",
                    }
                )
                continue
            if not candidates:
                expects_file = bool(
                    document.get("relative_path") or document.get("filename")
                )
                if expects_file:
                    self._set_document_issue(
                        catalogue, document, "document_file_missing"
                    )
                    self._update_document(
                        catalogue,
                        document,
                        {"file_status": PdfStatus.MISSING.value},
                        today,
                    )
                    result.needs_review.append(
                        {
                            "sheet": "Documents",
                            "row": document.row_number,
                            "document_id": document.get("document_id"),
                            "issue": "document_file_missing",
                        }
                    )
                continue
            key, item = candidates[0]
            matched.add(key)
            paper = papers.get(normalized_text(document.get("paper_uuid")))
            topic = paper.get("topic_folder") if paper else ""
            status, mismatch = self._status_for(item.relative_path, topic)
            self._update_document(
                catalogue,
                document,
                {
                    "filename": item.filename,
                    "relative_path": item.relative_path,
                    "extension": Path(item.filename).suffix,
                    "file_status": status.value,
                },
                today,
            )
            self._clear_document_issue(
                catalogue, document, "document_file_missing"
            )
            if mismatch:
                self._set_document_issue(
                    catalogue, document, "topic_location_mismatch"
                )
                result.needs_review.append(
                    {
                        "sheet": "Documents",
                        "row": document.row_number,
                        "document_id": document.get("document_id"),
                        "file": item.relative_path,
                        "issue": "topic_location_mismatch",
                    }
                )
            else:
                self._clear_document_issue(
                    catalogue, document, "topic_location_mismatch"
                )
        return matched

    @staticmethod
    def _update_document(
        catalogue: CatalogueService,
        document: Any,
        updates: dict[str, Any],
        today: str,
    ) -> None:
        proposed = {
            key: value
            for key, value in updates.items()
            if key in catalogue.document_headers and document.get(key, None) != value
        }
        if proposed and "date_updated" in catalogue.document_headers:
            proposed["date_updated"] = today
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
    def _clear_document_issue(
        catalogue: CatalogueService, document: Any, issue: str
    ) -> None:
        lines = [
            line.strip()
            for line in str(document.get("uncertainty") or "").splitlines()
            if line.strip() and line.strip() != issue
        ]
        current = str(document.get("uncertainty") or "")
        updated = "\n".join(lines)
        if current != updated:
            catalogue.update_document_fields(document, {"uncertainty": updated})

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

    def _status_for(self, relative_path: str, topic_folder: object) -> tuple[PdfStatus, bool]:
        parts = relative_path.replace("\\", "/").split("/")
        top = parts[0].casefold() if parts else ""
        topic = str(topic_folder or "").strip()
        if top == "inbox":
            return PdfStatus.INBOX, False
        if top == "registered":
            return PdfStatus.REGISTERED, False
        if top == "topics" and len(parts) >= 3:
            observed_topic = "/".join(parts[1:-1])
            try:
                expected_topic = DirectoryPolicy(
                    self.settings.library_root,
                    self.settings.reserved_root_directories,
                ).validate_topic_folder(topic)
            except FileOperationError:
                return PdfStatus.UNCLEAR, True
            if normalized_text(observed_topic) == normalized_text(expected_topic):
                return PdfStatus.FILED, False
            return PdfStatus.UNCLEAR, True
        if top not in {"inbox", "registered", "topics"}:
            return PdfStatus.UNCLEAR, bool(topic)
        return PdfStatus.UNCLEAR, False

    def _record_legacy_location(
        self,
        catalogue: CatalogueService,
        record: Any,
        result: WorkflowResult,
        today: str,
    ) -> bool:
        relative = str(record.get("pdf_relative_path") or "").strip()
        if not relative:
            return False
        path = (self.settings.library_root / relative).resolve()
        try:
            parts = path.relative_to(self.settings.library_root.resolve()).parts
        except ValueError:
            return False
        if len(parts) < 2 or not path.is_file() or path.suffix.casefold() != ".pdf":
            return False
        policy = DirectoryPolicy(
            self.settings.library_root,
            self.settings.reserved_root_directories,
        )
        kind = policy.classify_root_directory(
            parts[0], referenced_legacy_roots={parts[0]}
        )
        if kind != RootDirectoryKind.LEGACY_TOPIC_CANDIDATE:
            return False
        self._objective_update(
            catalogue,
            record,
            {"pdf_status": PdfStatus.UNCLEAR.value},
            today,
        )
        outcome = catalogue.ensure_review_blocker(
            record,
            "pdf_relative_path",
            "Registered PDF remains in a legacy root-level topic directory; run migrate-topics.",
            issue_key="legacy_topic_location",
        )
        self._record_review(
            result,
            outcome,
            {
                "row": record.row_number,
                "file": relative.replace("\\", "/"),
                "issue": "legacy_topic_location",
            },
        )
        return True

    def _referenced_legacy_roots(self, records: list[Any]) -> set[str]:
        policy = DirectoryPolicy(
            self.settings.library_root,
            self.settings.reserved_root_directories,
        )
        roots: set[str] = set()
        for record in records:
            relative = str(record.get("pdf_relative_path") or "").strip().replace("\\", "/")
            if relative:
                first = relative.split("/", 1)[0]
                kind = policy.classify_root_directory(first)
                if kind == RootDirectoryKind.UNKNOWN:
                    roots.add(first)
            topic = str(record.get("topic_folder") or "").strip().replace("\\", "/")
            if topic.casefold().startswith("topics/"):
                topic = topic.split("/", 1)[1]
            if topic:
                first = topic.split("/", 1)[0]
                if policy.classify_root_directory(first) == RootDirectoryKind.UNKNOWN:
                    roots.add(first)
        return roots

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
