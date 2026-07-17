from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..models import CatalogueRecord, MetadataRecord
from ..utils.identifiers import normalize_arxiv_id, normalize_doi, normalize_pmid
from ..utils.journal import journals_equivalent
from ..utils.normalize import normalized_text
from ..utils.publication_type import canonicalize_publication_type
from ..utils.title_matching import titles_tolerantly_equivalent
from ..utils.uncertainty import has_user_confirmation
from .catalogue_service import CatalogueService


CANONICAL_PROVIDER_PRIORITY = ("pubmed", "crossref", "arxiv", "unpaywall")


@dataclass(slots=True)
class CanonicalizationResult:
    paper_uuid: str = ""
    canonical_source: str = ""
    changed_fields: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.conflicts


class RegisteredRecordCanonicalizer:
    """Canonicalize a catalogue row after one durable provider identity is accepted."""

    def canonicalize(
        self,
        catalogue: CatalogueService,
        record: CatalogueRecord,
        metadata: MetadataRecord,
        *,
        merged: MetadataRecord | None = None,
    ) -> CanonicalizationResult:
        merged = merged or metadata
        conflicts = self._conflicts(record, metadata)
        if conflicts:
            return CanonicalizationResult(
                paper_uuid=str(record.get("paper_uuid") or ""),
                canonical_source=str(record.get("source") or ""),
                conflicts=conflicts,
            )

        canonical_source = self.canonical_source(metadata, record)
        if self._duplicates_external_identity(catalogue, record, metadata, merged):
            return CanonicalizationResult(
                paper_uuid=str(record.get("paper_uuid") or ""),
                canonical_source=str(record.get("source") or ""),
                conflicts=["external_identifier_conflict"],
            )
        updates = self._updates(catalogue, record, metadata, merged)
        if canonical_source and "source" in catalogue.headers:
            updates["source"] = canonical_source
        if "uncertainty" in catalogue.headers:
            updates["uncertainty"] = self._resolved_uncertainty(record)
        if "date_updated" in catalogue.headers:
            updates["date_updated"] = date.today().isoformat()

        paper_uuid = catalogue.ensure_paper_uuid(record)
        changes = catalogue.update_canonical_fields(record, updates)
        return CanonicalizationResult(
            paper_uuid=paper_uuid,
            canonical_source=str(record.get("source") or canonical_source),
            changed_fields=[change.field_name for change in changes],
        )

    def conflicts(
        self, record: CatalogueRecord | None, metadata: MetadataRecord
    ) -> list[str]:
        return self._conflicts(record, metadata) if record is not None else []

    @staticmethod
    def canonical_source(metadata: MetadataRecord, record: CatalogueRecord | None = None) -> str:
        sources = {
            normalized_text(item).replace(" ", "_")
            for item in metadata.source
            if normalized_text(item)
        }
        for provider in CANONICAL_PROVIDER_PRIORITY:
            if provider in sources:
                return provider
        current = str(record.get("source") or "") if record else ""
        current_sources = {
            normalized_text(item).replace(" ", "_")
            for item in re.split(r"\s*;\s*", current)
            if normalized_text(item)
        }
        for provider in CANONICAL_PROVIDER_PRIORITY:
            if provider in current_sources:
                return provider
        return "local_pdf" if "local_pdf" in current_sources or not sources else sorted(sources)[0]

    def _updates(
        self,
        catalogue: CatalogueService,
        record: CatalogueRecord,
        metadata: MetadataRecord,
        merged: MetadataRecord,
    ) -> dict[str, Any]:
        provider = metadata.catalogue_fields()
        combined = merged.catalogue_fields()
        updates: dict[str, Any] = {}
        provisional = self._is_provisional(record)
        for field_name in (
            "title",
            "authors",
            "year",
            "doi",
            "pmid",
            "arxiv_id",
            "abstract",
            "keywords",
            "auto_tags",
        ):
            if field_name not in catalogue.headers:
                continue
            value = provider.get(field_name) or combined.get(field_name)
            if value in (None, ""):
                continue
            current = record.get(field_name)
            provisional_machine_value = (
                provisional
                and field_name in {"title", "authors", "year"}
                and not has_user_confirmation(record.get("uncertainty"), field_name)
            )
            invalid_provisional_identifier = bool(
                provisional
                and field_name == "doi"
                and current not in (None, "")
                and not normalize_doi(current)
                and not has_user_confirmation(record.get("uncertainty"), field_name)
            )
            if (
                current in (None, "")
                or self._equivalent(field_name, current, value)
                or provisional_machine_value
                or invalid_provisional_identifier
            ):
                updates[field_name] = value

        publication = metadata.publication_type_result().canonical_type
        if publication is None:
            publication = canonicalize_publication_type(
                combined.get("publication_type") or record.get("publication_type")
            ).canonical_type
        if "publication_type" in catalogue.headers and publication not in (None, ""):
            updates["publication_type"] = publication

        provider_journal = metadata.journal or combined.get("journal")
        provider_abbrev = metadata.journal_abbrev or combined.get("journal_abbrev")
        if "journal" in catalogue.headers and provider_journal:
            updates["journal"] = provider_journal
        if "journal_abbrev" in catalogue.headers and provider_abbrev:
            updates["journal_abbrev"] = provider_abbrev
        return updates

    def _conflicts(self, record: CatalogueRecord, metadata: MetadataRecord) -> list[str]:
        conflicts: list[str] = []
        provisional = self._is_provisional(record)
        current_pmid = normalize_pmid(record.get("pmid"))
        provider_pmid = normalize_pmid(metadata.pmid)
        if current_pmid and provider_pmid and current_pmid != provider_pmid:
            conflicts.append("metadata_pmid_conflict")
        current_doi = normalize_doi(record.get("doi"))
        provider_doi = normalize_doi(metadata.doi)
        if current_doi and provider_doi and current_doi != provider_doi:
            conflicts.append("metadata_doi_conflict")
        current_arxiv = normalize_arxiv_id(record.get("arxiv_id"))
        provider_arxiv = normalize_arxiv_id(metadata.arxiv_id)
        if current_arxiv and provider_arxiv and current_arxiv != provider_arxiv:
            conflicts.append("metadata_arxiv_id_conflict")

        for field_name, provider_value in (
            ("title", metadata.title),
            ("authors", "; ".join(metadata.authors)),
            ("year", metadata.year),
        ):
            current = record.get(field_name)
            if current not in (None, "") and provider_value not in (None, ""):
                if provisional and not has_user_confirmation(
                    record.get("uncertainty"), field_name
                ):
                    continue
                if not self._equivalent(field_name, current, provider_value):
                    conflicts.append(f"metadata_{field_name}_conflict")

        current_journal = str(record.get("journal") or "")
        current_abbrev = str(record.get("journal_abbrev") or "")
        provider_journal = str(metadata.journal or "")
        provider_abbrev = str(metadata.journal_abbrev or "")
        for current in (current_journal, current_abbrev):
            if not current or not (provider_journal or provider_abbrev):
                continue
            if not any(
                journals_equivalent(current, candidate)
                for candidate in (provider_journal, provider_abbrev)
                if candidate
            ):
                conflicts.append("metadata_journal_conflict")
                break
        return list(dict.fromkeys(conflicts))

    @staticmethod
    def _duplicates_external_identity(
        catalogue: CatalogueService,
        record: CatalogueRecord,
        metadata: MetadataRecord,
        merged: MetadataRecord,
    ) -> bool:
        candidates = {
            "pmid": normalize_pmid(metadata.pmid or merged.pmid),
            "doi": normalize_doi(metadata.doi or merged.doi),
            "arxiv_id": normalize_arxiv_id(metadata.arxiv_id or merged.arxiv_id),
        }
        return any(
            value
            and any(item.row_number != record.row_number for item in catalogue.find_by(field, value))
            for field, value in candidates.items()
        )

    @staticmethod
    def _is_provisional(record: CatalogueRecord) -> bool:
        if normalized_text(record.get("source")) == "local_pdf":
            return True
        return any(
            line.lstrip().upper().startswith("NEEDS_REVIEW:")
            and "field=paper_identity" in normalized_text(line)
            for line in str(record.get("uncertainty") or "").splitlines()
        )

    @staticmethod
    def _equivalent(field_name: str, left: Any, right: Any) -> bool:
        if field_name == "doi":
            return normalize_doi(left) == normalize_doi(right)
        if field_name == "pmid":
            return normalize_pmid(left) == normalize_pmid(right)
        if field_name == "arxiv_id":
            return normalize_arxiv_id(left) == normalize_arxiv_id(right)
        if field_name == "title":
            return titles_tolerantly_equivalent(left, right)
        if field_name == "authors":
            normalize_authors = lambda value: [
                normalized_text(item)
                for item in re.split(r"\s*[;,]\s*", str(value or ""))
                if normalized_text(item)
            ]
            return normalize_authors(left) == normalize_authors(right)
        return normalized_text(left) == normalized_text(right)

    @staticmethod
    def _resolved_uncertainty(record: CatalogueRecord) -> str:
        retained: list[str] = []
        for line in str(record.get("uncertainty") or "").splitlines():
            normalized = normalized_text(line)
            upper = line.lstrip().upper()
            if upper.startswith("NEEDS_REVIEW:") and any(
                marker in normalized
                for marker in (
                    "field=paper_identity",
                    "issue_key=metadata_identity_unconfirmed",
                    "issue_key=metadata_journal_conflict",
                    "issue_key=catalogue_existing_value_conflict",
                )
            ):
                continue
            if upper.startswith("MACHINE_NOTE:") and any(
                marker in normalized
                for marker in (
                    "issue_key=journal_name_variant",
                    "title_provisional_",
                    "issue_key=local_pdf_metadata_used",
                )
            ):
                continue
            if line.strip():
                retained.append(line.rstrip())
        return "\n".join(retained)
