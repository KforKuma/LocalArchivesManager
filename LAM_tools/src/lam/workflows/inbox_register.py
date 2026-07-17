from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import FileOperationError
from ..models import (
    CatalogueRecord,
    IdentifierCandidate,
    MetadataLookupRequest,
    MetadataRecord,
    PdfStatus,
    WorkflowResult,
)
from ..providers.base import MetadataLookupService
from ..providers.unavailable import UnavailableMetadataService
from ..services.catalogue_service import CatalogueService
from ..services.metadata_service import CompositeMetadataLookupService
from ..utils.identifiers import normalize_arxiv_id, normalize_doi, normalize_pmid
from ..utils.journal import journal_is_variant, journals_equivalent
from ..utils.normalize import normalized_text
from ..utils.text import normalize_title
from ..utils.uncertainty import confirmation_for, confirmed_value
from ..utils.supplementary import is_supported_document_extension


class InboxRegisterWorkflow:
    def __init__(
        self,
        settings: Settings,
        metadata_service: MetadataLookupService | None = None,
        ocr_service=None,
    ):
        self.settings = settings
        self.metadata_service = metadata_service or (
            CompositeMetadataLookupService(settings)
            if settings.metadata_lookup_enabled
            else UnavailableMetadataService()
        )
        self.ocr_service = ocr_service

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
        offline: bool = False,
        refresh: bool = False,
        cache_write: bool = True,
    ) -> WorkflowResult:
        from .progressive_register import ProgressiveInboxRegisterWorkflow

        return ProgressiveInboxRegisterWorkflow(self).run(
            dry_run=dry_run,
            max_files=max_files,
            filename_only=filename_only,
            skip_pdf_text=skip_pdf_text,
            ocr_mode=ocr_mode,
            ocr_languages=ocr_languages,
            ocr_dpi=ocr_dpi,
            ocr_gpu=ocr_gpu,
            offline=offline,
            refresh=refresh,
            cache_write=cache_write,
        )

    def _discover_inbox(self) -> tuple[list[Path], list[dict[str, Any]]]:
        eligible: list[Path] = []
        skipped: list[dict[str, Any]] = []
        if not self.settings.inbox_dir.is_dir():
            return eligible, skipped
        for path in sorted(
            self.settings.inbox_dir.iterdir(), key=lambda item: item.name.casefold()
        ):
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

    def _discover_inbox_documents(self) -> tuple[list[Path], list[dict[str, Any]]]:
        """Discover supported managed document files without opening their content."""
        eligible: list[Path] = []
        skipped: list[dict[str, Any]] = []
        if not self.settings.inbox_dir.is_dir():
            return eligible, skipped
        for path in sorted(
            self.settings.inbox_dir.iterdir(), key=lambda item: item.name.casefold()
        ):
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
            if not is_supported_document_extension(path.suffix):
                skipped.append({"file": relative, "reason": "unsupported_document_type"})
                continue
            eligible.append(path)
        return eligible, skipped

    def _eligible_document_files(self) -> list[Path]:
        return self._discover_inbox_documents()[0]

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
    def _lookup_request(
        inspection, relative: str, record: CatalogueRecord | None = None
    ) -> MetadataLookupRequest:
        """Backward-compatible single request using the highest-priority evidence."""
        requests = InboxRegisterWorkflow._lookup_requests(record, inspection, relative)
        return requests[0] if requests else MetadataLookupRequest(source_pdf=relative)

    @staticmethod
    def _lookup_requests(
        record: CatalogueRecord | None, inspection, relative: str
    ) -> list[MetadataLookupRequest]:
        uncertainty = str(record.get("uncertainty") or "") if record else ""
        identity_confirmation = confirmation_for(uncertainty, "paper_identity")
        local = inspection.local_metadata or {}
        paper_uuid = str(record.get("paper_uuid") or "") if record else ""
        supporting = {
            "title": (
                confirmed_value(uncertainty, "title")
                or str(record.get("title") or "") if record else ""
            ) or str(local.get("title") or ""),
            "authors": (
                str(record.get("authors") or "") if record else ""
            ) or "; ".join(local.get("authors") or ()) or inspection.metadata_author,
            "year": (
                confirmed_value(uncertainty, "publication_year")
                or (str(record.get("year") or "") if record else "")
                or str(local.get("year") or "")
                or (inspection.year_candidates[0] if inspection.year_candidates else "")
            ),
            "journal": (
                confirmed_value(uncertainty, "journal")
                or (str(record.get("journal") or "") if record else "")
                or str(local.get("journal") or "")
                or (inspection.journal_candidates[0] if inspection.journal_candidates else "")
            ),
        }

        requests: list[MetadataLookupRequest] = []
        seen: set[tuple[str, str]] = set()

        def add_request(
            *,
            pmid: str = "",
            doi: str = "",
            arxiv_id: str = "",
            title: str = "",
            source: str,
            confidence: str = "high",
            context: str = "",
        ) -> None:
            query_type = "pmid" if pmid else "doi" if doi else "arxiv" if arxiv_id else "title"
            query_value = pmid or doi or arxiv_id or normalize_title(title)
            if query_type == "title" and (
                len(str(title).strip()) < 12 or len(str(title).split()) < 3
            ):
                return
            if not query_value or (query_type, query_value) in seen:
                return
            seen.add((query_type, query_value))
            requests.append(
                MetadataLookupRequest(
                    pmid=pmid or None,
                    doi=doi or None,
                    arxiv_id=arxiv_id or None,
                    title=title or supporting["title"] or None,
                    authors=supporting["authors"] or None,
                    year=supporting["year"] or None,
                    journal=supporting["journal"] or None,
                    paper_uuid=paper_uuid or None,
                    user_confirmed_identity=identity_confirmation is not None,
                    source_pdf=relative,
                    candidate_source=source,
                    candidate_confidence=confidence,
                    candidate_context=context[:300] or None,
                )
            )

        if identity_confirmation and identity_confirmation.value:
            confirmed = identity_confirmation.value
            add_request(
                pmid=normalize_pmid(confirmed),
                doi=normalize_doi(confirmed),
                arxiv_id=normalize_arxiv_id(confirmed),
                title=(supporting["title"] if not any((normalize_pmid(confirmed), normalize_doi(confirmed), normalize_arxiv_id(confirmed))) else ""),
                source="user_confirmation",
                context=confirmed,
            )
        if record:
            add_request(pmid=normalize_pmid(record.get("pmid")), source="catalogue_pmid")
            add_request(doi=normalize_doi(record.get("doi")), source="catalogue_doi")
            add_request(
                arxiv_id=normalize_arxiv_id(record.get("arxiv_id")),
                source="catalogue_arxiv_id",
            )
        for item in inspection.pmid_candidates:
            add_request(
                pmid=normalize_pmid(item.value),
                source=item.source_type,
                confidence=item.confidence,
                context=item.line_or_context,
            )
        for item in inspection.doi_candidates:
            add_request(
                doi=normalize_doi(item.value),
                source=item.source_type,
                confidence=item.confidence,
                context=item.line_or_context,
            )
        if record and supporting["title"]:
            add_request(title=supporting["title"], source="catalogue_title")
        source_priority = (
            "filename",
            "metadata",
            "page_top",
            "first_page",
            "sampled_page",
            "ocr",
        )
        for prefix in source_priority:
            candidate = next(
                (
                    item
                    for item in inspection.title_candidates
                    if item.source_type == prefix or item.source_type.startswith(prefix)
                ),
                None,
            )
            if candidate:
                add_request(
                    title=candidate.value,
                    source=candidate.source_type,
                    confidence=candidate.confidence,
                    context=candidate.value,
                )
        return requests

    @staticmethod
    def _metadata_supports_inspection(metadata: MetadataRecord, inspection) -> bool:
        doi_candidates = [
            item for item in inspection.doi_candidates if normalize_doi(item.value)
        ]
        pmid_candidates = [
            item for item in inspection.pmid_candidates if normalize_pmid(item.value)
        ]
        title_matches = bool(
            metadata.title
            and any(
                normalize_title(item.value) == normalize_title(metadata.title)
                for item in inspection.title_candidates
            )
        )
        year_matches = bool(
            metadata.year and str(metadata.year).strip() in inspection.year_candidates
        )
        if metadata.doi and doi_candidates:
            matching = [
                item for item in doi_candidates
                if normalize_doi(item.value) == normalize_doi(metadata.doi)
            ]
            if matching:
                if all(item.source_type == "ocr_corrected" for item in matching):
                    return title_matches and (year_matches or not metadata.year)
                return True
            return False
        if metadata.pmid and pmid_candidates:
            return any(
                normalize_pmid(item.value) == normalize_pmid(metadata.pmid)
                for item in pmid_candidates
            )
        if inspection.ocr_result is not None:
            return title_matches and (year_matches or not metadata.year)
        return True

    @staticmethod
    def _unsupported_ocr_title_only_match(match, inspection, records) -> bool:
        if match.method != "normalized_title" or match.confidence != "exact_title_only":
            return False
        record = next(
            (item for item in records if item.row_number == match.matched_row_id), None
        )
        if record is None:
            return False
        target = normalize_title(record.get("title"))
        ocr_titles = {
            normalize_title(item.value)
            for item in inspection.title_candidates
            if item.source_type.startswith("ocr")
        }
        other_titles = {
            normalize_title(item.value)
            for item in inspection.title_candidates
            if not item.source_type.startswith("ocr")
        }
        return target in ocr_titles and target not in other_titles

    @staticmethod
    def _add_ocr_report(file_result: dict[str, Any], inspection) -> None:
        ocr = inspection.ocr_result
        file_result.update(
            {
                "pypdf_text_available": inspection.pypdf_text_available,
                "ocr_triggered": ocr is not None,
                "ocr_trigger_reason": ocr.trigger_reason if ocr else None,
                "ocr_status": ocr.status if ocr else "not_run",
                "ocr_device": ocr.gpu_mode if ocr else None,
                "ocr_dpi": ocr.dpi if ocr else None,
                "ocr_cache_hit": ocr.cache_hit if ocr else False,
                "ocr_title_candidates": [item.value for item in ocr.title_candidates[:5]] if ocr else [],
                "ocr_doi_candidates": [item.value for item in ocr.doi_candidates] if ocr else [],
                "ocr_pmid_candidates": [item.value for item in ocr.pmid_candidates] if ocr else [],
                "ocr_warnings": list(ocr.warnings) if ocr else [],
                "evidence_selected": inspection.text_extraction_method,
            }
        )

    @staticmethod
    def _metadata_catalogue_candidates(
        records: list[CatalogueRecord], metadata: MetadataRecord
    ) -> list[CatalogueRecord]:
        for field, value, normalizer in (
            ("pmid", metadata.pmid, normalize_pmid),
            ("doi", metadata.doi, normalize_doi),
        ):
            key = normalizer(value)
            if key:
                matches = [row for row in records if normalizer(row.get(field)) == key]
                if matches:
                    return matches
        title = normalize_title(metadata.title)
        if not title:
            return []
        matches = [row for row in records if normalize_title(row.get("title")) == title]
        if metadata.year:
            supported = [
                row for row in matches
                if not row.get("year") or str(row.get("year")).strip() == metadata.year
            ]
            if supported:
                matches = supported
        return matches

    @staticmethod
    def _metadata_updates(
        catalogue: CatalogueService,
        record: CatalogueRecord,
        metadata: MetadataRecord,
    ) -> tuple[dict[str, Any], list[str]]:
        updates: dict[str, Any] = {}
        conflicts: list[str] = []
        for field, value in metadata.catalogue_fields().items():
            if field not in catalogue.headers or value in (None, ""):
                continue
            current = record.get(field)
            if current in (None, ""):
                updates[field] = value
                continue
            if field == "doi":
                equivalent = normalize_doi(current) == normalize_doi(value)
            elif field == "pmid":
                equivalent = normalize_pmid(current) == normalize_pmid(value)
            elif field == "title":
                equivalent = normalize_title(current) == normalize_title(value)
            elif field == "journal":
                equivalent = journals_equivalent(current, value)
            else:
                equivalent = normalized_text(current) == normalized_text(value)
            if not equivalent and field in {"title", "authors", "year", "journal", "doi", "pmid"}:
                conflicts.append(f"field={field}")
        return updates, conflicts

    @staticmethod
    def _eligible_provider_record(metadata: MetadataRecord) -> bool:
        return bool(
            (metadata.pmid or metadata.doi or metadata.arxiv_id)
            and metadata.title
            and metadata.authors
            and metadata.year
            and metadata.source
        )

    @staticmethod
    def _journal_names_equivalent(left: object, right: object) -> bool:
        return journals_equivalent(left, right)

    @staticmethod
    def _journal_is_variant(left: object, right: object) -> bool:
        return journal_is_variant(left, right)

    @staticmethod
    def _author_list(value: object) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text:
            return []
        return [item.strip() for item in re.split(r"\s*;\s*", text) if item.strip()]

    @classmethod
    def build_merged_identity_evidence(
        cls,
        record: CatalogueRecord | None,
        provider: MetadataRecord | None,
        inspection,
        *,
        lookup_confidence: str = "",
    ) -> tuple[MetadataRecord, dict[str, str], list[str]]:
        uncertainty = str(record.get("uncertainty") or "") if record else ""
        local = inspection.local_metadata or {}
        sources: dict[str, str] = {}
        conflicts: list[str] = []

        def choose(field: str, provider_value: object, confirmed_field: str | None = None):
            confirmed = confirmed_value(uncertainty, confirmed_field or field)
            catalogue_value = record.get(field) if record else None
            local_value = local.get(field)
            for source, value in (
                ("provider", provider_value),
                ("user_confirmed", confirmed),
                ("catalogue", catalogue_value),
                ("pypdf_first_page", local_value),
            ):
                if value not in (None, "", [], ()):
                    sources[field] = source
                    return value
            return ""

        provider = provider or MetadataRecord()
        pmid = normalize_pmid(choose("pmid", provider.pmid))
        doi = normalize_doi(choose("doi", provider.doi))
        arxiv_id = normalize_arxiv_id(
            choose("arxiv_id", provider.arxiv_id)
        )
        title = str(choose("title", provider.title) or "")
        authors = cls._author_list(choose("authors", provider.authors))
        year = str(choose("year", provider.year, "publication_year") or "")
        journal = str(choose("journal", provider.journal) or "")
        journal_abbrev = str(choose("journal_abbrev", provider.journal_abbrev) or "")
        publication_type = choose("publication_type", provider.publication_type)

        catalogue_pmid = normalize_pmid(record.get("pmid")) if record else ""
        catalogue_doi = normalize_doi(record.get("doi")) if record else ""
        pdf_pmids = {
            normalize_pmid(item.value)
            for item in inspection.pmid_candidates
            if normalize_pmid(item.value)
        }
        pdf_dois = {
            normalize_doi(item.value)
            for item in inspection.doi_candidates
            if normalize_doi(item.value) and item.source_type != "ocr_corrected"
        }
        if catalogue_pmid and provider.pmid and catalogue_pmid != normalize_pmid(provider.pmid):
            conflicts.append("pmid_catalogue_provider_conflict")
        if catalogue_doi and provider.doi and catalogue_doi != normalize_doi(provider.doi):
            conflicts.append("doi_catalogue_provider_conflict")
        if catalogue_pmid and pdf_pmids and catalogue_pmid not in pdf_pmids:
            conflicts.append("pmid_catalogue_pdf_conflict")
        if catalogue_doi and pdf_dois and catalogue_doi not in pdf_dois:
            conflicts.append("doi_catalogue_pdf_conflict")

        merged = MetadataRecord(
            canonical_id=provider.canonical_id,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            journal_abbrev=journal_abbrev,
            doi=doi,
            pmid=pmid,
            arxiv_id=arxiv_id,
            publication_type=publication_type or None,
            raw_publication_types=list(provider.raw_publication_types),
            abstract=provider.abstract,
            keywords=list(provider.keywords),
            mesh_terms=list(provider.mesh_terms),
            categories=list(provider.categories),
            source=list(dict.fromkeys([*provider.source, "catalogue" if record else "", "local_pdf" if local else ""])),
        )
        merged.source = [item for item in merged.source if item]
        sources["lookup_confidence"] = lookup_confidence
        return merged, sources, conflicts

    @staticmethod
    def validate_durable_identity(
        merged: MetadataRecord,
        *,
        sources: dict[str, str],
        conflicts: list[str],
        user_confirmed_identity: bool,
        lookup_confidence: str,
        inspection,
    ) -> tuple[bool, str]:
        if conflicts:
            return False, "metadata_identifier_conflict"
        has_naming_core = bool(merged.title and merged.year and (merged.journal or merged.journal_abbrev))
        has_authors = bool(merged.authors)
        exact_identifier = lookup_confidence == "exact_identifier" and bool(
            merged.pmid or merged.doi or merged.arxiv_id
        )
        if exact_identifier and merged.title and (merged.year or has_authors):
            return True, "provider_identifier_merged"
        if lookup_confidence == "exact_title_supported" and merged.title and has_authors and merged.year:
            return True, "provider_title_merged"
        if user_confirmed_identity and has_naming_core and (has_authors or merged.pmid or merged.doi):
            return True, "user_confirmed_merged"
        local = inspection.local_metadata or {}
        local_quality = bool(
            local.get("title")
            and local.get("authors")
            and local.get("year")
            and local.get("journal")
        )
        if local_quality and has_naming_core and has_authors:
            return True, "local_pdf_high_quality"
        return False, "durable_identity_incomplete"

    @staticmethod
    def _metadata_row_values(
        catalogue: CatalogueService,
        metadata: MetadataRecord,
        filename: str,
        relative: str,
    ) -> dict[str, Any]:
        today = date.today().isoformat()
        values = {
            **metadata.catalogue_fields(),
            "date_added": today,
            "date_updated": today,
        }
        return {
            key: value
            for key, value in values.items()
            if key in catalogue.headers and value not in (None, "")
        }

    @staticmethod
    def _confirmed_value(record: CatalogueRecord, field_name: str) -> str:
        return confirmed_value(record.get("uncertainty"), field_name)

    def _commit_file_blockers(self, file_results: list[dict[str, Any]]) -> None:
        path = self.settings.state_dir / "inbox_blockers.json"
        blockers = []
        for item in file_results:
            if item.get("action") not in {"blocked", "provisional"} or not item.get("issue_keys"):
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
                    "paper_uuid": item.get("paper_uuid"),
                    "size": inspection.get("size"),
                    "mtime_ns": inspection.get("mtime_ns"),
                    "issue_keys": sorted(set(item.get("issue_keys") or [])),
                }
            )
        payload = {
            "version": 1,
            "files": sorted(blockers, key=lambda row: row["stable_file_id"]),
        }
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
