from __future__ import annotations

import re
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import PurePosixPath
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
from ..services.download_service import DownloadService
from ..services.journal_service import OperationJournal, incomplete_journals
from ..services.metadata_service import CompositeMetadataLookupService
from ..services.report_service import ReportService, append_change_log
from ..services.record_canonicalization_service import RegisteredRecordCanonicalizer
from ..services.snapshot_service import SnapshotService
from ..utils.identifiers import normalize_doi, normalize_pmid
from ..utils.filename import standard_pdf_filename_result
from ..utils.normalize import normalized_text
from ..utils.text import normalize_title
from ..utils.uncertainty import confirmed_value
from .daily_check import DailyCheckWorkflow


class MetadataQueryWorkflow:
    def __init__(
        self,
        settings: Settings,
        metadata_service: CompositeMetadataLookupService | None = None,
        download_service: DownloadService | None = None,
    ):
        self.settings = settings
        self.metadata_service = metadata_service or CompositeMetadataLookupService(settings)
        self.download_service = download_service or DownloadService(settings)
        self.canonicalizer = RegisteredRecordCanonicalizer()

    def run(
        self,
        request: MetadataLookupRequest,
        *,
        dry_run: bool = False,
        catalogue_row: int | None = None,
        catalogue_id: str | None = None,
        missing_metadata: bool = False,
        incomplete_records: bool = False,
        normalize_existing: bool = False,
        max_records: int = 25,
        nested: bool = False,
        workflow_name: str = "metadata_query",
        download: bool = False,
        download_source: str = "auto",
        max_download_size_mb: float | None = None,
        download_timeout: float | None = None,
    ) -> WorkflowResult:
        result = WorkflowResult(
            workflow_name, dry_run=dry_run, mode="dry_run" if dry_run else "apply"
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous = snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        catalogue.configure_review_state(previous)
        record_uids_assigned = 0
        if normalize_existing:
            for record in records:
                if not record.get("record_uid"):
                    catalogue.ensure_record_uid(record)
                    record_uids_assigned += 1
                    result.completed.append(
                        {
                            "action": (
                                "would_assign_record_uid" if dry_run else "assigned_record_uid"
                            ),
                            "row": record.row_number,
                            "record_uid": record.get("record_uid"),
                        }
                    )
        for journal in incomplete_journals(self.settings.state_dir):
            if journal.get("workflow") == workflow_name:
                result.needs_review.append(
                    {**journal, "issue": "catalogue_write_incomplete"}
                )
        targets = self._targets(
            records,
            catalogue_row=catalogue_row,
            catalogue_id=catalogue_id,
            missing_metadata=missing_metadata,
            incomplete_records=incomplete_records,
            normalize_existing=normalize_existing,
            max_records=max_records,
        )
        if (catalogue_row is not None or catalogue_id) and not targets:
            result.needs_review.append(
                {
                    "issue": "metadata_query_target_not_found",
                    "row": catalogue_row,
                    "catalogue_id": catalogue_id,
                }
            )

        jobs: list[tuple[CatalogueRecord | None, MetadataLookupRequest]] = []
        if targets:
            jobs.extend(
                (
                    record,
                    self._request_for_record(
                        request,
                        record,
                        exact_only=incomplete_records or normalize_existing,
                    ),
                )
                for record in targets
            )
        elif not (
            catalogue_row is not None
            or catalogue_id
            or missing_metadata
            or incomplete_records
            or normalize_existing
        ):
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
        download_reports: list[dict[str, Any]] = []
        download_journals: list[OperationJournal] = []

        lookups = (
            self.metadata_service.lookup_many([job for _, job in jobs])
            if (missing_metadata or incomplete_records or normalize_existing)
            and hasattr(self.metadata_service, "lookup_many")
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
                    selected_target = new_record
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
                if selected_target is not None:
                    type_result = metadata.publication_type_result()
                    for warning in type_result.warnings:
                        if warning not in {
                            "publication_type_conflict",
                            "publication_type_unrecognized",
                        }:
                            continue
                        details = (
                            "Multiple equally ranked publication genres conflict."
                            if warning == "publication_type_conflict"
                            else "Publication type contains an unrecognized provider value."
                        )
                        self._record_review(
                            catalogue,
                            selected_target,
                            result,
                            warning,
                            details,
                            field="publication_type",
                        )
                        if warning not in query_report["issue_keys"]:
                            query_report["issue_keys"].append(warning)
                if download:
                    download_report, journal, file_changed = self._process_download(
                        catalogue,
                        selected_target,
                        metadata,
                        result,
                        dry_run=dry_run,
                        source=download_source,
                        max_download_size_mb=max_download_size_mb,
                        timeout_seconds=download_timeout,
                    )
                    download_reports.append(download_report)
                    if journal is not None:
                        download_journals.append(journal)
                    result.changed_files += int(file_changed)
            else:
                self._handle_lookup_issue(catalogue, target, lookup, result, query_report)

        changed_rows = {change.row_number for change in catalogue.changes}
        filename_implications: list[dict[str, Any]] = []
        if workflow_name == "record_normalization":
            for record in records:
                if record.row_number not in changed_rows:
                    continue
                relative = str(record.get("pdf_relative_path") or "").replace("\\", "/")
                current_filename = str(record.get("pdf_filename") or "")
                if not relative or not current_filename:
                    continue
                proposed = standard_pdf_filename_result(
                    title=record.get("title"),
                    year=record.get("year"),
                    journal_abbrev=record.get("journal_abbrev"),
                    journal=record.get("journal"),
                    publication_type=record.get("publication_type"),
                    max_length=self.settings.max_filename_length,
                ).filename
                if proposed and proposed != current_filename:
                    first_part = PurePosixPath(relative).parts[0] if PurePosixPath(relative).parts else ""
                    filename_implications.append(
                        {
                            "row": record.row_number,
                            "record_uid": record.get("record_uid") or None,
                            "current_path": relative,
                            "current_filename": current_filename,
                            "proposed_filename": proposed,
                            "location_scope": (
                                "registered"
                                if first_part.casefold() == "registered"
                                else "topic_folder"
                            ),
                            "action": "report_only",
                        }
                    )
        result.changed_rows = len(changed_rows)
        result.counts = {
            "queries": len(jobs),
            "records_found": len(records_found),
            "records_added": len(records_added),
            "records_updated": len(records_updated),
            "records_unchanged": len(records_unchanged),
            "record_uids_assigned": record_uids_assigned,
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
            "record_uids_assigned": record_uids_assigned,
            "conflicts": conflicts,
            "downloads": download_reports,
            "final_check": {},
            "network_failure": bool(result.failures),
            "filename_implications": filename_implications,
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
                    "record_uid": next(
                        (
                            record.get("record_uid") or None
                            for record in catalogue.records
                            if record.row_number == row
                        ),
                        None,
                    ),
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
                workflow=workflow_name,
                suffix="normalize" if workflow_name == "record_normalization" else "metadata",
            )
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            if catalogue.maintenance_actions:
                result.details["backup_maintenance"] = list(
                    catalogue.maintenance_actions
                )
            for row in changed_rows:
                record_uid = next(
                    (
                        record.get("record_uid") or None
                        for record in catalogue.records
                        if record.row_number == row
                    ),
                    None,
                )
                journal.set_operation_state(
                    row, "catalogue_committed", record_uid=record_uid
                )

        if catalogue.changes or result.changed_files:
            append_change_log(
                self.settings.changes_log_path,
                workflow=(
                    "Record normalization"
                    if workflow_name == "record_normalization"
                    else "Workflow 2"
                ),
                action=(
                    "Canonicalize existing catalogue records"
                    if workflow_name == "record_normalization"
                    else "Metadata query, catalogue completion, and controlled OA download"
                ),
                files_changed=result.changed_files,
                catalogue_rows_changed=len(changed_rows),
                reason="Processed high-confidence metadata and explicitly authorized downloads",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        for download_journal in download_journals:
            for operation in download_journal.payload["operations"]:
                download_journal.set_operation_state(
                    operation.get("catalogue_row"), "catalogue_committed"
                )

        if (catalogue.changes or result.changed_files) and not nested:
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
            for download_journal in download_journals:
                download_journal.finish("final_check_committed")
        elif journal:
            journal.finish("catalogue_committed")
            for download_journal in download_journals:
                download_journal.finish("final_check_committed")
        else:
            for download_journal in download_journals:
                download_journal.finish("final_check_committed")

        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _process_download(
        self,
        catalogue: CatalogueService,
        target: CatalogueRecord | None,
        metadata: MetadataRecord,
        result: WorkflowResult,
        *,
        dry_run: bool,
        source: str,
        max_download_size_mb: float | None,
        timeout_seconds: float | None,
    ) -> tuple[dict[str, Any], OperationJournal | None, bool]:
        report: dict[str, Any] = {
            "requested": True,
            "catalogue_row": target.row_number if target else None,
            "candidates": [
                {
                    "provider": item.provider,
                    "url": self.download_service.safe_url(item.source_url),
                    "priority": item.priority,
                }
                for item in metadata.download_candidates
            ],
        }
        if not self.settings.download.enabled:
            report.update(status="disabled", reason="download_disabled")
            self._record_review(
                catalogue,
                target,
                result,
                "download_disabled",
                "PDF download is disabled by configuration.",
                field="pdf_download",
            )
            return report, None, False
        candidate = self.download_service.select_candidate(
            metadata.download_candidates, source=source
        )
        if candidate is None:
            report.update(status="no_candidate", reason="no_legal_direct_pdf_candidate")
            self._record_review(
                catalogue,
                target,
                result,
                "no_download_candidate",
                "No explicit legal direct-PDF candidate was returned by arXiv or Unpaywall.",
                field="pdf_download",
            )
            return report, None, False

        run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f-download")
        try:
            plan = self.download_service.plan(
                candidate,
                run_id=run_id,
                max_bytes=(
                    max(1, int(max_download_size_mb * 1024 * 1024))
                    if max_download_size_mb is not None
                    else None
                ),
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            report.update(status="blocked", reason=str(exc))
            self._record_review(
                catalogue,
                target,
                result,
                "unsafe_download_candidate",
                str(exc),
                field="pdf_download",
            )
            return report, None, False
        report.update(
            selected_provider=candidate.provider,
            selected_url=self.download_service.safe_url(candidate.source_url),
            selection_reason=candidate.selection_reason,
            target=f"Inbox/{plan.target_filename}",
            max_bytes=plan.max_bytes,
            timeout_seconds=plan.timeout_seconds,
        )
        if dry_run:
            report["status"] = "planned"
            return report, None, False

        operation = {
            "catalogue_row": target.row_number if target else None,
            "execution_state": "candidate_selected",
            "provider": candidate.provider,
            "source_url": self.download_service.safe_url(candidate.source_url),
            "target": f"Inbox/{plan.target_filename}",
        }
        journal = OperationJournal.create(
            self.settings.state_dir,
            [operation],
            workflow="metadata_query",
            suffix="download",
        )
        row = target.row_number if target else None

        def record_stage(stage: str, details: dict[str, object]) -> None:
            journal.set_operation_state(row, stage, **details)

        outcome = self.download_service.execute(plan, stage_callback=record_stage)
        if outcome.status != "downloaded":
            journal.set_operation_state(
                row,
                outcome.status,
                error=outcome.error,
            )
        report.update(
            status=outcome.status,
            bytes_downloaded=outcome.bytes_downloaded,
            content_type=outcome.content_type,
            error=outcome.error,
            validation=(asdict(outcome.validation) if outcome.validation else None),
        )
        if outcome.status in {"downloaded", "already_present"}:
            if target is not None:
                updates = {
                    key: value
                    for key, value in {
                        "pdf_status": PdfStatus.INBOX.value,
                        "pdf_filename": plan.target_filename,
                        "pdf_relative_path": f"Inbox/{plan.target_filename}",
                        "date_updated": date.today().isoformat(),
                    }.items()
                    if key in catalogue.headers
                }
                catalogue.update_fields(target, updates)
            result.completed.append(
                {
                    "action": outcome.status,
                    "row": row,
                    "path": f"Inbox/{plan.target_filename}",
                }
            )
            return report, journal, outcome.status == "downloaded"

        self._record_review(
            catalogue,
            target,
            result,
            outcome.status,
            outcome.error or "Downloaded payload did not pass controlled validation.",
            field="pdf_download",
        )
        return report, journal, False

    @staticmethod
    def _targets(
        records: list[CatalogueRecord],
        *,
        catalogue_row: int | None,
        catalogue_id: str | None,
        missing_metadata: bool,
        incomplete_records: bool = False,
        normalize_existing: bool = False,
        max_records: int = 25,
    ) -> list[CatalogueRecord]:
        if catalogue_row is not None:
            return [item for item in records if item.row_number == catalogue_row]
        if catalogue_id:
            key = normalized_text(catalogue_id)
            return [item for item in records if normalized_text(item.get("id")) == key]
        if incomplete_records or normalize_existing:
            candidates = []
            for item in records:
                has_identifier = bool(
                    normalize_pmid(item.get("pmid"))
                    or normalize_doi(item.get("doi"))
                    or MetadataQueryWorkflow._record_arxiv_id(item)
                )
                if not has_identifier:
                    continue
                if normalize_existing or MetadataQueryWorkflow._record_is_incomplete(item):
                    candidates.append(item)
            return candidates[:max_records]
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
        base: MetadataLookupRequest,
        record: CatalogueRecord,
        *,
        exact_only: bool = False,
    ) -> MetadataLookupRequest:
        pmid = (
            str(record.get("pmid") or "")
            or MetadataQueryWorkflow._confirmed_value(record, "pmid")
            or base.pmid
        )
        doi = (
            str(record.get("doi") or "")
            or MetadataQueryWorkflow._confirmed_value(record, "doi")
            or base.doi
        )
        arxiv_id = MetadataQueryWorkflow._record_arxiv_id(record) or base.arxiv_id
        return replace(
            base,
            pmid=pmid,
            doi=None if pmid else doi,
            arxiv_id=None if pmid or doi else arxiv_id,
            title=None if exact_only else (str(record.get("title") or "") or base.title),
            authors=None if exact_only else (str(record.get("authors") or "") or base.authors),
            year=None if exact_only else (str(record.get("year") or "") or base.year),
            journal=None if exact_only else (str(record.get("journal") or "") or base.journal),
        )

    @staticmethod
    def _record_arxiv_id(record: CatalogueRecord) -> str:
        current_id = str(record.get("id") or "")
        if current_id.upper().startswith("ARXIV:"):
            return current_id.split(":", 1)[1].strip()
        return ""

    @staticmethod
    def _record_is_incomplete(record: CatalogueRecord) -> bool:
        identifier_available = bool(
            normalize_pmid(record.get("pmid"))
            or normalize_doi(record.get("doi"))
            or MetadataQueryWorkflow._record_arxiv_id(record)
        )
        if not identifier_available:
            return False
        source = str(record.get("source") or "")
        mixed_source = len([item for item in re.split(r"\s*;\s*", source) if item]) > 1
        local_primary = normalized_text(source) == "local pdf"
        local_id = str(record.get("id") or "").upper().startswith("LOCAL:")
        missing_core = any(
            field in record.values and not record.get(field)
            for field in ("authors", "year", "journal", "abstract")
        )
        journal_incomplete = bool(record.get("journal")) != bool(record.get("journal_abbrev"))
        return local_id or local_primary or mixed_source or missing_core or journal_incomplete

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
        issue_keys = self.canonicalizer.conflicts(record, metadata)
        conflicts: list[dict[str, Any]] = []
        for issue_key in issue_keys:
            field = issue_key.removeprefix("metadata_").removesuffix("_conflict")
            recorded_issue_key = (
                issue_key
                if issue_key == "metadata_journal_conflict"
                else "catalogue_existing_value_conflict"
            )
            issue = f"Existing {field} conflicts with the accepted provider record."
            outcome = catalogue.ensure_review_blocker(
                record,
                field,
                issue,
                issue_key=recorded_issue_key,
                conflict_with_confirmation=True,
            )
            if outcome in {"added", "existing"}:
                conflicts.append(
                    {
                        "row": record.row_number,
                        "record_uid": record.get("record_uid") or None,
                        "field": field,
                        "issue": recorded_issue_key,
                    }
                )
        if conflicts:
            return [], conflicts
        canonical = self.canonicalizer.canonicalize(catalogue, record, metadata)
        return canonical.changed_fields, []

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
            "id": RegisteredRecordCanonicalizer.canonical_id(metadata),
            **metadata.catalogue_fields(),
            "source": RegisteredRecordCanonicalizer.canonical_source(metadata),
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
        return confirmed_value(record.get("uncertainty"), field_name)
