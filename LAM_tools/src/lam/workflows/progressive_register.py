from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..exceptions import CatalogueError, FileOperationError
from ..models import (
    CatalogueRecord,
    FileOperation,
    InboxItemStatus,
    InspectionLevel,
    MatchStatus,
    MetadataLookupStatus,
    MetadataRecord,
    PdfInspection,
    PdfStatus,
    TitleCandidate,
    WorkflowResult,
)
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal, incomplete_journals
from ..services.matching_service import MatchingService
from ..services.pdf_service import PdfService
from ..services.report_service import ReportService, append_change_log
from ..services.record_canonicalization_service import RegisteredRecordCanonicalizer
from ..services.snapshot_service import SnapshotService
from ..services.supplementary_registration_service import (
    SupplementaryInboxItem,
    SupplementaryRegistrationService,
)
from ..utils.filename import standard_pdf_filename_result
from ..utils.hashing import full_hash
from ..utils.normalize import normalized_relative_path, normalized_text
from ..utils.text import is_probable_supplement, normalize_title
from ..utils.title_matching import (
    FilenameEvidence,
    filename_evidence,
    tolerant_title_score,
    titles_tolerantly_equivalent,
)
from ..utils.supplementary import parse_supplementary_filename
from .daily_check import DailyCheckWorkflow


PROVISIONAL_BLOCKER = (
    "PDF was recorded locally but no unique external metadata match was confirmed."
)


def should_inspect_pdf(
    *,
    identity_confirmed: bool,
    content_allowed: bool,
    pypdf_completed: bool = False,
    pypdf_sufficient: bool = False,
    ocr_allowed: bool = True,
    ocr_completed: bool = False,
) -> InspectionLevel:
    """Return the next least-expensive inspection level for one Inbox PDF."""
    if identity_confirmed or not content_allowed:
        return InspectionLevel.SKIP
    if not pypdf_completed:
        return InspectionLevel.PYPDF_TEXT
    if not pypdf_sufficient and ocr_allowed and not ocr_completed:
        return InspectionLevel.OCR
    return InspectionLevel.SKIP


class ProgressiveInboxRegisterWorkflow:
    def __init__(self, owner):
        self.owner = owner
        self.settings = owner.settings
        self.metadata_service = owner.metadata_service
        self.ocr_service = owner.ocr_service
        self.canonicalizer = RegisteredRecordCanonicalizer()

    def run(
        self,
        *,
        dry_run: bool = False,
        max_files: int | None = None,
        filename_only: bool = False,
        skip_pdf_text: bool = False,
        ocr_mode: str = "auto",
        ocr_languages: tuple[str, ...] | None = None,
        ocr_dpi: int | None = None,
        ocr_gpu: str | None = None,
    ) -> WorkflowResult:
        result = WorkflowResult(
            "inbox_register", dry_run=dry_run, mode="dry_run" if dry_run else "apply"
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        snapshots = SnapshotService(self.settings.library_root, self.settings.state_dir)
        previous_catalogue = snapshots.load_catalogue_snapshot() if snapshots.initialized else {}
        catalogue.configure_review_state(previous_catalogue)
        for journal in incomplete_journals(self.settings.state_dir):
            result.needs_review.append({**journal, "issue": "catalogue_write_incomplete"})

        files = FileService(self.settings.library_root, self.settings.max_filename_length)
        matcher = MatchingService()
        pdfs = PdfService(self.settings, self.ocr_service)
        run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f-register")
        supplementary_inputs: list[dict[str, Any]] = []
        if catalogue.has_documents_sheet:
            all_documents, skipped = self.owner._discover_inbox_documents()
            discovered = []
            for path in all_documents:
                parsed = parse_supplementary_filename(path.name)
                if parsed is not None:
                    supplementary_inputs.append({"source": path, "parsed": parsed})
                elif path.suffix.casefold() == ".pdf":
                    discovered.append(path)
                else:
                    result.needs_review.append(
                        {
                            "file": path.relative_to(
                                self.settings.library_root
                            ).as_posix(),
                            "issue": "supplementary_binding_ambiguous",
                        }
                    )
            main_stems = {path.stem.casefold() for path in discovered}
            for item in supplementary_inputs:
                parsed = item["parsed"]
                if parsed.binding == "same_stem" and (
                    not parsed.parent_stem
                    or parsed.parent_stem.casefold() not in main_stems
                ):
                    item["orphan_issue"] = "supplementary_binding_ambiguous"
        else:
            discovered, skipped = self.owner._discover_inbox()
        result.skipped.extend(skipped)
        if max_files is not None:
            discovered = discovered[:max_files]

        planned: list[dict[str, Any]] = []
        file_results: list[dict[str, Any]] = []
        lookup_count = 0
        ocr_files = 0
        provisional_created = 0
        provisional_updated = 0
        supplementary_planned: list[dict[str, Any]] = []
        supplementary_results: list[dict[str, Any]] = []

        if catalogue.has_documents_sheet:
            for item in supplementary_inputs:
                parsed = item["parsed"]
                if parsed.binding != "paper_uuid":
                    continue
                planned_item, file_result = self._plan_supplementary(
                    catalogue,
                    files,
                    item["source"],
                    parsed,
                    paper_uuid=parsed.paper_uuid or "",
                    reserved=supplementary_planned,
                )
                supplementary_results.append(file_result)
                if planned_item is not None:
                    supplementary_planned.append(planned_item)
                else:
                    result.needs_review.append(
                        {
                            "file": file_result["source_path"],
                            "issue": file_result["issue_keys"][0],
                            "details": file_result.get("details"),
                        }
                    )

        for source in discovered:
            relative = source.relative_to(self.settings.library_root).as_posix()
            evidence = filename_evidence(source.name)
            inspection = self._filename_inspection(source, evidence)
            file_result = self._new_file_result(relative, evidence)
            file_results.append(file_result)
            source_sha256 = ""
            if catalogue.has_documents_sheet:
                try:
                    source_sha256 = full_hash(source)
                except OSError as exc:
                    file_result["issue_keys"].append("source_changed_during_run")
                    file_result["details"] = str(exc)
                    result.needs_review.append(
                        {
                            "file": relative,
                            "issue": "source_changed_during_run",
                            "details": str(exc),
                        }
                    )
                    continue
                duplicates = catalogue.find_documents_by("sha256", source_sha256)
                if duplicates:
                    existing_document = duplicates[0]
                    file_result["issue_keys"].append("duplicate_file_exact")
                    file_result["details"] = {
                        "existing_document_id": existing_document.get("document_id"),
                        "existing_path": existing_document.get("relative_path"),
                    }
                    result.needs_review.append(
                        {
                            "file": relative,
                            "issue": "duplicate_file_exact",
                            **file_result["details"],
                        }
                    )
                    continue
            existing = self._existing_local_record(records, relative, source.name, source)
            formal_records = [row for row in records if not self._is_provisional(row)]
            initial_match = matcher.match(
                formal_records,
                relative_path=relative,
                filename=source.name,
                inspection=inspection,
            )
            record = self.owner._record_for_match(formal_records, initial_match.matched_row_id)
            identity_confirmed = initial_match.status in {
                MatchStatus.EXACT,
                MatchStatus.HIGH_CONFIDENCE,
            }
            if existing is not None and self._is_provisional(existing):
                record = existing
                identity_confirmed = False
                file_result["provisional_updated"] = True
                provisional_updated += 1
            elif existing is not None and record is None:
                record = existing

            file_result["catalogue_id"] = str(record.get("id") or "") if record else None
            self._set_match_report(file_result, initial_match)

            attempted_signatures: set[tuple[Any, ...]] = set()
            last_lookup_issue = "metadata_identity_unconfirmed"
            last_lookup_detail = PROVISIONAL_BLOCKER
            hard_issue: tuple[str, str] | None = None

            if not identity_confirmed and self._lookup_worthy(inspection, record):
                outcome = self._lookup_and_apply(
                    catalogue,
                    records,
                    record,
                    inspection,
                    file_result,
                    attempted_signatures,
                )
                record, identity_confirmed, issue_key, issue, attempts = outcome
                lookup_count += attempts
                last_lookup_issue, last_lookup_detail = issue_key, issue
                if identity_confirmed:
                    provisional_updated += int(self._is_provisional(record))

            content_allowed = not (filename_only or skip_pdf_text)
            decision = should_inspect_pdf(
                identity_confirmed=identity_confirmed,
                content_allowed=content_allowed,
                ocr_allowed=ocr_mode != "never",
            )
            if decision == InspectionLevel.PYPDF_TEXT:
                try:
                    inspection = pdfs.inspect(
                        source,
                        extract_text=True,
                        ocr_mode="never",
                        ocr_languages=ocr_languages,
                        ocr_dpi=ocr_dpi,
                        ocr_gpu=ocr_gpu,
                        run_id=run_id,
                        ocr_cache_write=not dry_run,
                    )
                    file_result["inspection_level_used"] = InspectionLevel.PYPDF_TEXT.value
                    self._set_inspection_report(file_result, inspection)
                except (OSError, ValueError) as exc:
                    hard_issue = (
                        "source_changed_during_run",
                        f"Inbox PDF became unavailable before inspection: {exc}",
                    )
                if hard_issue is None and not self._same_source(source, inspection):
                    hard_issue = (
                        "source_changed_during_run",
                        "Inbox PDF changed while it was being inspected.",
                    )
                if hard_issue is None and not inspection.is_readable:
                    key = "pdf_encrypted" if "pdf_encrypted" in inspection.errors else "pdf_unreadable"
                    hard_issue = (key, "PDF cannot be read safely.")

                if hard_issue is None:
                    formal_records = [row for row in records if not self._is_provisional(row)]
                    local_match = matcher.match(
                        formal_records,
                        relative_path=relative,
                        filename=source.name,
                        inspection=inspection,
                    )
                    self._set_match_report(file_result, local_match)
                    matched = self.owner._record_for_match(
                        formal_records, local_match.matched_row_id
                    )
                    if local_match.status in {MatchStatus.EXACT, MatchStatus.HIGH_CONFIDENCE}:
                        if self._is_provisional(record) and matched is not None:
                            hard_issue = (
                                "possible_duplicate_provisional_record",
                                "Local PDF evidence also points to an existing formal catalogue row; rows were not merged automatically.",
                            )
                        else:
                            record = matched or record
                            identity_confirmed = True
                    elif local_match.status in {MatchStatus.AMBIGUOUS, MatchStatus.CONFLICT}:
                        hard_issue = (
                            local_match.issue_key or "paper_identity_ambiguous",
                            "; ".join(local_match.conflicts) or "Paper identity requires review.",
                        )

                if hard_issue is None and not identity_confirmed and self._lookup_worthy(inspection, record):
                    outcome = self._lookup_and_apply(
                        catalogue,
                        records,
                        record,
                        inspection,
                        file_result,
                        attempted_signatures,
                    )
                    record, identity_confirmed, issue_key, issue, attempts = outcome
                    lookup_count += attempts
                    last_lookup_issue, last_lookup_detail = issue_key, issue

                if hard_issue is None and not identity_confirmed and self._is_provisional(record):
                    local_confirmed, local_issue, local_detail = self._apply_local_fallback(
                        catalogue, record, inspection, file_result
                    )
                    if local_confirmed:
                        identity_confirmed = True
                    elif local_issue:
                        last_lookup_issue, last_lookup_detail = local_issue, local_detail

                pypdf_sufficient = (
                    False if ocr_mode == "always" else self._pypdf_sufficient(inspection)
                )
                decision = should_inspect_pdf(
                    identity_confirmed=identity_confirmed,
                    content_allowed=content_allowed,
                    pypdf_completed=True,
                    pypdf_sufficient=pypdf_sufficient,
                    ocr_allowed=(
                        ocr_mode != "never"
                        and ocr_files < self.settings.ocr.max_files_per_run
                    ),
                )
                if hard_issue is None and decision == InspectionLevel.OCR:
                    try:
                        inspection = pdfs.inspect(
                            source,
                            extract_text=True,
                            ocr_mode="always",
                            ocr_languages=ocr_languages,
                            ocr_dpi=ocr_dpi,
                            ocr_gpu=ocr_gpu,
                            run_id=run_id,
                            ocr_cache_write=not dry_run,
                        )
                        file_result["inspection_level_used"] = InspectionLevel.OCR.value
                        self._set_inspection_report(file_result, inspection)
                        if inspection.ocr_result is not None:
                            ocr_files += 1
                    except (OSError, ValueError) as exc:
                        hard_issue = ("ocr_failed", str(exc))
                    if hard_issue is None and "pdf_text_ocr_conflict" in inspection.warnings:
                        hard_issue = (
                            "pdf_text_ocr_conflict",
                            "The embedded PDF text and first-page OCR point to conflicting evidence.",
                        )
                    if hard_issue is None:
                        ocr = inspection.ocr_result
                        if ocr is None or ocr.status != "success":
                            hard_issue = (
                                ocr.status if ocr is not None else "ocr_unavailable",
                                "First-page OCR did not produce usable identification evidence.",
                            )
                    if hard_issue is None:
                        formal_records = [
                            row for row in records if not self._is_provisional(row)
                        ]
                        ocr_match = matcher.match(
                            formal_records,
                            relative_path=relative,
                            filename=source.name,
                            inspection=inspection,
                        )
                        if self.owner._unsupported_ocr_title_only_match(
                            ocr_match, inspection, formal_records
                        ):
                            ocr_match.status = MatchStatus.NOT_FOUND
                            ocr_match.requires_metadata_lookup = True
                            ocr_match.issue_key = "ocr_title_requires_support"
                        self._set_match_report(file_result, ocr_match)
                        matched = self.owner._record_for_match(
                            formal_records, ocr_match.matched_row_id
                        )
                        if ocr_match.status in {
                            MatchStatus.EXACT,
                            MatchStatus.HIGH_CONFIDENCE,
                        }:
                            if self._is_provisional(record) and matched is not None:
                                hard_issue = (
                                    "possible_duplicate_provisional_record",
                                    "OCR evidence also points to an existing formal catalogue row; rows were not merged automatically.",
                                )
                            else:
                                record = matched or record
                                identity_confirmed = True
                        elif ocr_match.status in {
                            MatchStatus.AMBIGUOUS,
                            MatchStatus.CONFLICT,
                        }:
                            hard_issue = (
                                ocr_match.issue_key or "paper_identity_ambiguous",
                                "; ".join(ocr_match.conflicts)
                                or "OCR evidence leaves multiple paper identities plausible.",
                            )
                    if (
                        hard_issue is None
                        and not identity_confirmed
                        and self._lookup_worthy(inspection, record)
                    ):
                        outcome = self._lookup_and_apply(
                            catalogue,
                            records,
                            record,
                            inspection,
                            file_result,
                            attempted_signatures,
                        )
                        record, identity_confirmed, issue_key, issue, attempts = outcome
                        lookup_count += attempts
                        last_lookup_issue, last_lookup_detail = issue_key, issue

                    if hard_issue is None and not identity_confirmed and self._is_provisional(record):
                        local_confirmed, local_issue, local_detail = self._apply_local_fallback(
                            catalogue, record, inspection, file_result
                        )
                        if local_confirmed:
                            identity_confirmed = True
                        elif local_issue:
                            last_lookup_issue, last_lookup_detail = local_issue, local_detail

            file_result["catalogue_id"] = str(record.get("id") or "") if record else None
            if identity_confirmed and hard_issue is None:
                catalogue.resolve_confirmed_reviews(record)
                active = catalogue.active_review_lines(record)
                if active:
                    hard_issue = (
                        "active_catalogue_review",
                        "Catalogue row has an unresolved NEEDS_REVIEW blocker.",
                    )

            if not identity_confirmed or hard_issue is not None:
                if record is None:
                    record = self._create_provisional(
                        catalogue, source.name, relative, evidence, inspection
                    )
                    provisional_created += 1
                    file_result["provisional_created"] = True
                    file_result["catalogue_id"] = str(record.get("id") or "")
                issue_key, issue = hard_issue or (last_lookup_issue, last_lookup_detail)
                self._retain_provisional(
                    catalogue,
                    record,
                    result,
                    file_result,
                    issue_key,
                    issue,
                    relative,
                    blocked=hard_issue is not None,
                )
                continue

            if inspection.is_probable_supplement:
                self._retain_provisional(
                    catalogue,
                    record,
                    result,
                    file_result,
                    "supplement_parent_unknown",
                    "Supplementary material cannot use the single main-PDF catalogue fields safely.",
                    relative,
                    blocked=True,
                )
                continue
            if catalogue.has_documents_sheet:
                paper_uuid = catalogue.ensure_paper_uuid(record)
                main_documents = [
                    item
                    for item in catalogue.documents_for_paper(paper_uuid)
                    if normalized_text(item.get("document_type")) == "main"
                ]
                current_path = (
                    normalized_relative_path(main_documents[0].get("relative_path"))
                    if len(main_documents) == 1
                    else ""
                )
            else:
                current_path = normalized_relative_path(record.get("pdf_relative_path"))
            if current_path and current_path != normalized_relative_path(relative):
                if catalogue.has_documents_sheet:
                    file_result["issue_keys"].append("duplicate_paper_confirmed")
                    file_result["details"] = {
                        "existing_record": record.get("paper_uuid"),
                        "existing_path": current_path,
                    }
                    result.needs_review.append(
                        {
                            "file": relative,
                            "issue": "duplicate_paper_confirmed",
                            **file_result["details"],
                        }
                    )
                else:
                    self._retain_provisional(
                        catalogue,
                        record,
                        result,
                        file_result,
                        "multiple_local_files_for_single_row",
                        "Catalogue row already points to another local PDF.",
                        relative,
                        blocked=True,
                    )
                continue

            confirmed_title = self.owner._confirmed_value(record, "title")
            confirmed_year = self.owner._confirmed_value(record, "publication_year")
            confirmed_journal = self.owner._confirmed_value(record, "journal")
            naming_title = record.get("title") or confirmed_title
            naming_year = record.get("year") or confirmed_year
            naming_journal = record.get("journal") or confirmed_journal
            filename_result = standard_pdf_filename_result(
                title=naming_title,
                year=naming_year,
                journal_abbrev=record.get("journal_abbrev"),
                journal=naming_journal,
                publication_type=record.get("publication_type"),
                max_length=self.settings.max_filename_length,
            )
            target_filename = filename_result.filename
            if not target_filename:
                self._retain_provisional(
                    catalogue,
                    record,
                    result,
                    file_result,
                    "required_naming_metadata_missing",
                    "Registration requires title, year, and journal or journal_abbrev.",
                    relative,
                    blocked=True,
                )
                continue
            if catalogue.has_documents_sheet:
                catalogue.ensure_paper_uuid(record)
            else:
                catalogue.ensure_record_uid(record)
            catalogue.repair_publication_type(record, record.get("publication_type"))
            publication_warnings = tuple(
                dict.fromkeys(
                    (
                        *filename_result.warnings,
                        *file_result.get("publication_type_warnings", []),
                    )
                )
            )
            for warning in publication_warnings:
                if warning not in {
                    "publication_type_conflict",
                    "publication_type_unrecognized",
                }:
                    continue
                if warning not in file_result["issue_keys"]:
                    file_result["issue_keys"].append(warning)
                issue = (
                    "Multiple equally ranked publication genres conflict."
                    if warning == "publication_type_conflict"
                    else "Publication type contains an unrecognized provider value."
                )
                catalogue.ensure_review_blocker(
                    record,
                    "publication_type",
                    issue,
                    issue_key=warning,
                )
                review_item = {
                    "file": relative,
                    "row": record.row_number,
                    "issue": warning,
                    "details": issue,
                }
                if review_item not in result.needs_review:
                    result.needs_review.append(review_item)
            try:
                operation = files.plan_registration_move(
                    source,
                    target_filename,
                    record.row_number,
                    "unique high-confidence paper identity",
                )
            except FileOperationError as exc:
                self._retain_provisional(
                    catalogue,
                    record,
                    result,
                    file_result,
                    "source_changed_during_run",
                    str(exc),
                    relative,
                    blocked=True,
                )
                continue
            target_relative = operation.target.relative_to(
                self.settings.library_root
            ).as_posix()
            updates = (
                {}
                if catalogue.has_documents_sheet
                else {
                    "pdf_status": PdfStatus.REGISTERED.value,
                    "pdf_filename": target_filename,
                    "pdf_relative_path": target_relative,
                }
            )
            if not record.get("title") and confirmed_title:
                updates["title"] = confirmed_title
            if not record.get("year") and confirmed_year:
                updates["year"] = confirmed_year
            if not record.get("journal") and confirmed_journal:
                updates["journal"] = confirmed_journal
            file_result.update(
                {
                    "target_filename": target_filename,
                    "target_path": target_relative,
                    "canonical_publication_type": filename_result.publication_type,
                    "title_truncated": filename_result.title_truncated,
                    "action": "planned",
                    "result_status": InboxItemStatus.REGISTERED.value,
                    "selected_title": str(record.get("title") or ""),
                }
            )
            planned.append(
                {
                    "source": source,
                    "record": record,
                    "operation": operation,
                    "updates": updates,
                    "sha256": source_sha256,
                    "file_result": file_result,
                }
            )

        problems = files.validate_plan([item["operation"] for item in planned])
        blocked_rows = {int(problem["row"]) for problem in problems}
        for problem in problems:
            item = next(
                entry for entry in planned
                if entry["record"].row_number == int(problem["row"])
            )
            self._retain_provisional(
                catalogue,
                item["record"],
                result,
                item["file_result"],
                "registered_filename_collision",
                f"Registration collision: {problem['issue']} at {problem['target']}",
                item["file_result"]["source_path"],
                blocked=True,
            )
        ready = [item for item in planned if item["record"].row_number not in blocked_rows]

        if catalogue.has_documents_sheet:
            ready_by_stem = {
                item["source"].stem.casefold(): item for item in ready
            }
            for item in supplementary_inputs:
                parsed = item["parsed"]
                if parsed.binding != "same_stem":
                    continue
                relative = item["source"].relative_to(
                    self.settings.library_root
                ).as_posix()
                parent = (
                    ready_by_stem.get(parsed.parent_stem.casefold())
                    if parsed.parent_stem
                    else None
                )
                if item.get("orphan_issue"):
                    issue = str(item["orphan_issue"])
                    result.needs_review.append({"file": relative, "issue": issue})
                    supplementary_results.append(
                        {
                            "source_path": relative,
                            "result_status": InboxItemStatus.BLOCKED.value,
                            "issue_keys": [issue],
                        }
                    )
                    continue
                if parent is None:
                    issue = "supplementary_parent_registration_failed"
                    result.needs_review.append({"file": relative, "issue": issue})
                    supplementary_results.append(
                        {
                            "source_path": relative,
                            "result_status": InboxItemStatus.BLOCKED.value,
                            "issue_keys": [issue],
                        }
                    )
                    continue
                paper_uuid = catalogue.ensure_paper_uuid(parent["record"])
                planned_item, file_result = self._plan_supplementary(
                    catalogue,
                    files,
                    item["source"],
                    parsed,
                    paper_uuid=paper_uuid,
                    reserved=supplementary_planned,
                )
                supplementary_results.append(file_result)
                if planned_item is not None:
                    supplementary_planned.append(planned_item)
                else:
                    result.needs_review.append(
                        {
                            "file": file_result["source_path"],
                            "issue": file_result["issue_keys"][0],
                            "details": file_result.get("details"),
                        }
                    )

        result.details.update(
            {
                "files": [*file_results, *supplementary_results],
                "supplementary_ready": len(supplementary_planned),
                "metadata_lookup_requests": lookup_count,
                "provider_lookup_attempts": lookup_count,
                "provisional_created": provisional_created,
                "provisional_updated": provisional_updated,
                "manual_checkpoint_required": False,
            }
        )
        self._set_counts(
            result, discovered, file_results, ready, lookup_count, ocr_files,
            provisional_created, provisional_updated,
        )
        result.counts["supplementary_ready"] = len(supplementary_planned)
        if dry_run:
            for item in ready:
                result.completed.append(
                    {
                        "action": "would_register",
                        "row": item["record"].row_number,
                        "source": item["file_result"]["source_path"],
                        "target": item["file_result"]["target_path"],
                    }
                )
            for item in supplementary_planned:
                result.completed.append(
                    {
                        "action": "would_register_supplementary",
                        "document_id": item["document_values"]["document_id"],
                        "source": item["file_result"]["source_path"],
                        "target": item["file_result"]["target_path"],
                    }
                )
            result.changed_rows = len({change.row_number for change in catalogue.changes})
            result.details["remaining_in_inbox"] = [
                path.relative_to(self.settings.library_root).as_posix()
                for path in (
                    self.owner._eligible_document_files()
                    if catalogue.has_documents_sheet
                    else self.owner._eligible_pdf_files()
                )
            ]
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = (
            self._create_journal(ready, supplementary_planned)
            if ready or supplementary_planned
            else None
        )
        moved: list[dict[str, Any]] = []
        today = date.today().isoformat()
        for item in ready:
            operation: FileOperation = item["operation"]
            record: CatalogueRecord = item["record"]
            try:
                files.apply_registration_move(operation)
                moved.append(item)
                updates = dict(item["updates"])
                if "date_updated" in catalogue.headers:
                    updates["date_updated"] = today
                if catalogue.has_documents_sheet:
                    paper_uuid = catalogue.ensure_paper_uuid(record)
                    document_id = f"{paper_uuid}:main"
                    catalogue.add_document(
                        {
                            "document_id": document_id,
                            "paper_uuid": paper_uuid,
                            "document_type": "main",
                            "filename": operation.target.name,
                            "relative_path": operation.target.relative_to(
                                self.settings.library_root
                            ).as_posix(),
                            "extension": operation.target.suffix,
                            "sha256": item.get("sha256") or full_hash(operation.target),
                            "file_status": PdfStatus.REGISTERED.value,
                            "source": str(record.get("source") or ""),
                            "date_added": str(record.get("date_added") or today),
                            "date_updated": today,
                        }
                    )
                    item["document_id"] = document_id
                    if updates:
                        catalogue.update_fields(record, updates)
                else:
                    catalogue.update_fields(record, updates)
                item["file_result"]["action"] = "registered"
                item["file_result"]["result_status"] = InboxItemStatus.REGISTERED.value
                result.completed.append(
                    {
                        "action": "registered",
                        "row": record.row_number,
                        "source": item["file_result"]["source_path"],
                        "target": item["file_result"]["target_path"],
                    }
                )
                if journal:
                    if catalogue.has_documents_sheet:
                        journal.set_operation_state(
                            record.row_number,
                            "file_moved",
                            document_id=item.get("document_id"),
                        )
                    else:
                        journal.set_operation_state(
                            record.row_number,
                            "file_moved",
                            record_uid=str(record.get("record_uid") or "") or None,
                        )
            except (FileOperationError, CatalogueError) as exc:
                self._retain_provisional(
                    catalogue,
                    record,
                    result,
                    item["file_result"],
                    "target_appeared_during_run" if operation.target.exists() else "source_changed_during_run",
                    str(exc),
                    item["file_result"]["source_path"],
                    blocked=True,
                )
                if journal:
                    journal.set_operation_state(
                        record.row_number,
                        "failed",
                        document_id=(
                            f"{record.get('paper_uuid')}:main"
                            if catalogue.has_documents_sheet
                            else None
                        ),
                        record_uid=(
                            None
                            if catalogue.has_documents_sheet
                            else str(record.get("record_uid") or "") or None
                        ),
                        error=str(exc),
                    )

        supplementary_moved: list[dict[str, Any]] = []
        for item in supplementary_planned:
            operation = item["operation"]
            try:
                files.apply_document_registration_move(operation)
                catalogue.add_document(item["document_values"])
                supplementary_moved.append(item)
                item["file_result"]["action"] = "registered_supplementary"
                item["file_result"]["result_status"] = InboxItemStatus.REGISTERED.value
                result.completed.append(
                    {
                        "action": "registered_supplementary",
                        "document_id": item["document_values"]["document_id"],
                        "source": item["file_result"]["source_path"],
                        "target": item["file_result"]["target_path"],
                    }
                )
                if journal:
                    journal.set_operation_state(
                        item["catalogue_row"],
                        "file_moved",
                        document_id=item["document_values"]["document_id"],
                    )
            except (FileOperationError, CatalogueError) as exc:
                item["file_result"]["issue_keys"].append(
                    "supplementary_target_collision"
                    if operation.target.exists()
                    else "source_changed_during_run"
                )
                result.needs_review.append(
                    {
                        "file": item["file_result"]["source_path"],
                        "issue": item["file_result"]["issue_keys"][-1],
                        "details": str(exc),
                    }
                )
                if journal:
                    journal.set_operation_state(
                        item["catalogue_row"],
                        "failed",
                        document_id=item["document_values"]["document_id"],
                        error=str(exc),
                    )

        backup = catalogue.save_atomic()
        if backup:
            result.catalogue_backup = str(backup)
        if catalogue.maintenance_actions:
            result.details["backup_maintenance"] = list(
                catalogue.maintenance_actions
            )
        if journal:
            for item in moved:
                if catalogue.has_documents_sheet:
                    journal.set_operation_state(
                        item["record"].row_number,
                        "catalogue_committed",
                        document_id=item.get("document_id"),
                    )
                else:
                    journal.set_operation_state(
                        item["record"].row_number,
                        "catalogue_committed",
                        record_uid=str(item["record"].get("record_uid") or "") or None,
                    )
            for item in supplementary_moved:
                journal.set_operation_state(
                    item["catalogue_row"],
                    "catalogue_committed",
                    document_id=item["document_values"]["document_id"],
                )
        self.owner._commit_file_blockers([*file_results, *supplementary_results])
        result.changed_files = len(moved) + len(supplementary_moved)
        result.changed_rows = len(
            {("Catalogue", change.row_number) for change in catalogue.changes}
            | {("Documents", change.row_number) for change in catalogue.document_changes}
        )
        if moved or supplementary_moved or catalogue.changes or catalogue.document_changes:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Workflow 3",
                action="Progressive Inbox identification and registration",
                files_changed=len(moved) + len(supplementary_moved),
                catalogue_rows_changed=result.changed_rows,
                reason="Registered confirmed PDFs and maintained provisional Inbox records",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        final_check = DailyCheckWorkflow(self.settings).run(dry_run=False, final_check=True)
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
        result.details["manual_checkpoint_required"] = bool(moved or catalogue.changes)
        result.details["manual_checkpoint"] = (
            "Please review catalogue.xlsx before running Workflow 4."
            if moved or catalogue.changes else None
        )
        result.details["remaining_in_inbox"] = [
            path.relative_to(self.settings.library_root).as_posix()
                for path in (
                    self.owner._eligible_document_files()
                    if catalogue.has_documents_sheet
                    else self.owner._eligible_pdf_files()
                )
        ]
        self._set_counts(
            result, discovered, file_results, ready, lookup_count, ocr_files,
            provisional_created, provisional_updated,
        )
        result.counts["supplementary_registered"] = len(supplementary_moved)
        result.counts["supplementary_blocked"] = sum(
            item.get("result_status") == InboxItemStatus.BLOCKED.value
            for item in supplementary_results
        )
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _lookup_and_apply(
        self,
        catalogue: CatalogueService,
        records: list[CatalogueRecord],
        record: CatalogueRecord | None,
        inspection: PdfInspection,
        file_result: dict[str, Any],
        attempted_signatures: set[tuple[Any, ...]],
    ) -> tuple[CatalogueRecord | None, bool, str, str, int]:
        attempts_made = 0
        lookup = None
        request = None
        last_key = "metadata_identity_unconfirmed"
        last_detail = PROVISIONAL_BLOCKER
        for candidate_request in self.owner._lookup_requests(
            record, inspection, file_result["source_path"]
        ):
            signature = (
                candidate_request.doi,
                candidate_request.pmid,
                candidate_request.arxiv_id,
                normalize_title(candidate_request.title),
            )
            if signature in attempted_signatures:
                continue
            attempted_signatures.add(signature)
            request = candidate_request
            lookup = self.metadata_service.lookup(request)
            attempts_made += 1
            attempt = {
                "candidate_source": request.candidate_source,
                "query_type": (
                    "pmid" if request.pmid else "doi" if request.doi else "arxiv_id" if request.arxiv_id else "title"
                ),
                "query_value": request.pmid or request.doi or request.arxiv_id or request.title,
                "status": lookup.status.value,
                "confidence": lookup.confidence,
                "providers": lookup.providers_used,
                "selection_reason": lookup.selection_reason,
            }
            file_result["provider_lookup_attempts"].append(attempt)
            file_result["metadata_lookup_status"] = lookup.status.value
            file_result["metadata_providers"] = lookup.providers_used
            file_result["metadata_selection_reason"] = lookup.selection_reason
            last_key = {
                MetadataLookupStatus.NOT_FOUND: "metadata_query_not_found",
                MetadataLookupStatus.AMBIGUOUS: "metadata_query_ambiguous",
                MetadataLookupStatus.CONFLICT: "metadata_identifier_conflict",
                MetadataLookupStatus.UNAVAILABLE: "metadata_provider_unavailable",
                MetadataLookupStatus.FAILED: "metadata_provider_failed",
            }.get(lookup.status, "metadata_query_ambiguous")
            last_detail = (
                lookup.selection_reason or "; ".join(lookup.errors) or PROVISIONAL_BLOCKER
            )
            if lookup.status == MetadataLookupStatus.CONFLICT:
                return record, False, last_key, last_detail, attempts_made
            if (
                lookup.status == MetadataLookupStatus.FOUND
                and lookup.best_record
                and lookup.confidence in {"exact_identifier", "exact_title_supported"}
            ):
                break
            lookup = None
            request = None
        if lookup is None or request is None:
            return record, False, last_key, last_detail, attempts_made

        metadata = MetadataRecord.from_dict(lookup.best_record)
        metadata_type = metadata.publication_type_result()
        file_result["raw_publication_types"] = list(metadata_type.raw_types)
        file_result["publication_type_warnings"] = list(metadata_type.warnings)
        file_result["provider_candidate"] = self._candidate_dict(
            TitleCandidate(
                metadata.title,
                "high",
                "provider",
                None,
                "; ".join(lookup.providers_used),
            ) if metadata.title else None
        )
        if not self.owner._metadata_supports_inspection(metadata, inspection):
            return (
                record,
                False,
                "metadata_identifier_conflict",
                "Provider metadata conflicts with reliable local PDF identifiers.",
                attempts_made,
            )
        formal_records = [row for row in records if not self._is_provisional(row)]
        candidates = self.owner._metadata_catalogue_candidates(formal_records, metadata)
        if self._is_provisional(record) and candidates:
            return (
                record,
                False,
                "possible_duplicate_provisional_record",
                "Provider metadata points to an existing formal row; automatic merge was refused.",
                attempts_made,
            )
        if len(candidates) > 1:
            return (
                record,
                False,
                "metadata_query_ambiguous",
                "Provider metadata matches multiple catalogue rows.",
                attempts_made,
            )
        merged, field_sources, identity_conflicts = self.owner.build_merged_identity_evidence(
            record,
            metadata,
            inspection,
            lookup_confidence=lookup.confidence,
        )
        durable, durable_reason = self.owner.validate_durable_identity(
            merged,
            sources=field_sources,
            conflicts=identity_conflicts,
            user_confirmed_identity=request.user_confirmed_identity,
            lookup_confidence=lookup.confidence,
            inspection=inspection,
        )
        file_result["final_field_sources"] = field_sources
        file_result["durable_identity_reason"] = durable_reason
        if not durable:
            return (
                record,
                False,
                durable_reason,
                (
                    "Provider, catalogue, and local PDF evidence contain conflicting identifiers."
                    if identity_conflicts
                    else "Merged provider, catalogue, and local PDF evidence is incomplete."
                ),
                attempts_made,
            )

        canonical_conflicts = self.canonicalizer.conflicts(record, metadata)
        if canonical_conflicts:
            issue_key = (
                "metadata_journal_conflict"
                if "metadata_journal_conflict" in canonical_conflicts
                else canonical_conflicts[0]
            )
            return (
                record,
                False,
                issue_key,
                "; ".join(canonical_conflicts),
                attempts_made,
            )

        if self._is_provisional(record):
            assert record is not None
            confirmed_title = self.owner._confirmed_value(record, "title")
            if (
                confirmed_title and metadata.title
                and not titles_tolerantly_equivalent(confirmed_title, metadata.title)
            ):
                return (
                    record,
                    False,
                    "metadata_title_conflict",
                    "Provider title conflicts with USER_CONFIRMED title.",
                    attempts_made,
                )
            current_journal = str(record.get("journal") or "")
            if current_journal and metadata.journal:
                journal_equivalent = self.owner._journal_names_equivalent(
                    current_journal, metadata.journal
                ) or self.owner._journal_names_equivalent(
                    current_journal, metadata.journal_abbrev
                )
                if not journal_equivalent:
                    return (
                        record,
                        False,
                        "metadata_journal_conflict",
                        f"Catalogue journal {current_journal!r} differs from provider journal {metadata.journal!r}.",
                        attempts_made,
                    )
                if self.owner._journal_is_variant(current_journal, metadata.journal):
                    catalogue.add_uncertainty(
                        record,
                        "MACHINE_NOTE:",
                        "journal",
                        f"Provider journal name {metadata.journal!r} is a formatting variant of {current_journal!r}.",
                        issue_key="journal_name_variant",
                    )
            previous_title = str(record.get("title") or "")
            previous_source = file_result.get("selected_title_source") or "local"
            updates: dict[str, Any] = {}
            for key, value in merged.catalogue_fields().items():
                if key not in catalogue.headers or value in (None, ""):
                    continue
                current = record.get(key)
                if current in (None, ""):
                    updates[key] = value
                elif (
                    key == "title"
                    and metadata.title
                    and not confirmed_title
                ):
                    updates[key] = metadata.title
            current_source = str(record.get("source") or "")
            merged_sources = [
                item
                for item in [*re.split(r"\s*;\s*", current_source), *metadata.source]
                if item
            ]
            if "source" in catalogue.headers and merged_sources:
                updates["source"] = "; ".join(dict.fromkeys(merged_sources))
            updates["uncertainty"] = self._resolved_provisional_uncertainty(record)
            if "date_updated" in catalogue.headers:
                updates["date_updated"] = date.today().isoformat()
            catalogue.update_provisional_fields(record, updates)
            file_result.update(
                {
                    "previous_title": previous_title,
                    "new_canonical_title": metadata.title,
                    "previous_title_source": previous_source,
                    "selected_provider": "; ".join(lookup.providers_used),
                    "selection_reason": lookup.selection_reason,
                    "merged_identity": merged.catalogue_fields(),
                }
            )
        else:
            if record is None:
                if candidates:
                    record = candidates[0]
                else:
                    record = catalogue.add_record(
                        self.owner._metadata_row_values(
                            catalogue,
                            metadata,
                            Path(file_result["source_path"]).name,
                            file_result["source_path"],
                        )
                    )
            target = candidates[0] if candidates else record
            if target.row_number != record.row_number:
                return (
                    record,
                    False,
                    "metadata_identifier_conflict",
                    "Provider metadata points to a different catalogue row.",
                    attempts_made,
                )
            updates, conflicts = self.owner._metadata_updates(catalogue, record, metadata)
            if conflicts:
                return (
                    record,
                    False,
                    "catalogue_existing_value_conflict",
                    "; ".join(conflicts),
                    attempts_made,
                )
            if updates:
                catalogue.update_fields(record, updates)

        canonical = self.canonicalizer.canonicalize(
            catalogue,
            record,
            metadata,
            merged=merged,
        )
        if canonical.conflicts:
            issue_key = (
                "metadata_journal_conflict"
                if "metadata_journal_conflict" in canonical.conflicts
                else canonical.conflicts[0]
            )
            return (
                record,
                False,
                issue_key,
                "; ".join(canonical.conflicts),
                attempts_made,
            )
        file_result["canonicalization"] = {
            "record_uid": canonical.record_uid,
            "canonical_id": canonical.canonical_id,
            "canonical_source": canonical.canonical_source,
            "changed_fields": canonical.changed_fields,
        }

        file_result.update(
            {
                "match_status": MatchStatus.EXACT.value,
                "matched_catalogue_id": str(record.get("id") or ""),
                "match_method": "workflow2_provider",
                "canonical_title_selected": metadata.title,
                "canonical_title_source": "provider",
                "selected_title": str(record.get("title") or metadata.title),
                "selected_title_source": "provider",
                "match_evidence": {
                    "confidence": lookup.confidence,
                    "providers": lookup.providers_used,
                    "selection_reason": lookup.selection_reason,
                },
            }
        )
        return record, True, "", "", attempts_made

    def _apply_local_fallback(
        self,
        catalogue: CatalogueService,
        record: CatalogueRecord,
        inspection: PdfInspection,
        file_result: dict[str, Any],
    ) -> tuple[bool, str, str]:
        if file_result.get("metadata_lookup_status") not in {
            MetadataLookupStatus.NOT_FOUND.value,
            MetadataLookupStatus.UNAVAILABLE.value,
            MetadataLookupStatus.FAILED.value,
            None,
        }:
            return False, "", ""
        local = inspection.local_metadata or {}
        if not local:
            return (
                False,
                "local_pdf_metadata_incomplete",
                "No structured first-page metadata passed the local quality checks.",
            )
        merged, field_sources, conflicts = self.owner.build_merged_identity_evidence(
            record, None, inspection
        )
        if conflicts:
            return (
                False,
                "metadata_identifier_conflict",
                "Catalogue and local PDF identifiers conflict.",
            )
        updates: dict[str, Any] = {}
        used_fields: list[str] = []
        for field_name, value in local.items():
            if field_name in {"abstract", "field_sources", "warnings"}:
                continue
            if field_name not in catalogue.headers or value in (None, "", [], ()):
                continue
            if record.get(field_name) not in (None, ""):
                continue
            if field_name == "authors" and isinstance(value, (list, tuple)):
                value = "; ".join(str(item) for item in value if str(item).strip())
            updates[field_name] = value
            used_fields.append(field_name)
        if "source" in catalogue.headers:
            updates["source"] = "local_pdf"
        if updates:
            catalogue.ensure_record_uid(record)
            catalogue.update_provisional_fields(record, updates)
        for field_name in used_fields:
            catalogue.add_uncertainty(
                record,
                "MACHINE_NOTE:",
                field_name,
                "High-quality first-page PDF metadata filled a blank catalogue field.",
                issue_key="local_pdf_metadata_used",
            )
        merged, field_sources, conflicts = self.owner.build_merged_identity_evidence(
            record, None, inspection
        )
        confirmed, reason = self.owner.validate_durable_identity(
            merged,
            sources=field_sources,
            conflicts=conflicts,
            user_confirmed_identity=catalogue.has_user_confirmation(record, "paper_identity"),
            lookup_confidence="",
            inspection=inspection,
        )
        file_result["local_pdf_metadata_used"] = used_fields
        file_result["final_field_sources"] = field_sources
        file_result["durable_identity_reason"] = reason
        if confirmed:
            resolved = self._resolved_provisional_uncertainty(record)
            catalogue.update_provisional_fields(record, {"uncertainty": resolved})
            file_result.update(
                {
                    "match_status": MatchStatus.HIGH_CONFIDENCE.value,
                    "matched_catalogue_id": str(record.get("id") or ""),
                    "match_method": "local_pdf_merged",
                    "canonical_title_selected": str(record.get("title") or ""),
                    "canonical_title_source": field_sources.get("title", "local_pdf"),
                    "selected_title": str(record.get("title") or ""),
                    "selected_title_source": field_sources.get("title", "local_pdf"),
                }
            )
            return True, "", ""
        return (
            False,
            "local_pdf_metadata_incomplete",
            "Local first-page metadata was reused, but merged identity evidence remains incomplete.",
        )

    def _create_provisional(
        self,
        catalogue: CatalogueService,
        filename: str,
        relative: str,
        evidence: FilenameEvidence,
        inspection: PdfInspection,
    ) -> CatalogueRecord:
        administrative_title = re.compile(
            r"^(?:accepted|received|published(?:\s+online)?|pii\s*:|doi\s*:|copyright)\b",
            re.I,
        )
        pypdf_candidates = [
            item for item in inspection.title_candidates
            if item.source_type != "filename"
            and not item.source_type.startswith("ocr")
            and item.confidence == "high"
            and not administrative_title.search(item.value.strip())
        ]
        pypdf_candidate = pypdf_candidates[0] if pypdf_candidates else None
        if (
            pypdf_candidates
            and evidence.title_candidate
            and evidence.title_candidate.evidence == "standard_filename"
        ):
            pypdf_candidate = max(
                pypdf_candidates,
                key=lambda item: tolerant_title_score(
                    item.value, evidence.title_candidate.value
                ),
            )
            if tolerant_title_score(
                pypdf_candidate.value, evidence.title_candidate.value
            ) < 0.92:
                pypdf_candidate = None
        ocr_candidate = next(
            (item for item in inspection.title_candidates if item.source_type.startswith("ocr")),
            None,
        )
        selected = pypdf_candidate or evidence.title_candidate or ocr_candidate
        title = selected.value if selected else Path(filename).stem
        title_source = (
            "pypdf" if pypdf_candidate
            else "filename" if evidence.title_candidate
            else "ocr" if ocr_candidate
            else "filename"
        )
        today = date.today().isoformat()
        uncertainty = (
            "NEEDS_REVIEW: field=paper_identity; issue_key=metadata_identity_unconfirmed; "
            f"issue={PROVISIONAL_BLOCKER}\n"
            f"MACHINE_NOTE: field=title; issue_key=title_provisional_{title_source}; "
            f"issue=Current title was derived from {title_source} evidence."
        )
        values = {
            "id": f"LOCAL:{uuid.uuid4()}",
            "title": title,
            "year": evidence.year,
            "journal": evidence.journal,
            "publication_type": evidence.publication_type,
            "pdf_status": PdfStatus.INBOX.value,
            "pdf_filename": filename,
            "pdf_relative_path": relative,
            "source": "local_pdf",
            "date_added": today,
            "date_updated": today,
            "uncertainty": uncertainty,
        }
        return catalogue.add_record(values)

    def _retain_provisional(
        self,
        catalogue: CatalogueService,
        record: CatalogueRecord,
        result: WorkflowResult,
        file_result: dict[str, Any],
        issue_key: str,
        issue: str,
        relative: str,
        *,
        blocked: bool,
    ) -> None:
        if issue_key not in file_result["issue_keys"]:
            file_result["issue_keys"].append(issue_key)
        if self._is_provisional(record):
            if "metadata_identity_unconfirmed" not in file_result["issue_keys"]:
                file_result["issue_keys"].append("metadata_identity_unconfirmed")
            hard_conflict = issue_key in {
                "metadata_identifier_conflict",
                "metadata_title_conflict",
                "metadata_journal_conflict",
                "registered_filename_collision",
                "source_changed_during_run",
                "target_appeared_during_run",
            }
            if hard_conflict:
                blocker_outcome = catalogue.ensure_review_blocker(
                    record,
                    "paper_identity",
                    issue,
                    issue_key=issue_key,
                    conflict_with_confirmation=True,
                )
            else:
                blocker_outcome = catalogue.ensure_review_blocker(
                    record,
                    "paper_identity",
                    PROVISIONAL_BLOCKER,
                    issue_key="metadata_identity_unconfirmed",
                )
                if blocker_outcome == "cleared" and issue_key != "metadata_identity_unconfirmed":
                    blocker_outcome = catalogue.ensure_review_blocker(
                        record,
                        "paper_identity",
                        issue,
                        issue_key=issue_key,
                    )
            updates = {
                "pdf_status": PdfStatus.INBOX.value,
                "pdf_filename": Path(relative).name,
                "pdf_relative_path": relative,
                "date_updated": date.today().isoformat(),
            }
            catalogue.update_provisional_fields(record, updates)
        else:
            blocker_outcome = catalogue.ensure_review_blocker(
                record,
                "paper_identity",
                issue,
                issue_key=issue_key,
                conflict_with_confirmation=issue_key in {"metadata_identifier_conflict"},
            )
        status = InboxItemStatus.BLOCKED if blocked else InboxItemStatus.PROVISIONAL
        file_result["action"] = status.value
        file_result["result_status"] = status.value
        file_result["selected_title"] = str(record.get("title") or "")
        file_result["selected_title_source"] = (
            file_result.get("selected_title_source") or self._local_title_source(file_result)
        )
        item = {
            "file": relative,
            "row": record.row_number,
            "issue": issue_key,
            "details": issue,
            "result_status": status.value,
        }
        if blocker_outcome in {"added", "existing"} and item not in result.needs_review:
            result.needs_review.append(item)
        result.completed.append(
            {
                "action": status.value,
                "row": record.row_number,
                "source": relative,
            }
        )

    def _filename_inspection(
        self, source: Path, evidence: FilenameEvidence
    ) -> PdfInspection:
        stat = source.stat()
        title_candidates = [evidence.title_candidate] if evidence.title_candidate else []
        inspection = PdfInspection(
            relative_path=source.relative_to(self.settings.library_root).as_posix(),
            filename=source.name,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            is_readable=True,
            doi_candidates=list(evidence.doi_candidates),
            pmid_candidates=list(evidence.pmid_candidates),
            title_candidates=title_candidates,
            year_candidates=[evidence.year] if evidence.year else [],
            journal_candidates=[evidence.journal] if evidence.journal else [],
            is_probable_supplement=is_probable_supplement(source.name),
            text_extraction_method="filename",
        )
        return inspection

    @staticmethod
    def _lookup_worthy(
        inspection: PdfInspection, record: CatalogueRecord | None = None
    ) -> bool:
        if record and any(
            record.get(field) for field in ("pmid", "doi", "arxiv_id", "title")
        ):
            return True
        if inspection.doi_candidates or inspection.pmid_candidates:
            return True
        return any(
            item.confidence in {"medium", "high"}
            and len(item.value.split()) >= 3
            and len(item.value) >= 12
            for item in inspection.title_candidates
        )

    @staticmethod
    def _pypdf_sufficient(inspection: PdfInspection) -> bool:
        if inspection.doi_candidates or inspection.pmid_candidates:
            return True
        return any(
            item.confidence == "high" and not item.source_type.startswith("ocr")
            for item in inspection.title_candidates
        )

    @staticmethod
    def _same_source(source: Path, inspection: PdfInspection) -> bool:
        try:
            stat = source.stat()
        except OSError:
            return False
        return stat.st_size == inspection.size and stat.st_mtime_ns == inspection.mtime_ns

    @staticmethod
    def _is_provisional(record: CatalogueRecord | None) -> bool:
        return bool(record and str(record.get("id") or "").upper().startswith("LOCAL:"))

    def _existing_local_record(
        self,
        records: list[CatalogueRecord],
        relative: str,
        filename: str,
        source: Path,
    ) -> CatalogueRecord | None:
        path_key = normalized_relative_path(relative)
        filename_key = normalized_text(filename)
        by_path = [
            row for row in records
            if normalized_relative_path(row.get("pdf_relative_path")) == path_key
        ]
        if len(by_path) == 1:
            return by_path[0]
        by_name = [
            row for row in records
            if normalized_text(row.get("pdf_filename")) == filename_key
        ]
        if len(by_name) == 1:
            return by_name[0]

        # Lower-priority saved local identity: a unique unchanged size/mtime
        # pair from the previous blocker state can reconnect a renamed Inbox
        # file to its existing LOCAL row without making hashes authoritative.
        blocker_path = self.settings.state_dir / "inbox_blockers.json"
        if not blocker_path.is_file():
            return None
        try:
            stat = source.stat()
            payload = json.loads(blocker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        identities = [
            item
            for item in payload.get("files", [])
            if item.get("size") == stat.st_size
            and item.get("mtime_ns") == stat.st_mtime_ns
        ]
        if len(identities) != 1:
            return None
        previous_path = normalized_relative_path(identities[0].get("source_path"))
        matched = [
            row for row in records
            if normalized_relative_path(row.get("pdf_relative_path")) == previous_path
        ]
        return matched[0] if len(matched) == 1 else None

    @staticmethod
    def _resolved_provisional_uncertainty(record: CatalogueRecord) -> str:
        retained = []
        for line in str(record.get("uncertainty") or "").splitlines():
            normalized = normalized_text(line)
            if line.lstrip().upper().startswith("NEEDS_REVIEW:") and (
                "issue_key=metadata_identity_unconfirmed" in normalized
                or "field=paper_identity" in normalized
            ):
                continue
            if line.lstrip().upper().startswith("MACHINE_NOTE:") and "title_provisional_" in normalized:
                continue
            if line.strip():
                retained.append(line.rstrip())
        return "\n".join(retained)

    @staticmethod
    def _active_review(record: CatalogueRecord) -> bool:
        return any(
            line.lstrip().upper().startswith("NEEDS_REVIEW:")
            for line in str(record.get("uncertainty") or "").splitlines()
        )

    @staticmethod
    def _candidate_dict(candidate: TitleCandidate | None) -> dict[str, Any] | None:
        if candidate is None:
            return None
        return {
            "value": candidate.value,
            "source": candidate.source_type,
            "confidence": candidate.confidence,
            "evidence": candidate.evidence,
        }

    def _new_file_result(
        self, relative: str, evidence: FilenameEvidence
    ) -> dict[str, Any]:
        filename_candidate = self._candidate_dict(evidence.title_candidate)
        return {
            "source_path": relative,
            "catalogue_id": None,
            "result_status": InboxItemStatus.BLOCKED.value,
            "inspection_status": "filename_only",
            "inspection_level_used": InspectionLevel.SKIP.value,
            "match_status": MatchStatus.NOT_FOUND.value,
            "matched_catalogue_id": None,
            "match_method": "none",
            "target_filename": None,
            "target_path": None,
            "action": InboxItemStatus.BLOCKED.value,
            "issue_keys": [],
            "filename_candidate": filename_candidate,
            "pypdf_candidate": None,
            "ocr_candidate": None,
            "provider_candidate": None,
            "selected_title": evidence.title_candidate.value if evidence.title_candidate else "",
            "selected_title_source": "filename" if evidence.title_candidate else "",
            "title_candidate_sources": ["filename"] if evidence.title_candidate else [],
            "provider_lookup_attempts": [],
            "canonical_title_selected": "",
            "canonical_title_source": "",
            "match_evidence": {},
            "provisional_created": False,
            "provisional_updated": False,
        }

    def _set_inspection_report(
        self, file_result: dict[str, Any], inspection: PdfInspection
    ) -> None:
        file_result["inspection_status"] = "readable" if inspection.is_readable else "unreadable"
        file_result["inspection"] = inspection.report_summary()
        self.owner._add_ocr_report(file_result, inspection)
        pypdf = next(
            (
                item for item in inspection.title_candidates
                if not item.source_type.startswith("ocr") and item.source_type != "filename"
            ),
            None,
        )
        ocr = next(
            (item for item in inspection.title_candidates if item.source_type.startswith("ocr")),
            None,
        )
        file_result["pypdf_candidate"] = self._candidate_dict(pypdf)
        file_result["ocr_candidate"] = self._candidate_dict(ocr)
        sources = [item.source_type for item in inspection.title_candidates]
        file_result["title_candidate_sources"] = list(dict.fromkeys(sources))

    @staticmethod
    def _set_match_report(file_result: dict[str, Any], match) -> None:
        file_result["match_status"] = match.status.value
        file_result["matched_catalogue_id"] = match.matched_catalogue_id
        file_result["match_method"] = match.method
        file_result["match_evidence"] = {
            "confidence": match.confidence,
            "method": match.method,
            "candidate_rows": match.candidate_rows,
            "conflicts": match.conflicts,
        }

    @staticmethod
    def _local_title_source(file_result: dict[str, Any]) -> str:
        if file_result.get("pypdf_candidate"):
            return "pypdf"
        if file_result.get("filename_candidate"):
            return "filename"
        if file_result.get("ocr_candidate"):
            return "ocr"
        return "unknown"

    @staticmethod
    def _set_counts(
        result: WorkflowResult,
        discovered: list[Path],
        file_results: list[dict[str, Any]],
        ready: list[dict[str, Any]],
        lookups: int,
        ocr_files: int,
        provisional_created: int,
        provisional_updated: int,
    ) -> None:
        statuses = [item.get("result_status") for item in file_results]
        result.counts = {
            "files_discovered": len(discovered),
            "ready": len(ready),
            "registered": statuses.count(InboxItemStatus.REGISTERED.value),
            "provisional": statuses.count(InboxItemStatus.PROVISIONAL.value),
            "blocked": statuses.count(InboxItemStatus.BLOCKED.value),
            "failed": statuses.count(InboxItemStatus.FAILED.value),
            "provisional_created": provisional_created,
            "provisional_updated": provisional_updated,
            "metadata_lookup_requests": lookups,
            "ocr_files": ocr_files,
        }

    def _plan_supplementary(
        self,
        catalogue: CatalogueService,
        files: FileService,
        source: Path,
        parsed: Any,
        *,
        paper_uuid: str,
        reserved: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        relative = source.relative_to(self.settings.library_root).as_posix()
        file_result: dict[str, Any] = {
            "source_path": relative,
            "result_status": InboxItemStatus.BLOCKED.value,
            "action": InboxItemStatus.BLOCKED.value,
            "issue_keys": [],
            "document_type": "supplementary",
            "supplementary_type": parsed.supplementary_type,
            "sequence": parsed.sequence,
        }
        matches = catalogue.find_by("paper_uuid", paper_uuid)
        if len(matches) != 1:
            file_result["issue_keys"].append("supplementary_uuid_not_found")
            file_result["details"] = f"No unique Catalogue paper_uuid={paper_uuid!r}"
            return None, file_result
        record = matches[0]
        service = SupplementaryRegistrationService(
            self.settings.library_root,
            catalogue,
            max_filename_length=self.settings.max_filename_length,
        )
        inbox_item = SupplementaryInboxItem(source, relative, parsed)
        plan = service.plan_item(
            inbox_item,
            record,
            main_document_expected=parsed.binding == "same_stem",
        )
        conflicts = list(plan.conflicts)
        for existing in reserved or []:
            values = existing["document_values"]
            if normalized_text(values.get("sha256")) == normalized_text(plan.sha256):
                conflicts.append("supplementary_duplicate_file")
            if normalized_text(values.get("document_id")) == normalized_text(plan.document_id):
                conflicts.append("supplementary_document_id_conflict")
            if (
                normalized_text(values.get("paper_uuid")) == normalized_text(plan.paper_uuid)
                and normalized_text(values.get("supplementary_type"))
                == normalized_text(plan.supplementary_type)
                and normalized_text(values.get("sequence"))
                == normalized_text(plan.sequence)
            ):
                conflicts.append("supplementary_sequence_conflict")
            if normalized_relative_path(values.get("relative_path")) == normalized_relative_path(
                plan.target_relative_path
            ):
                conflicts.append("supplementary_target_collision")
        conflicts = list(dict.fromkeys(conflicts))
        if conflicts or not plan.target_filename or not plan.target_relative_path:
            file_result["issue_keys"].extend(
                conflicts or ["supplementary_naming_metadata_missing"]
            )
            file_result["details"] = {"conflicts": conflicts}
            return None, file_result
        try:
            operation = files.plan_document_registration_move(
                source,
                plan.target_filename,
                record.row_number,
                "confirmed supplementary binding",
            )
        except FileOperationError as exc:
            file_result["issue_keys"].append("supplementary_target_collision")
            file_result["details"] = str(exc)
            return None, file_result
        file_result.update(
            {
                "result_status": InboxItemStatus.REGISTERED.value,
                "action": "planned",
                "target_filename": plan.target_filename,
                "target_path": plan.target_relative_path,
                "document_id": plan.document_id,
            }
        )
        return (
            {
                "source": source,
                "record": record,
                "catalogue_row": record.row_number,
                "operation": operation,
                "document_values": plan.document_values,
                "file_result": file_result,
            },
            file_result,
        )

    def _create_journal(
        self,
        ready: list[dict[str, Any]],
        supplementary_ready: list[dict[str, Any]] | None = None,
    ) -> OperationJournal:
        operations = []
        for item in ready:
            operation = item["operation"]
            paper_uuid = str(item["record"].get("paper_uuid") or "") or None
            document_id = f"{paper_uuid}:main" if paper_uuid else None
            operations.append(
                {
                    **operation.to_dict(),
                    "record_uid": item["record"].get("record_uid"),
                    "paper_uuid": paper_uuid,
                    "document_id": document_id,
                    "operation_id": document_id or f"catalogue-row:{item['record'].row_number}",
                    "source_fingerprint": {
                        "size": operation.expected_size,
                        "mtime_ns": operation.expected_mtime_ns,
                    },
                    "planned_updates": item["updates"],
                    "title_provenance": {
                        key: item["file_result"].get(key)
                        for key in (
                            "previous_title",
                            "new_canonical_title",
                            "previous_title_source",
                            "selected_provider",
                            "selection_reason",
                        )
                    },
                    "execution_state": "planned",
                }
            )
        for item in supplementary_ready or []:
            operation = item["operation"]
            values = item["document_values"]
            operations.append(
                {
                    **operation.to_dict(),
                    "paper_uuid": values["paper_uuid"],
                    "document_id": values["document_id"],
                    "operation_id": values["document_id"],
                    "source_fingerprint": {
                        "size": operation.expected_size,
                        "mtime_ns": operation.expected_mtime_ns,
                    },
                    "planned_updates": values,
                    "execution_state": "planned",
                }
            )
        return OperationJournal.create(self.settings.state_dir, operations)
