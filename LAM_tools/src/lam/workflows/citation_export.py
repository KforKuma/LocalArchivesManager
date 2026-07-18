from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from filelock import FileLock

from ..config import Settings
from ..exceptions import ConfigurationError
from ..models import CitationExportRecord, WorkflowResult
from ..run_context import current_run_context
from ..services.catalogue_service import CatalogueService
from ..services.citation_export_service import (
    ExportArtifactWriter,
    NbibSerializer,
    PubMedCitationClient,
    citation_record_from_catalogue,
    enrich_citation_record_from_provider_cache,
    merge_pubmed_xml,
    validate_local_record,
)
from ..services.report_service import ReportService
from ..utils.normalize import normalized_text


class CitationExportWorkflow:
    def __init__(
        self,
        settings: Settings,
        *,
        pubmed_client: PubMedCitationClient | None = None,
        writer: type[ExportArtifactWriter] = ExportArtifactWriter,
    ):
        self.settings = settings
        self.pubmed = pubmed_client or PubMedCitationClient(settings)
        self.writer = writer

    def run(
        self,
        *,
        dry_run: bool,
        all_records: bool = False,
        paper_uuid: str | None = None,
        topic_folder: str | None = None,
        format_name: str = "nbib",
        official_only: bool = False,
        offline: bool = False,
        refresh: bool = False,
        cache_write: bool = True,
        output: Path | None = None,
    ) -> WorkflowResult:
        if format_name not in {"nbib", "pubmed-xml"}:
            raise ConfigurationError(f"Unsupported citation export format: {format_name}")
        result = WorkflowResult(
            "citation_export",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(
            self.settings.catalogue_path, allow_citation_duplicates=True
        )
        records = catalogue.load()
        selected = self._select(
            catalogue,
            records,
            all_records=all_records,
            paper_uuid=paper_uuid,
            topic_folder=topic_folder,
            result=result,
        )
        selected_count = len(selected)
        record_reports = [self._record_report(record) for record in selected]
        reports_by_uuid = {
            str(report["paper_uuid"]): report for report in record_reports
        }
        selected, dedupe_skips = self._deduplicate(selected, result)
        result.skipped.extend(dedupe_skips)
        dedupe_skip_ids = {
            str(item["paper_uuid"])
            for item in dedupe_skips
            if item.get("paper_uuid")
        }
        selected_ids = {record.paper_uuid for record in selected}
        for paper_id, report in reports_by_uuid.items():
            if paper_id in dedupe_skip_ids:
                reason = next(
                    item["reason"]
                    for item in dedupe_skips
                    if item.get("paper_uuid") == paper_id
                )
                report.update({"export_status": "skipped", "skip_reason": reason})
            elif paper_id not in selected_ids:
                report.update(
                    {
                        "export_status": "needs_review",
                        "skip_reason": "citation_identifier_or_metadata_conflict",
                    }
                )

        payloads: list[tuple[CitationExportRecord, bytes, str, str, bool]] = []
        provider_failures = 0
        cache_hits = 0
        official_count = 0
        generated_count = 0

        for record in selected:
            report = reports_by_uuid[record.paper_uuid]
            if record.pmid:
                official = self.pubmed.fetch(
                    record.pmid,
                    format_name=format_name,
                    offline=offline,
                    refresh=refresh,
                    cache_write=cache_write,
                )
                report["export_status"] = official.status
                if official.status in {
                    "official_nbib_exported",
                    "official_nbib_cache_hit",
                    "official_xml_exported",
                    "official_xml_cache_hit",
                }:
                    if record.doi and official.doi and record.doi != official.doi:
                        report.update(
                            {
                                "export_status": "pubmed_record_mismatch",
                                "skip_reason": "Catalogue DOI conflicts with official PubMed DOI",
                            }
                        )
                        result.needs_review.append(
                            {
                                "paper_uuid": record.paper_uuid,
                                "pmid": record.pmid,
                                "issue": "pubmed_catalogue_identifier_conflict",
                            }
                        )
                    else:
                        record.record_source = "pubmed"
                        payloads.append(
                            (
                                record,
                                self._terminated(official.content, format_name),
                                "pubmed",
                                official.status,
                                official.cache_hit,
                            )
                        )
                        report["record_source"] = "pubmed"
                        official_count += 1
                        cache_hits += int(official.cache_hit)
                else:
                    report["skip_reason"] = official.error or official.status
                    issue = {
                        "paper_uuid": record.paper_uuid,
                        "pmid": record.pmid,
                        "issue": official.status,
                    }
                    result.needs_review.append(issue)
                    provider_failures += int(official.status == "provider_failed")
            elif official_only or format_name == "pubmed-xml":
                reason = (
                    "official_only_without_pmid"
                    if official_only
                    else "pubmed_xml_requires_pmid"
                )
                report.update(
                    {"export_status": "skipped", "skip_reason": reason}
                )
                result.skipped.append(
                    {"paper_uuid": record.paper_uuid, "reason": reason}
                )
            else:
                missing = validate_local_record(record)
                if missing:
                    report.update(
                        {
                            "export_status": "metadata_incomplete",
                            "skip_reason": f"missing: {', '.join(missing)}",
                        }
                    )
                    result.needs_review.append(
                        {
                            "paper_uuid": record.paper_uuid,
                            "issue": "citation_metadata_incomplete",
                            "missing_fields": missing,
                        }
                    )
                else:
                    content = NbibSerializer.serialize(record)
                    payloads.append(
                        (record, content, "lam_generated", "lam_nbib_exported", False)
                    )
                    report.update(
                        {
                            "record_source": "lam_generated",
                            "export_status": "lam_nbib_exported",
                        }
                    )
                    generated_count += 1

        if provider_failures and not payloads:
            result.failures.append(
                {
                    "issue": "provider_failed_for_all_exportable_records",
                    "provider_failures": provider_failures,
                }
            )

        plans = self._plans(
            payloads,
            format_name=format_name,
            paper_uuid_selector=paper_uuid,
            output=output,
        )
        collisions = [plan for plan in plans if plan.action == "collision"]
        collision_reports = [self._plan_report(plan) for plan in collisions]
        if collisions:
            result.needs_review.extend(
                {
                    "issue": "export_target_collision",
                    "output_path": str(plan.path),
                }
                for plan in collisions
            )
            plans = []

        bytes_written = 0
        output_files: list[dict[str, Any]] = list(collision_reports)
        if dry_run:
            for plan in plans:
                output_files.append(self._plan_report(plan))
                result.completed.append(
                    {
                        "action": f"would_{plan.action}_export",
                        "output_path": str(plan.path),
                        "record_count": plan.record_count,
                    }
                )
        elif plans:
            self.settings.export_lock_path.parent.mkdir(parents=True, exist_ok=True)
            context = current_run_context()
            if context is not None:
                context.lock_state = "export_required"
            with FileLock(self.settings.export_lock_path, timeout=0):
                if context is not None:
                    context.lock_state = "export_acquired"
                written_results = self.writer.commit_many(plans)
                for plan, written in zip(plans, written_results, strict=True):
                    bytes_written += written
                    output_files.append(self._plan_report(plan))
                    if written:
                        result.changed_files += 1
                        result.completed.append(
                            {
                                "action": f"{plan.action}d_export",
                                "output_path": str(plan.path),
                                "record_count": plan.record_count,
                                "bytes": written,
                            }
                        )
                    else:
                        result.skipped.append(
                            {
                                "output_path": str(plan.path),
                                "reason": "identical_lam_export_exists",
                            }
                        )

        exported_count = len(payloads) if not collisions else 0
        if plans:
            main_path = str(plans[0].path)
            individual_paths = {
                plan.path.stem: str(plan.path)
                for plan in plans[1:]
                if plan.path.parent.name.casefold() == "records"
            }
            for report in record_reports:
                if report["export_status"] not in {
                    "official_nbib_exported",
                    "official_nbib_cache_hit",
                    "official_xml_exported",
                    "official_xml_cache_hit",
                    "lam_nbib_exported",
                }:
                    continue
                report["output_path"] = individual_paths.get(
                    str(report["paper_uuid"]), main_path
                )
        result.counts = {
            "selected_records": selected_count,
            "exported_records": exported_count,
            "official_pubmed_records": official_count,
            "lam_generated_records": generated_count,
            "skipped_records": len(result.skipped),
            "provider_failures": provider_failures,
            "cache_hits": cache_hits,
            "output_files": len(output_files),
            "bytes_written": bytes_written,
        }
        result.details = {
            "format": format_name,
            "official_only": official_only,
            "provider_policy": {
                "offline": offline,
                "refresh": refresh,
                "cache_write": cache_write,
            },
            "selection": {
                "all": all_records,
                "paper_uuid": paper_uuid,
                "topic_folder": topic_folder,
            },
            "records": record_reports,
            "output_files": output_files,
            "bytes_written": bytes_written,
            "modifies_workbook": False,
            "modifies_managed_files": False,
            "runs_final_check": False,
        }
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _select(
        self,
        catalogue: CatalogueService,
        records: list[Any],
        *,
        all_records: bool,
        paper_uuid: str | None,
        topic_folder: str | None,
        result: WorkflowResult,
    ) -> list[CitationExportRecord]:
        selected = []
        for record in records:
            uuid_value = str(record.get("paper_uuid") or "").strip()
            registered = bool(catalogue.documents_for_paper(uuid_value))
            matches = all_records
            if paper_uuid:
                matches = normalized_text(uuid_value) == normalized_text(paper_uuid)
            elif topic_folder:
                matches = normalized_text(
                    str(record.get("topic_folder") or "").replace("\\", "/")
                ) == normalized_text(topic_folder.replace("\\", "/"))
            if not matches:
                continue
            if not registered:
                result.needs_review.append(
                    {"paper_uuid": uuid_value, "issue": "citation_record_not_registered"}
                )
                continue
            projected = citation_record_from_catalogue(record)
            if not projected.pmid:
                enrich_citation_record_from_provider_cache(
                    projected,
                    self.settings.metadata_cache_dir,
                    expected_versions={
                        "cache_schema_version": self.settings.cache.cache_schema_version,
                        "parser_version": self.settings.cache.parser_version,
                        "provider_schema_version": self.settings.cache.provider_schema_version,
                    },
                )
            selected.append(projected)
        if not selected and not result.needs_review:
            result.needs_review.append(
                {
                    "issue": "citation_export_selection_empty",
                    "paper_uuid": paper_uuid,
                    "topic_folder": topic_folder,
                }
            )
        return selected

    def _deduplicate(
        self,
        records: list[CitationExportRecord],
        result: WorkflowResult,
    ) -> tuple[list[CitationExportRecord], list[dict[str, Any]]]:
        blocked: set[str] = set()
        skips: list[dict[str, Any]] = []
        pmids: dict[str, list[CitationExportRecord]] = {}
        for record in records:
            if record.pmid:
                pmids.setdefault(record.pmid, []).append(record)
        for pmid, group in pmids.items():
            if len(group) <= 1:
                continue
            blocked.update(record.paper_uuid for record in group)
            result.needs_review.append(
                {
                    "issue": "duplicate_pmid_conflict",
                    "pmid": pmid,
                    "paper_uuids": [record.paper_uuid for record in group],
                }
            )

        dois: dict[str, list[CitationExportRecord]] = {}
        for record in records:
            if record.paper_uuid not in blocked and record.doi:
                dois.setdefault(record.doi, []).append(record)
        duplicates: set[str] = set()
        for doi, group in dois.items():
            if len(group) <= 1:
                continue
            signatures = {self._bibliographic_signature(record) for record in group}
            if len(signatures) > 1:
                blocked.update(record.paper_uuid for record in group)
                result.needs_review.append(
                    {
                        "issue": "duplicate_doi_metadata_conflict",
                        "doi": doi,
                        "paper_uuids": [record.paper_uuid for record in group],
                    }
                )
                continue
            keep = sorted(group, key=lambda item: (not bool(item.pmid), item.paper_uuid))[0]
            for record in group:
                if record is keep:
                    continue
                duplicates.add(record.paper_uuid)
                skips.append(
                    {
                        "paper_uuid": record.paper_uuid,
                        "reason": "duplicate_doi",
                        "canonical_paper_uuid": keep.paper_uuid,
                    }
                )
        return [
            record
            for record in records
            if record.paper_uuid not in blocked and record.paper_uuid not in duplicates
        ], skips

    def _plans(
        self,
        payloads: list[tuple[CitationExportRecord, bytes, str, str, bool]],
        *,
        format_name: str,
        paper_uuid_selector: str | None,
        output: Path | None,
    ):
        if not payloads:
            return []
        if format_name == "nbib":
            combined = b"".join(item[1] for item in payloads)
        else:
            combined = merge_pubmed_xml([item[1] for item in payloads])
        if output is not None:
            main = output if output.is_absolute() else self.settings.library_root / output
        elif paper_uuid_selector and len(payloads) == 1:
            suffix = ".nbib" if format_name == "nbib" else ".pubmed.xml"
            main = self.settings.zotero_exports_dir / "records" / f"{payloads[0][0].paper_uuid}{suffix}"
        else:
            main = self.settings.zotero_exports_dir / (
                "library.nbib" if format_name == "nbib" else "library.pubmed.xml"
            )
        plans = [
            self.writer.plan(
                main, combined, format_name=format_name, record_count=len(payloads)
            )
        ]
        if output is None and format_name == "nbib" and not paper_uuid_selector:
            for record, content, _source, _status, _hit in payloads:
                path = (
                    self.settings.zotero_exports_dir
                    / "records"
                    / f"{record.paper_uuid}.nbib"
                )
                plans.append(
                    self.writer.plan(
                        path, content, format_name="nbib", record_count=1
                    )
                )
        return plans

    @staticmethod
    def _record_report(record: CitationExportRecord) -> dict[str, Any]:
        return {
            "paper_uuid": record.paper_uuid,
            "pmid": record.pmid or None,
            "doi": record.doi or None,
            "title": record.title,
            "record_source": None if record.pmid else "lam_generated",
            "export_status": "pending",
            "skip_reason": None,
            "output_path": None,
        }

    @staticmethod
    def _bibliographic_signature(record: CitationExportRecord) -> str:
        material = "|".join(
            normalized_text(value)
            for value in (
                record.title,
                ";".join(record.authors),
                record.year,
                record.journal or record.journal_abbrev,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _terminated(content: bytes, format_name: str) -> bytes:
        if format_name == "nbib":
            return content.rstrip(b"\r\n") + b"\n\n"
        return content

    @staticmethod
    def _plan_report(plan) -> dict[str, Any]:
        return {
            "path": str(plan.path),
            "format": plan.format,
            "record_count": plan.record_count,
            "action": plan.action,
            "bytes": len(plan.content),
        }
