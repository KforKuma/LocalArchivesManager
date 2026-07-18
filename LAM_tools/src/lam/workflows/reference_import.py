from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import (
    MetadataLookupRequest,
    MetadataLookupStatus,
    MetadataRecord,
    ReferenceBatch,
    ReferenceCandidate,
    ReferenceResolution,
    WorkflowResult,
)
from ..services.catalogue_service import CatalogueService
from ..services.download_service import DownloadService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal
from ..services.metadata_service import CompositeMetadataLookupService
from ..services.reference_text_service import ReferenceTextParser
from ..services.report_service import ReportService, append_change_log
from ..utils.filename import standard_pdf_filename_result
from ..utils.hashing import full_hash
from ..utils.identifiers import normalize_arxiv_id, normalize_doi, normalize_pmid
from ..utils.normalize import normalized_text
from ..utils.text import normalize_title
from .daily_check import DailyCheckWorkflow


TERMINAL_REFERENCE_STATES = {
    "registered_new",
    "matched_existing",
    "metadata_updated",
    "duplicate_in_batch",
    "invalid_reference",
}
UNRESOLVED_REFERENCE_STATES = {
    "ambiguous",
    "not_found",
    "identifier_conflict",
    "provider_failed",
    "download_identity_mismatch",
}


class ReferenceTextImportWorkflow:
    def __init__(
        self,
        settings: Settings,
        *,
        metadata_service=None,
        download_service: DownloadService | None = None,
        parser: ReferenceTextParser | None = None,
    ):
        self.settings = settings
        self.metadata_service = metadata_service or CompositeMetadataLookupService(settings)
        self.download_service = download_service or DownloadService(settings)
        self.parser = parser or ReferenceTextParser()

    def run(
        self,
        *,
        dry_run: bool,
        mode: str,
        reference_files: tuple[str, ...] = (),
        max_references: int | None = None,
        download_missing: bool = False,
        require_download: bool = False,
        offline: bool = False,
        refresh: bool = False,
        cache_write: bool = True,
        nested: bool = False,
    ) -> WorkflowResult:
        result = WorkflowResult(
            "reference_text_import",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        sources, discovery_skips = self._discover(reference_files)
        result.skipped.extend(discovery_skips)
        catalogue = CatalogueService(self.settings.catalogue_path)
        catalogue.load()
        batches: list[tuple[Path, ReferenceBatch]] = []
        remaining = max_references
        for source in sources:
            try:
                batch = self.parser.parse_file(source)
            except OSError as exc:
                result.failures.append(
                    {
                        "file": source.name,
                        "issue": "reference_text_unreadable",
                        "detail": type(exc).__name__,
                    }
                )
                continue
            if not batch.recognized:
                result.skipped.append(
                    {
                        "file": source.name,
                        "issue": "plain_text_not_recognized_as_reference_list",
                        "detection_score": batch.detection_score,
                    }
                )
                continue
            if remaining is not None:
                batch.candidates = batch.candidates[: max(0, remaining)]
                remaining -= len(batch.candidates)
            batches.append((source, batch))
            if remaining is not None and remaining <= 0:
                break

        journal = self._journal(batches) if batches and not dry_run else None
        batch_reports: list[dict[str, Any]] = []
        seen_batch_identities: dict[str, str] = {}
        processed_moves: list[Any] = []
        all_resolutions: list[ReferenceResolution] = []

        for source, batch in batches:
            previous = self._read_receipt(batch.sha256)
            previous_by_key = {
                str(item.get("candidate_key") or ""): item
                for item in previous.get("resolutions", [])
            }
            resolutions: list[ReferenceResolution] = []
            for candidate in batch.candidates:
                operation_id = f"{batch.sha256}:{candidate.reference_index}"
                prior = previous_by_key.get(candidate.stable_key())
                if prior and prior.get("status") in TERMINAL_REFERENCE_STATES:
                    resolution = ReferenceResolution(
                        candidate.reference_index,
                        str(prior["status"]),
                        paper_uuid=str(prior.get("paper_uuid") or ""),
                        provider_status="receipt",
                    )
                    resolutions.append(resolution)
                    result.skipped.append(
                        {
                            "file": source.name,
                            "reference_index": candidate.reference_index,
                            "action": "receipt_terminal_skipped",
                        }
                    )
                    continue
                resolution, metadata, record = self._resolve_candidate(
                    catalogue,
                    candidate,
                    seen_batch_identities,
                    offline=offline,
                    refresh=refresh,
                    cache_write=cache_write,
                )
                resolutions.append(resolution)
                if journal:
                    journal.set_operation_state(
                        None,
                        "metadata_resolved",
                        operation_id=operation_id,
                        result=resolution.status,
                        paper_uuid=resolution.paper_uuid or None,
                    )
                if resolution.status in UNRESOLVED_REFERENCE_STATES:
                    result.needs_review.append(
                        {
                            "file": source.name,
                            "reference_index": candidate.reference_index,
                            "issue": resolution.issue or resolution.status,
                        }
                    )
                elif resolution.status == "invalid_reference":
                    result.skipped.append(
                        {
                            "file": source.name,
                            "reference_index": candidate.reference_index,
                            "issue": "invalid_reference",
                        }
                    )
                else:
                    result.completed.append(
                        {
                            "action": (
                                f"would_{resolution.status}" if dry_run else resolution.status
                            ),
                            "file": source.name,
                            "reference_index": candidate.reference_index,
                            "paper_uuid": resolution.paper_uuid,
                        }
                    )
                if download_missing and metadata is not None and record is not None:
                    self._download_for_record(
                        catalogue,
                        record,
                        metadata,
                        resolution,
                        result,
                        dry_run=dry_run,
                        require_download=require_download,
                        offline=offline,
                        refresh=refresh,
                        cache_write=cache_write,
                        run_id=(
                            journal.payload["run_id"] if journal else f"dry-{batch.sha256[:12]}"
                        ),
                    )
            all_resolutions.extend(resolutions)
            unresolved = any(
                item.status in UNRESOLVED_REFERENCE_STATES for item in resolutions
            )
            move = None
            if resolutions and not unresolved:
                try:
                    move = FileService(
                        self.settings.library_root,
                        self.settings.max_filename_length,
                    ).plan_reference_import_move(source)
                    if move.target.exists():
                        if full_hash(source) == full_hash(move.target):
                            move = None
                        else:
                            result.needs_review.append(
                                {
                                    "file": source.name,
                                    "issue": "reference_import_target_collision",
                                }
                            )
                except Exception as exc:
                    result.needs_review.append(
                        {
                            "file": source.name,
                            "issue": "reference_import_move_unsafe",
                            "detail": str(exc),
                        }
                    )
                    move = None
            if move is not None:
                processed_moves.append((source, batch, move, resolutions))
            batch_reports.append(
                {
                    "source_file": source.name,
                    "sha256": batch.sha256,
                    "recognized": batch.recognized,
                    "detection_score": batch.detection_score,
                    "candidate_count": len(batch.candidates),
                    "resolutions": [
                        {
                            **asdict(item),
                            "candidate_key": batch.candidates[index].stable_key(),
                        }
                        for index, item in enumerate(resolutions)
                    ],
                    "would_move_to_processed": move is not None,
                }
            )

        changed_rows = {
            ("Catalogue", item.row_number) for item in catalogue.changes
        } | {("Documents", item.row_number) for item in catalogue.document_changes}
        result.changed_rows = len(changed_rows)
        result.details = {
            "reference_text_mode": mode,
            "provider_policy": {
                "offline": offline,
                "refresh": refresh,
                "cache_write": cache_write,
            },
            "download_missing": download_missing,
            "require_download": require_download,
            "batches": batch_reports,
            "documents_created_from_text": 0,
            "operation_journal": str(journal.path) if journal else None,
        }
        result.counts = {
            "reference_files": len(batches),
            "references": sum(len(batch.candidates) for _, batch in batches),
            "registered_new": sum(item.status == "registered_new" for item in all_resolutions),
            "matched_existing": sum(item.status == "matched_existing" for item in all_resolutions),
            "metadata_updated": sum(item.status == "metadata_updated" for item in all_resolutions),
            "unresolved": sum(item.status in UNRESOLVED_REFERENCE_STATES for item in all_resolutions),
        }

        if dry_run:
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        backup = catalogue.save_atomic()
        if backup:
            result.catalogue_backup = str(backup)
            result.state_committed = True
        files = FileService(
            self.settings.library_root,
            self.settings.max_filename_length,
        )
        for source, batch, move, resolutions in processed_moves:
            try:
                files.apply_reference_import_move(move)
                result.changed_files += 1
                result.completed.append(
                    {
                        "action": "reference_batch_processed",
                        "source": source.relative_to(self.settings.library_root).as_posix(),
                        "target": move.target.relative_to(
                            self.settings.library_root
                        ).as_posix(),
                    }
                )
            except Exception as exc:
                result.failures.append(
                    {
                        "file": source.name,
                        "issue": "reference_import_move_failed",
                        "detail": str(exc),
                    }
                )
            self._write_receipt(batch, resolutions)
            result.state_committed = True
        moved_hashes = {batch.sha256 for _, batch, _, _ in processed_moves}
        for _, batch in batches:
            if batch.sha256 not in moved_hashes:
                resolutions = next(
                    report["resolutions"]
                    for report in batch_reports
                    if report["sha256"] == batch.sha256
                )
                self._write_receipt_payload(batch, resolutions)
                result.state_committed = True

        if result.changed_files or result.changed_rows:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 3 reference-text import",
                action="Resolve reference text and register canonical metadata",
                files_changed=result.changed_files,
                catalogue_rows_changed=result.changed_rows,
                reason="Explicit reference-text registration",
                uncertainty=f"{len(result.needs_review)} unresolved reference(s)",
            )
        if not nested:
            final = DailyCheckWorkflow(self.settings).run(
                dry_run=False, final_check=True
            )
            result.details["final_check"] = {
                "status": final.status.value,
                "report": final.report_path,
            }
            result.state_committed = result.state_committed or final.state_committed
            result.needs_review.extend(
                item for item in final.needs_review if item not in result.needs_review
            )
            result.failures.extend(
                item for item in final.failures if item not in result.failures
            )
        if journal:
            journal.finish("final_check_committed" if not nested else "catalogue_committed")
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _resolve_candidate(
        self,
        catalogue: CatalogueService,
        candidate: ReferenceCandidate,
        seen_batch: dict[str, str],
        *,
        offline: bool,
        refresh: bool,
        cache_write: bool,
    ) -> tuple[ReferenceResolution, MetadataRecord | None, Any | None]:
        request = self._request(candidate, offline, refresh, cache_write)
        if request is None:
            return ReferenceResolution(candidate.reference_index, "invalid_reference"), None, None
        lookup = self.metadata_service.lookup(request)
        if lookup.status != MetadataLookupStatus.FOUND or not lookup.best_record:
            status = {
                MetadataLookupStatus.AMBIGUOUS: "ambiguous",
                MetadataLookupStatus.CONFLICT: "identifier_conflict",
                MetadataLookupStatus.NOT_FOUND: "not_found",
            }.get(lookup.status, "provider_failed")
            return (
                ReferenceResolution(
                    candidate.reference_index,
                    status,
                    provider_status=lookup.status.value,
                    issue=(lookup.conflicts[0] if lookup.conflicts else status),
                ),
                None,
                None,
            )
        if lookup.confidence not in {"exact_identifier", "exact_title_supported"}:
            return (
                ReferenceResolution(
                    candidate.reference_index,
                    "ambiguous",
                    provider_status=lookup.status.value,
                    issue="reference_match_lacks_support",
                ),
                None,
                None,
            )
        metadata = MetadataRecord.from_dict(lookup.best_record)
        identity = self._identity_key(metadata, candidate)
        if identity in seen_batch:
            return (
                ReferenceResolution(
                    candidate.reference_index,
                    "duplicate_in_batch",
                    paper_uuid=seen_batch[identity],
                    provider_status=lookup.status.value,
                ),
                metadata,
                None,
            )
        matches = self._existing_matches(catalogue, metadata)
        if len(matches) > 1:
            return (
                ReferenceResolution(
                    candidate.reference_index,
                    "identifier_conflict",
                    provider_status=lookup.status.value,
                    issue="reference_matches_multiple_catalogue_rows",
                ),
                metadata,
                None,
            )
        if matches:
            record = matches[0]
            changed = self._fill_existing(catalogue, record, metadata)
            paper_uuid = str(record.get("paper_uuid") or "")
            seen_batch[identity] = paper_uuid
            return (
                ReferenceResolution(
                    candidate.reference_index,
                    "metadata_updated" if changed else "matched_existing",
                    paper_uuid=paper_uuid,
                    provider_status=lookup.status.value,
                ),
                metadata,
                record,
            )
        values = self._new_values(metadata)
        record = catalogue.add_record(values)
        paper_uuid = str(record.get("paper_uuid") or "")
        seen_batch[identity] = paper_uuid
        return (
            ReferenceResolution(
                candidate.reference_index,
                "registered_new",
                paper_uuid=paper_uuid,
                provider_status=lookup.status.value,
            ),
            metadata,
            record,
        )

    @staticmethod
    def _request(
        candidate: ReferenceCandidate,
        offline: bool,
        refresh: bool,
        cache_write: bool,
    ) -> MetadataLookupRequest | None:
        title = candidate.title_candidates[0] if candidate.title_candidates else None
        if not any(
            (
                candidate.pmid_candidates,
                candidate.doi_candidates,
                candidate.arxiv_candidates,
                title,
            )
        ):
            return None
        return MetadataLookupRequest(
            pmid=candidate.pmid_candidates[0] if candidate.pmid_candidates else None,
            doi=candidate.doi_candidates[0] if candidate.doi_candidates else None,
            arxiv_id=(
                candidate.arxiv_candidates[0] if candidate.arxiv_candidates else None
            ),
            title=title,
            authors=(
                candidate.author_candidates[0] if candidate.author_candidates else None
            ),
            year=candidate.year_candidates[0] if candidate.year_candidates else None,
            journal=(
                candidate.journal_candidates[0] if candidate.journal_candidates else None
            ),
            offline=offline,
            refresh=refresh,
            cache_write=cache_write,
            candidate_source="reference_text",
            candidate_confidence="high" if candidate.doi_candidates else "usable",
            candidate_context=candidate.normalized_text[:300],
        )

    @staticmethod
    def _identity_key(metadata: MetadataRecord, candidate: ReferenceCandidate) -> str:
        if metadata.pmid:
            return f"pmid:{normalize_pmid(metadata.pmid)}"
        if metadata.doi:
            return f"doi:{normalize_doi(metadata.doi)}"
        if metadata.arxiv_id:
            return f"arxiv:{normalize_arxiv_id(metadata.arxiv_id)}"
        author = normalized_text(metadata.authors[0] if metadata.authors else "")
        return f"title:{normalize_title(metadata.title)}|{author}|{metadata.year}"

    @staticmethod
    def _existing_matches(catalogue: CatalogueService, metadata: MetadataRecord):
        for field, value in (
            ("pmid", metadata.pmid),
            ("doi", metadata.doi),
            ("arxiv_id", metadata.arxiv_id),
        ):
            if value:
                matches = catalogue.find_by(field, value)
                if matches:
                    return matches
        title = normalize_title(metadata.title)
        first_author = normalized_text(metadata.authors[0] if metadata.authors else "")
        if not title:
            return []
        return [
            record
            for record in catalogue.records
            if normalize_title(record.get("title")) == title
            and (
                not metadata.year
                or not record.get("year")
                or str(record.get("year")) == metadata.year
            )
            and (
                not first_author
                or first_author
                in normalized_text(str(record.get("authors") or "").split(";", 1)[0])
            )
        ]

    @staticmethod
    def _canonical_source(metadata: MetadataRecord) -> str:
        for source in ("pubmed", "crossref", "arxiv", "unpaywall"):
            if source in metadata.source:
                return source
        return metadata.source[0] if metadata.source else ""

    def _new_values(self, metadata: MetadataRecord) -> dict[str, Any]:
        today = date.today().isoformat()
        values = metadata.catalogue_fields()
        values["source"] = self._canonical_source(metadata)
        values["date_added"] = today
        values["date_updated"] = today
        return {key: value for key, value in values.items() if value not in (None, "")}

    def _fill_existing(self, catalogue, record, metadata: MetadataRecord) -> list[str]:
        updates: dict[str, Any] = {}
        fields = metadata.catalogue_fields()
        fields["source"] = self._canonical_source(metadata)
        for field, value in fields.items():
            if field not in catalogue.headers or value in (None, ""):
                continue
            current = record.get(field)
            if current in (None, ""):
                updates[field] = value
            elif field == "source" and normalized_text(current) != normalized_text(value):
                updates[field] = value
        if updates:
            updates["date_updated"] = date.today().isoformat()
            return [item.field_name for item in catalogue.update_fields(record, updates)]
        return []

    def _download_for_record(
        self,
        catalogue,
        record,
        metadata: MetadataRecord,
        resolution: ReferenceResolution,
        result: WorkflowResult,
        *,
        dry_run: bool,
        require_download: bool,
        offline: bool,
        refresh: bool,
        cache_write: bool,
        run_id: str,
    ) -> None:
        existing = [
            item
            for item in catalogue.documents_for_paper(record.get("paper_uuid"))
            if normalized_text(item.get("document_type")) == "main"
        ]
        if existing:
            resolution.download_status = "document_already_registered"
            return
        if not metadata.download_candidates and metadata.doi:
            oa_lookup = self.metadata_service.lookup(
                MetadataLookupRequest(
                    doi=metadata.doi,
                    provider="unpaywall",
                    offline=offline,
                    refresh=refresh,
                    cache_write=cache_write,
                    candidate_source="reference_text_oa_enrichment",
                )
            )
            if oa_lookup.status == MetadataLookupStatus.FOUND and oa_lookup.best_record:
                oa_record = MetadataRecord.from_dict(oa_lookup.best_record)
                metadata.download_candidates.extend(oa_record.download_candidates)
        candidate = self.download_service.select_candidate(metadata.download_candidates)
        if candidate is None:
            resolution.download_status = "open_access_pdf_not_found"
            warning = {
                "paper_uuid": record.get("paper_uuid"),
                "issue": "open_access_pdf_not_found",
            }
            (result.needs_review if require_download else result.skipped).append(warning)
            return
        naming = standard_pdf_filename_result(
            title=metadata.title,
            year=metadata.year,
            journal_abbrev=metadata.journal_abbrev,
            journal=metadata.journal,
            publication_type=metadata.publication_type,
            max_length=self.settings.max_filename_length,
        )
        if not naming.filename:
            resolution.download_status = "download_filename_incomplete"
            (result.needs_review if require_download else result.skipped).append(
                {
                    "paper_uuid": record.get("paper_uuid"),
                    "issue": "download_filename_incomplete",
                }
            )
            return
        plan = self.download_service.plan(
            candidate,
            run_id=f"{run_id}-reference-download",
            final_directory=self.settings.registered_dir,
            target_filename=naming.filename,
        )
        if dry_run:
            resolution.download_status = "would_download"
            result.completed.append(
                {
                    "action": "would_download_to_registered",
                    "paper_uuid": record.get("paper_uuid"),
                    "target": f"Registered/{naming.filename}",
                }
            )
            return
        download = self.download_service.execute(plan)
        resolution.download_status = download.status
        if download.cleanup_error:
            result.failures.append(
                {
                    "paper_uuid": record.get("paper_uuid"),
                    "issue": "download_temporary_cleanup_failed",
                    "detail": download.cleanup_error,
                }
            )
        if download.status not in {"downloaded", "already_present"}:
            issue = (
                "download_identity_mismatch"
                if download.validation
                and download.validation.identity_status == "mismatch"
                else "download_failed_nonblocking"
            )
            target = result.needs_review if require_download or issue == "download_identity_mismatch" else result.skipped
            target.append(
                {
                    "paper_uuid": record.get("paper_uuid"),
                    "issue": issue,
                    "status": download.status,
                }
            )
            if issue == "download_identity_mismatch":
                resolution.status = "download_identity_mismatch"
                resolution.issue = issue
            return
        final = Path(download.final_path or plan.final_path)
        today = date.today().isoformat()
        catalogue.add_document(
            {
                "document_id": f"{record.get('paper_uuid')}:main",
                "paper_uuid": record.get("paper_uuid"),
                "document_type": "main",
                "filename": final.name,
                "relative_path": final.relative_to(
                    self.settings.library_root
                ).as_posix(),
                "extension": ".pdf",
                "sha256": download.fingerprint or full_hash(final),
                "file_status": "registered",
                "source": candidate.provider,
                "date_added": today,
                "date_updated": today,
            }
        )
        result.changed_files += int(download.status == "downloaded")

    def _discover(self, requested: tuple[str, ...]) -> tuple[list[Path], list[dict[str, Any]]]:
        if not self.settings.inbox_dir.is_dir():
            return [], []
        requested_names = {Path(value).name.casefold() for value in requested}
        found: list[Path] = []
        skipped: list[dict[str, Any]] = []
        for path in sorted(self.settings.inbox_dir.iterdir(), key=lambda item: item.name.casefold()):
            if requested_names and path.name.casefold() not in requested_names:
                continue
            if path.name.startswith((".", "~")) or path.suffix.casefold() != ".txt":
                continue
            if not path.is_file() or path.is_symlink():
                skipped.append({"file": path.name, "issue": "unsafe_reference_text"})
                continue
            found.append(path)
        for name in sorted(requested_names - {item.name.casefold() for item in found}):
            skipped.append({"file": name, "issue": "reference_file_not_found"})
        return found, skipped

    def _receipt_path(self, digest: str) -> Path:
        return (
            self.settings.state_dir
            / "imports"
            / "reference_text"
            / f"{digest}.json"
        )

    def _read_receipt(self, digest: str) -> dict[str, Any]:
        path = self._receipt_path(digest)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_receipt(
        self, batch: ReferenceBatch, resolutions: list[ReferenceResolution]
    ) -> None:
        payload = [
            {
                **asdict(item),
                "candidate_key": batch.candidates[index].stable_key(),
            }
            for index, item in enumerate(resolutions)
        ]
        self._write_receipt_payload(batch, payload)

    def _write_receipt_payload(
        self, batch: ReferenceBatch, resolutions: list[dict[str, Any]]
    ) -> None:
        path = self._receipt_path(batch.sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_file": batch.source_file,
                    "sha256": batch.sha256,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "resolutions": resolutions,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)

    def _journal(self, batches: list[tuple[Path, ReferenceBatch]]) -> OperationJournal:
        operations = []
        for _, batch in batches:
            for candidate in batch.candidates:
                operations.append(
                    {
                        "operation_id": f"{batch.sha256}:{candidate.reference_index}",
                        "operation_type": "reference_resolution",
                        "source_file": batch.source_file,
                        "reference_index": candidate.reference_index,
                        "execution_state": "planned",
                    }
                )
        return OperationJournal.create(
            self.settings.state_dir,
            operations,
            workflow="reference_text_import",
            suffix="reference-import",
        )
