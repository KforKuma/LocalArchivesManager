from __future__ import annotations

import re
from dataclasses import asdict, replace
from datetime import date
from typing import Any

from .. import __version__
from ..config import Settings
from ..models import (
    CatalogueRecord,
    MetadataLookupRequest,
    MetadataLookupStatus,
    MetadataRecord,
    PdfStatus,
    WorkflowResult,
)
from ..schema import MACHINE_FILLABLE_FIELDS
from ..services.catalogue_service import CatalogueService
from ..services.journal_service import OperationJournal, incomplete_journals
from ..services.metadata_service import CompositeMetadataLookupService
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from ..utils.identifiers import normalize_doi, normalize_pmid
from ..utils.normalize import normalized_text
from ..utils.text import normalize_title
from .daily_check import DailyCheckWorkflow


class MetadataQueryWorkflow:
    def __init__(
        self,
        settings: Settings,
        metadata_service: CompositeMetadataLookupService | None = None,
    ):
        self.settings = settings
        self.metadata_service = metadata_service or CompositeMetadataLookupService(settings)

    def run(
        self,
        request: MetadataLookupRequest,
        *,
        dry_run: bool = False,
        catalogue_row: int | None = None,
        catalogue_id: str | None = None,
        missing_metadata: bool = False,
        max_records: int = 25,
        nested: bool = False,
    ) -> WorkflowResult:
        result = WorkflowResult(
            "metadata_query", dry_run=dry_run, mode="dry_run" if dry_run else "apply"
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous = snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        catalogue.configure_review_state(previous)
        for journal in incomplete_journals(self.settings.state_dir):
            if journal.get("workflow") == "metadata_query":
                result.needs_review.append(
                    {**journal, "issue": "catalogue_write_incomplete"}
                )
        targets = self._targets(
            records,
            catalogue_row=catalogue_row,
            catalogue_id=catalogue_id,
            missing_metadata=missing_metadata,
            max_records=max_records,
        )
        if (catalogue_row is not None or catalogue_id or missing_metadata) and not targets:
            result.needs_review.append(
                {
                    "issue": "metadata_query_target_not_found",
                    "row": catalogue_row,
                    "catalogue_id": catalogue_id,
                }
            )

        jobs: list[tuple[CatalogueRecord | None, MetadataLookupRequest]] = []
        if targets:
            jobs.extend((record, self._request_for_record(request, record)) for record in targets)
        elif not (catalogue_row is not None or catalogue_id or missing_metadata):
            jobs.append((None, request))

        query_reports: list[dict[str, Any]] = []
        records_found: list[dict[str, Any]] = []
        records_added: list[dict[str, Any]] = []
        records_updated: list[dict[str, Any]] = []
        records_unchanged: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        provider_summary: dict[str, dict[str, Any]] = {}
        cache_hits = 0
        cache_misses = 0

        lookups = (
            self.metadata_service.lookup_many([job for _, job in jobs])
            if missing_metadata and hasattr(self.metadata_service, "lookup_many")
            else [self.metadata_service.lookup(job) for _, job in jobs]
        )
        for (target, job), lookup in zip(jobs, lookups, strict=True):
            if target is not None and lookup.status == MetadataLookupStatus.AMBIGUOUS:
                confirmed = self._confirmed_value(target, "metadata_candidate")
                selected = next(
                    (
                        MetadataRecord.from_dict(item)
                        for item in lookup.records
                        if normalized_text(item.get("canonical_id"))
                        == normalized_text(confirmed)
                    ),
                    None,
                )
                if selected is not None:
                    lookup.status = MetadataLookupStatus.FOUND
                    lookup.best_record = selected.to_dict()
                    lookup.confidence = "user_confirmed"
                    lookup.selection_reason = "USER_CONFIRMED selected the metadata candidate."
            for provider in lookup.provider_results:
                stats = provider_summary.setdefault(
                    provider.provider,
                    {
                        "status": provider.status.value,
                        "request_count": 0,
                        "cache_hits": 0,
                        "retries": 0,
                        "rate_limit_wait_seconds": 0.0,
                        "records_returned": 0,
                        "parse_errors": 0,
                    },
                )
                stats["status"] = provider.status.value
                stats["request_count"] += provider.stats.request_count
                stats["cache_hits"] += provider.stats.cache_hits
                stats["retries"] += provider.stats.retries
                stats["rate_limit_wait_seconds"] += provider.stats.rate_limit_wait_seconds
                stats["records_returned"] += provider.stats.records_returned
                stats["parse_errors"] += provider.stats.parse_errors
                cache_hits += int(provider.cache_hit)
                cache_misses += int(not provider.cache_hit)

            query_report = {
                "query_type": self._query_type(job),
                "query_value": self._query_value(job),
                "catalogue_row": target.row_number if target else None,
                "providers_attempted": [item.provider for item in lookup.provider_results],
                "provider_results": [
                    {
                        "provider": item.provider,
                        "status": item.status.value,
                        "records_returned": len(item.records),
                        "cache_hit": item.cache_hit,
                        "errors": item.errors,
                    }
                    for item in lookup.provider_results
                ],
                "selected_candidate": (
                    lookup.best_record.get("canonical_id") if lookup.best_record else None
                ),
                "selection_reason": lookup.selection_reason,
                "confidence": lookup.confidence,
                "catalogue_action": "none",
                "issue_keys": list(lookup.conflicts),
            }
            query_reports.append(query_report)

            if lookup.status == MetadataLookupStatus.FOUND and lookup.best_record:
                metadata = MetadataRecord.from_dict(lookup.best_record)
                records_found.append(
                    {
                        "canonical_id": metadata.canonical_id,
                        "providers": metadata.source,
                        "confidence": lookup.confidence,
                    }
                )
                selected_target = target
                if selected_target is None:
                    duplicates = self._duplicates(catalogue, metadata)
                    if len(duplicates) > 1:
                        self._record_review(
                            catalogue,
                            None,
                            result,
                            "metadata_query_ambiguous",
                            "New metadata may duplicate multiple catalogue rows.",
                            field="paper_identity",
                        )
                        query_report["catalogue_action"] = "blocked_duplicate"
                        continue
                    selected_target = duplicates[0] if duplicates else None

                if selected_target is not None:
                    changed, row_conflicts = self._plan_update(
                        catalogue, selected_target, metadata
                    )
                    conflicts.extend(row_conflicts)
                    if row_conflicts:
                        result.needs_review.extend(
                            item for item in row_conflicts if item not in result.needs_review
                        )
                    if changed:
                        item = {
                            "row": selected_target.row_number,
                            "id": selected_target.get("id"),
                            "fields": changed,
                        }
                        records_updated.append(item)
                        result.completed.append(
                            {"action": "would_update" if dry_run else "updated", **item}
                        )
                        query_report["catalogue_action"] = "update"
                    else:
                        item = {
                            "row": selected_target.row_number,
                            "id": selected_target.get("id"),
                        }
                        records_unchanged.append(item)
                        query_report["catalogue_action"] = "unchanged"
                elif self._eligible_new_record(metadata, lookup.confidence):
                    values = self._new_row_values(catalogue, metadata)
                    new_record = catalogue.add_record(values)
                    item = {
                        "row": new_record.row_number,
                        "id": new_record.get("id"),
                        "canonical_id": metadata.canonical_id,
                    }
                    records_added.append(item)
                    result.completed.append(
                        {"action": "would_add" if dry_run else "added", **item}
                    )
                    query_report["catalogue_action"] = "add"
                else:
                    self._record_review(
                        catalogue,
                        target,
                        result,
                        "metadata_query_ambiguous",
                        "Candidate lacks sufficient evidence for a new catalogue row.",
                        field="paper_identity",
                    )
                    query_report["catalogue_action"] = "blocked_low_confidence"
            else:
                self._handle_lookup_issue(catalogue, target, lookup, result, query_report)

        changed_rows = {change.row_number for change in catalogue.changes}
        result.changed_rows = len(changed_rows)
        result.counts = {
            "queries": len(jobs),
            "records_found": len(records_found),
            "records_added": len(records_added),
            "records_updated": len(records_updated),
            "records_unchanged": len(records_unchanged),
        }
        result.details = {
            "version": __version__,
            "queries": query_reports,
            "providers": provider_summary,
            "cache": {"hits": cache_hits, "misses": cache_misses},
            "records_found": records_found,
            "records_added": records_added,
            "records_updated": records_updated,
            "records_unchanged": records_unchanged,
            "conflicts": conflicts,
            "downloads": [],
            "final_check": {},
            "network_failure": bool(result.failures),
        }

        if dry_run:
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = None
        if catalogue.changes:
            operations = [
                {
                    "catalogue_row": row,
                    "execution_state": "planned",
                    "changes": [
                        asdict(change) for change in catalogue.changes if change.row_number == row
                    ],
                }
                for row in sorted(changed_rows)
            ]
            journal = OperationJournal.create(
                self.settings.state_dir,
                operations,
                workflow="metadata_query",
                suffix="metadata",
            )
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            for row in changed_rows:
                journal.set_operation_state(row, "catalogue_committed")
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 2",
                action="Metadata query and catalogue completion",
                files_changed=0,
                catalogue_rows_changed=len(changed_rows),
                reason="Added or completed high-confidence bibliographic metadata",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        if catalogue.changes and not nested:
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
        elif journal:
            journal.finish("catalogue_committed")

        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    @staticmethod
    def _targets(
        records: list[CatalogueRecord],
        *,
        catalogue_row: int | None,
        catalogue_id: str | None,
        missing_metadata: bool,
        max_records: int,
    ) -> list[CatalogueRecord]:
        if catalogue_row is not None:
            return [item for item in records if item.row_number == catalogue_row]
        if catalogue_id:
            key = normalized_text(catalogue_id)
            return [item for item in records if normalized_text(item.get("id")) == key]
        if missing_metadata:
            candidates = []
            for item in records:
                uncertainty = str(item.get("uncertainty") or "")
                active_fields = set(
                    re.findall(
                        r"^NEEDS_REVIEW:\s*field=(metadata_[^;\r\n]+)",
                        uncertainty,
                        re.I | re.M,
                    )
                )
                confirmed_fields = set(
                    re.findall(
                        r"^USER_CONFIRMED:\s*field=(metadata_[^;\r\n]+)",
                        uncertainty,
                        re.I | re.M,
                    )
                )
                active = bool(
                    {item.casefold() for item in active_fields}
                    - {item.casefold() for item in confirmed_fields}
                )
                missing = any(not item.get(field) for field in MACHINE_FILLABLE_FIELDS if field in item.values)
                if missing and not active and (item.get("pmid") or item.get("doi") or item.get("title")):
                    candidates.append(item)
            return candidates[:max_records]
        return []

    @staticmethod
    def _request_for_record(
        base: MetadataLookupRequest, record: CatalogueRecord
    ) -> MetadataLookupRequest:
        return replace(
            base,
            pmid=(
                str(record.get("pmid") or "")
                or MetadataQueryWorkflow._confirmed_value(record, "pmid")
                or base.pmid
            ),
            doi=(
                str(record.get("doi") or "")
                or MetadataQueryWorkflow._confirmed_value(record, "doi")
                or base.doi
            ),
            title=str(record.get("title") or "") or base.title,
            authors=str(record.get("authors") or "") or base.authors,
            year=str(record.get("year") or "") or base.year,
            journal=str(record.get("journal") or "") or base.journal,
        )

    @staticmethod
    def _duplicates(
        catalogue: CatalogueService, metadata: MetadataRecord
    ) -> list[CatalogueRecord]:
        for field, value in (("pmid", metadata.pmid), ("doi", metadata.doi)):
            if value and field in catalogue.headers:
                matches = catalogue.find_by(field, value)
                if matches:
                    return matches
        title = normalize_title(metadata.title)
        if title and "title" in catalogue.headers:
            matches = [
                record
                for record in catalogue.records
                if normalize_title(record.get("title")) == title
            ]
            if matches:
                return matches
        return []

    def _plan_update(
        self,
        catalogue: CatalogueService,
        record: CatalogueRecord,
        metadata: MetadataRecord,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        proposed = metadata.catalogue_fields()
        updates: dict[str, Any] = {}
        conflicts: list[dict[str, Any]] = []
        for field, value in proposed.items():
            if field not in catalogue.headers or value in (None, ""):
                continue
            current = record.get(field)
            if current in (None, ""):
                updates[field] = value
            elif field in MACHINE_FILLABLE_FIELDS and not self._equivalent(field, current, value):
                issue = f"Existing value {current!r} conflicts with provider value {value!r}."
                outcome = catalogue.ensure_review_blocker(
                    record,
                    field,
                    issue,
                    issue_key="catalogue_existing_value_conflict",
                    conflict_with_confirmation=True,
                )
                if outcome in {"added", "existing"}:
                    conflicts.append(
                        {
                            "row": record.row_number,
                            "field": field,
                            "issue": "catalogue_existing_value_conflict",
                            "existing": current,
                            "provider": value,
                        }
                    )
        if updates and "date_updated" in catalogue.headers:
            updates["date_updated"] = date.today().isoformat()
        changes = catalogue.update_fields(record, updates) if updates else []
        return [change.field_name for change in changes], conflicts

    @staticmethod
    def _equivalent(field: str, left: Any, right: Any) -> bool:
        if field == "doi":
            return normalize_doi(left) == normalize_doi(right)
        if field == "pmid":
            return normalize_pmid(left) == normalize_pmid(right)
        if field == "title":
            return normalize_title(left) == normalize_title(right)
        return normalized_text(left) == normalized_text(right)

    @staticmethod
    def _eligible_new_record(metadata: MetadataRecord, confidence: str) -> bool:
        if confidence == "exact_identifier":
            return bool(metadata.pmid or metadata.doi or metadata.arxiv_id)
        return confidence == "exact_title_supported" and bool(
            metadata.title and metadata.authors and metadata.year and metadata.source
        )

    @staticmethod
    def _new_row_values(
        catalogue: CatalogueService, metadata: MetadataRecord
    ) -> dict[str, Any]:
        today = date.today().isoformat()
        values = {
            "id": metadata.canonical_id,
            **metadata.catalogue_fields(),
            "pdf_status": PdfStatus.NOT_DOWNLOADED.value,
            "date_added": today,
            "date_updated": today,
        }
        return {key: value for key, value in values.items() if key in catalogue.headers and value not in (None, "")}

    def _handle_lookup_issue(
        self,
        catalogue: CatalogueService,
        target: CatalogueRecord | None,
        lookup: Any,
        result: WorkflowResult,
        query_report: dict[str, Any],
    ) -> None:
        mapping = {
            MetadataLookupStatus.NOT_FOUND: "metadata_query_not_found",
            MetadataLookupStatus.AMBIGUOUS: "metadata_query_ambiguous",
            MetadataLookupStatus.CONFLICT: "metadata_cross_provider_conflict",
            MetadataLookupStatus.UNAVAILABLE: "metadata_provider_unavailable",
            MetadataLookupStatus.FAILED: "metadata_provider_failed",
        }
        issue_key = mapping[lookup.status]
        details = lookup.selection_reason or "; ".join(lookup.errors) or lookup.status.value
        query_report["catalogue_action"] = "blocked"
        if issue_key not in query_report["issue_keys"]:
            query_report["issue_keys"].append(issue_key)
        all_disabled = bool(lookup.provider_results) and all(
            item.status.value == "disabled" for item in lookup.provider_results
        )
        if lookup.status in {MetadataLookupStatus.UNAVAILABLE, MetadataLookupStatus.FAILED} and not all_disabled:
            result.failures.append({"row": target.row_number if target else None, "issue": issue_key, "details": details})
            return
        self._record_review(
            catalogue,
            target,
            result,
            issue_key,
            details,
            field=issue_key,
        )

    @staticmethod
    def _record_review(
        catalogue: CatalogueService,
        record: CatalogueRecord | None,
        result: WorkflowResult,
        issue_key: str,
        issue: str,
        *,
        field: str,
    ) -> None:
        item = {"row": record.row_number if record else None, "issue": issue_key, "details": issue}
        if record is None:
            if item not in result.needs_review:
                result.needs_review.append(item)
            return
        outcome = catalogue.ensure_review_blocker(
            record,
            field,
            issue,
            issue_key=issue_key,
            conflict_with_confirmation=issue_key in {"metadata_cross_provider_conflict"},
        )
        if outcome in {"added", "existing"} and item not in result.needs_review:
            result.needs_review.append(item)
        elif outcome not in {"added", "existing"}:
            result.skipped.append({**item, "reason": f"review_{outcome}"})

    @staticmethod
    def _query_type(request: MetadataLookupRequest) -> str:
        if request.pmid:
            return "pmid"
        if request.doi:
            return "doi"
        if request.arxiv_id:
            return "arxiv_id"
        return "title"

    @staticmethod
    def _query_value(request: MetadataLookupRequest) -> str:
        return request.pmid or request.doi or request.arxiv_id or request.title or ""

    @staticmethod
    def _confirmed_value(record: CatalogueRecord, field_name: str) -> str:
        match = re.search(
            rf"^USER_CONFIRMED:\s*field={re.escape(field_name)};\s*value=([^;\r\n]*)",
            str(record.get("uncertainty") or ""),
            re.I | re.M,
        )
        return match.group(1).strip() if match else ""
