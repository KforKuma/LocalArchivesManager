from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from ..models import CatalogueChange, WorkflowResult
from ..schema import CATALOGUE_FIELDS, DOCUMENT_FIELDS
from ..services.catalogue_service import CatalogueService
from ..services.journal_service import OperationJournal
from ..services.report_service import ReportService, append_change_log
from ..utils.normalize import normalized_text
from .daily_check import DailyCheckWorkflow
from .identifier_migration import IdentifierMigrationWorkflow


_UNKNOWN_EXPECTATION_REVIEW = (
    "NEEDS_REVIEW: field=document_expectation; "
    "issue_key=legacy_document_expectation_unknown; "
    "issue=No Documents row or reference-text receipt establishes whether a managed "
    "document is expected."
)


class SchemaMigrationWorkflow:
    """Upgrade a strict 0.6.0 workbook to the 0.6.1 paper semantics."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, dry_run: bool) -> WorkflowResult:
        result = WorkflowResult(
            "schema_migration",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(
            self.settings.catalogue_path,
            allow_legacy_schema=True,
        )
        records = catalogue.load()
        receipt_uuids = self._reference_receipt_uuids()
        document_uuids = {
            normalized_text(document.get("paper_uuid"))
            for document in catalogue.documents
            if normalized_text(document.get("paper_uuid"))
        }
        plan = [
            self._plan_record(record, receipt_uuids, document_uuids)
            for record in records
        ]
        result.details["plan"] = plan
        result.counts = {
            "catalogue_rows": len(plan),
            "record_origin_reference_text": sum(
                item["record_origin"] == "reference_text" for item in plan
            ),
            "record_origin_pdf": sum(item["record_origin"] == "pdf" for item in plan),
            "record_origin_legacy": sum(
                item["record_origin"] == "legacy" for item in plan
            ),
            "document_required": sum(
                item["document_expectation"] == "required" for item in plan
            ),
            "document_optional": sum(
                item["document_expectation"] == "optional" for item in plan
            ),
            "document_unknown": sum(
                item["document_expectation"] == "unknown" for item in plan
            ),
        }
        result.needs_review.extend(
            {
                "row": item["row"],
                "paper_uuid": item["paper_uuid"],
                "issue_key": "legacy_document_expectation_unknown",
            }
            for item in plan
            if item["document_expectation"] == "unknown"
        )

        if dry_run:
            result.completed.extend(
                {
                    "action": "would_set_paper_semantics",
                    **item,
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
                    "operation_type": "catalogue_schema_migration_061",
                    "operation_id": f"paper:{item['paper_uuid']}",
                    "catalogue_row": item["row"],
                    "paper_uuid": item["paper_uuid"],
                    "execution_state": "planned",
                }
                for item in plan
            ],
            workflow="schema_migration",
            suffix="migrate-schema-061",
        )
        self._apply(catalogue, plan)
        backup = catalogue.save_atomic()
        if backup:
            result.catalogue_backup = str(backup)
            journal.payload["catalogue_backup"] = str(backup)
            journal.write()
        for item in plan:
            journal.set_operation_state(
                item["row"],
                "catalogue_committed",
                operation_id=f"paper:{item['paper_uuid']}",
            )
        result.changed_rows = len(plan)
        result.completed.append(
            {
                "action": "migrated_catalogue_schema_061",
                "catalogue_columns": list(CATALOGUE_FIELDS),
                "document_columns": list(DOCUMENT_FIELDS),
            }
        )
        append_change_log(
            self.settings.changes_log_path,
            workflow="Schema migration",
            action="Add record_origin and document_expectation",
            files_changed=0,
            catalogue_rows_changed=len(plan),
            reason="Adopt LAM 0.6.1 paper provenance and document expectation semantics",
            uncertainty=(
                f"{result.counts['document_unknown']} legacy rows require review"
                if result.counts["document_unknown"]
                else "None"
            ),
        )
        final_check = DailyCheckWorkflow(self.settings).run(
            dry_run=False,
            final_check=True,
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

    @staticmethod
    def _plan_record(
        record: Any,
        receipt_uuids: set[str],
        document_uuids: set[str],
    ) -> dict[str, Any]:
        paper_uuid = str(record.get("paper_uuid") or "").strip()
        key = normalized_text(paper_uuid)
        from_reference = key in receipt_uuids
        has_documents = key in document_uuids
        return {
            "row": record.row_number,
            "paper_uuid": paper_uuid,
            "record_origin": (
                "reference_text" if from_reference else "pdf" if has_documents else "legacy"
            ),
            "document_expectation": (
                "required" if has_documents else "optional" if from_reference else "unknown"
            ),
        }

    def _apply(
        self,
        catalogue: CatalogueService,
        plan: list[dict[str, Any]],
    ) -> None:
        for field_name in CATALOGUE_FIELDS:
            catalogue.ensure_header(field_name)
        by_row = {record.row_number: record for record in catalogue.records}
        for item in plan:
            record = by_row[item["row"]]
            for field_name in ("record_origin", "document_expectation"):
                old = record.get(field_name)
                new = item[field_name]
                catalogue.worksheet.cell(
                    row=record.row_number,
                    column=catalogue.headers[field_name],
                ).value = new
                record.values[field_name] = new
                if old != new:
                    catalogue.changes.append(
                        CatalogueChange(record.row_number, field_name, old, new)
                    )
            if item["document_expectation"] == "unknown":
                old_uncertainty = str(record.get("uncertainty") or "").strip()
                if "issue_key=legacy_document_expectation_unknown" not in old_uncertainty:
                    new_uncertainty = "\n".join(
                        value for value in (old_uncertainty, _UNKNOWN_EXPECTATION_REVIEW) if value
                    )
                    catalogue.worksheet.cell(
                        row=record.row_number,
                        column=catalogue.headers["uncertainty"],
                    ).value = new_uncertainty
                    record.values["uncertainty"] = new_uncertainty
                    catalogue.changes.append(
                        CatalogueChange(
                            record.row_number,
                            "uncertainty",
                            old_uncertainty,
                            new_uncertainty,
                        )
                    )

        old_headers = tuple(catalogue.headers)
        IdentifierMigrationWorkflow._reorder_sheet(
            catalogue.worksheet,
            catalogue.headers,
            CATALOGUE_FIELDS,
        )
        catalogue.headers = {
            field: index for index, field in enumerate(CATALOGUE_FIELDS, start=1)
        }
        catalogue.mark_schema_dirty()
        for record in catalogue.records:
            record.values = {
                field: catalogue.worksheet.cell(
                    row=record.row_number,
                    column=catalogue.headers[field],
                ).value
                for field in CATALOGUE_FIELDS
            }
        catalogue.changes.append(
            CatalogueChange(1, "__schema__", old_headers, CATALOGUE_FIELDS)
        )
        catalogue._validate_duplicate_values()
        catalogue._validate_record_semantics()
        catalogue._validate_documents()

    def _reference_receipt_uuids(self) -> set[str]:
        root = self.settings.state_dir / "imports" / "reference_text"
        found: set[str] = set()
        if not root.is_dir():
            return found
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            for resolution in payload.get("resolutions", ()):
                if not isinstance(resolution, dict):
                    continue
                paper_uuid = normalized_text(resolution.get("paper_uuid"))
                if paper_uuid:
                    found.add(paper_uuid)
        return found
