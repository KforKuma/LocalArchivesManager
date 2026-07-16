from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError
from ..models import WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.journal_service import OperationJournal
from ..services.report_service import ReportService, append_change_log
from ..schema import CATALOGUE_051_FIELDS
from ..utils.hashing import full_hash
from .daily_check import DailyCheckWorkflow


class DocumentMigrationWorkflow:
    """Migrate the legacy one-paper/one-PDF workbook to the 0.5.1 dual model."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, dry_run: bool = False) -> WorkflowResult:
        result = WorkflowResult(
            "document_migration",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(
            self.settings.catalogue_path, allow_legacy_schema=True
        )
        records = catalogue.load()
        if catalogue.has_documents_sheet:
            incomplete = [
                record.row_number
                for record in records
                if not record.get("paper_uuid")
                or not any(
                    document.get("document_type") == "main"
                    for document in catalogue.documents_for_paper(record.get("paper_uuid"))
                )
            ]
            if incomplete:
                raise CatalogueError(
                    "Documents migration is partial; refusing to guess missing relationships "
                    f"for Catalogue rows {incomplete}"
                )
            result.skipped.append({"reason": "already_migrated"})
            result.counts = {
                "catalogue_rows": len(records),
                "documents": len(catalogue.documents),
            }
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        plan = self._plan(records)
        result.details["plan"] = [self._public_plan(item) for item in plan]
        result.counts = {
            "catalogue_rows": len(records),
            "paper_uuid_preserved": sum(item["uuid_source"] == "paper_uuid" for item in plan),
            "record_uid_reused": sum(item["uuid_source"] == "record_uid" for item in plan),
            "paper_uuid_generated": sum(item["uuid_source"] == "generated" for item in plan),
            "main_documents": sum(item["document"] is not None for item in plan),
            "missing_files": sum(
                item["document"] is not None
                and item["document"]["file_status"] == "missing"
                for item in plan
            ),
        }
        if dry_run:
            result.completed.extend(
                {
                    "action": "would_migrate_paper",
                    "row": item["row_number"],
                    "paper_uuid_source": item["uuid_source"],
                    "main_document": bool(item["document"]),
                }
                for item in plan
            )
            result.changed_rows = len(plan)
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = OperationJournal.create(
            self.settings.state_dir,
            [
                {
                    "operation_type": "workbook_migration",
                    "operation_id": f"paper:{item['paper_uuid']}",
                    "catalogue_row": item["row_number"],
                    "paper_uuid": item["paper_uuid"],
                    "document_id": (
                        item["document"]["document_id"] if item["document"] else None
                    ),
                    "execution_state": "planned",
                }
                for item in plan
            ],
            workflow="document_migration",
            suffix="migrate-documents",
        )
        for field_name in CATALOGUE_051_FIELDS:
            catalogue.ensure_header(field_name)
        catalogue.ensure_documents_sheet()
        for item in plan:
            record = next(
                row for row in records if row.row_number == item["row_number"]
            )
            current = str(record.get("paper_uuid") or "").strip()
            if not current:
                column = catalogue.headers["paper_uuid"]
                catalogue.worksheet.cell(row=record.row_number, column=column).value = item[
                    "paper_uuid"
                ]
                record.values["paper_uuid"] = item["paper_uuid"]
                from ..models import CatalogueChange

                catalogue.changes.append(
                    CatalogueChange(
                        record.row_number, "paper_uuid", None, item["paper_uuid"]
                    )
                )
            if item["document"]:
                catalogue.add_document(item["document"])

        backup = catalogue.save_atomic()
        if backup:
            result.catalogue_backup = str(backup)
            journal.payload["catalogue_backup"] = str(backup)
            journal.write()
        for item in plan:
            journal.set_operation_state(
                item["row_number"],
                "catalogue_committed",
                operation_id=f"paper:{item['paper_uuid']}",
                document_id=(
                    item["document"]["document_id"] if item["document"] else None
                ),
            )
        result.changed_rows = len(plan)
        result.details["backup_cleanup"] = list(catalogue.maintenance_actions)
        result.completed.extend(
            {
                "action": "migrated_paper",
                "row": item["row_number"],
                "paper_uuid": item["paper_uuid"],
                "document_id": (
                    item["document"]["document_id"] if item["document"] else None
                ),
            }
            for item in plan
        )
        append_change_log(
            self.settings.changes_log_path,
            workflow="Document migration",
            action="Create Catalogue/Documents workbook model",
            files_changed=0,
            catalogue_rows_changed=len(plan),
            reason="Assign stable paper_uuid values and register legacy main PDFs in Documents",
            uncertainty=(
                f"{result.counts['missing_files']} missing legacy file(s) recorded in Documents"
            ),
        )
        final_check = DailyCheckWorkflow(self.settings).run(
            dry_run=False, final_check=True
        )
        result.details["final_check"] = {
            "status": final_check.status.value,
            "report": final_check.report_path,
        }
        result.state_committed = final_check.state_committed
        result.needs_review.extend(
            item for item in final_check.needs_review if item not in result.needs_review
        )
        result.failures.extend(
            item for item in final_check.failures if item not in result.failures
        )
        journal.finish("final_check_committed")
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _plan(self, records: list[Any]) -> list[dict[str, Any]]:
        today = date.today().isoformat()
        plan: list[dict[str, Any]] = []
        seen_paths: dict[str, int] = {}
        for record in records:
            paper_uuid, source = self._paper_uuid(record)
            relative = str(record.get("pdf_relative_path") or "").strip().replace(
                "\\", "/"
            )
            filename = str(record.get("pdf_filename") or "").strip()
            document = None
            if relative or filename:
                if not relative:
                    raise CatalogueError(
                        f"Catalogue row {record.row_number} has a filename but no legacy path"
                    )
                key = relative.casefold()
                if key in seen_paths:
                    raise CatalogueError(
                        f"Legacy path {relative!r} is shared by rows "
                        f"{seen_paths[key]} and {record.row_number}"
                    )
                seen_paths[key] = record.row_number
                path = (self.settings.library_root / relative).resolve()
                try:
                    path.relative_to(self.settings.library_root.resolve())
                except ValueError as exc:
                    raise CatalogueError(
                        f"Legacy path escapes library root at row {record.row_number}: {relative}"
                    ) from exc
                observed_name = path.name if path.is_file() else filename or Path(relative).name
                file_status = str(record.get("pdf_status") or "missing")
                uncertainty = ""
                digest = ""
                if path.is_file():
                    digest = full_hash(path)
                else:
                    file_status = "missing"
                    uncertainty = "document_file_missing"
                document = {
                    "document_id": f"{paper_uuid}:main",
                    "paper_uuid": paper_uuid,
                    "document_type": "main",
                    "filename": observed_name,
                    "relative_path": relative,
                    "extension": Path(observed_name).suffix,
                    "sha256": digest,
                    "file_status": file_status,
                    "source": str(record.get("source") or ""),
                    "uncertainty": uncertainty,
                    "date_added": str(record.get("date_added") or today),
                    "date_updated": today,
                }
            plan.append(
                {
                    "row_number": record.row_number,
                    "paper_uuid": paper_uuid,
                    "uuid_source": source,
                    "document": document,
                }
            )
        return plan

    @staticmethod
    def _paper_uuid(record: Any) -> tuple[str, str]:
        for field_name in ("paper_uuid", "record_uid"):
            value = str(record.get(field_name) or "").strip()
            if not value:
                continue
            try:
                parsed = uuid.UUID(value)
                if parsed.version == 4:
                    return str(parsed), field_name
                if field_name == "paper_uuid":
                    raise CatalogueError(
                        f"paper_uuid must be UUID4 at Catalogue row "
                        f"{record.row_number}: {value!r}"
                    )
            except ValueError:
                if field_name == "paper_uuid":
                    raise CatalogueError(
                        f"Invalid paper_uuid at Catalogue row {record.row_number}: {value!r}"
                    )
        return str(uuid.uuid4()), "generated"

    @staticmethod
    def _public_plan(item: dict[str, Any]) -> dict[str, Any]:
        document = item["document"]
        return {
            "row": item["row_number"],
            "paper_uuid_source": item["uuid_source"],
            "document_id": document["document_id"] if document else None,
            "relative_path": document["relative_path"] if document else None,
            "file_status": document["file_status"] if document else None,
        }
