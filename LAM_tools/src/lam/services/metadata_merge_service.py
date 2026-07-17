from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
from typing import Iterable

try:
    from rapidfuzz.fuzz import ratio as title_ratio
except ImportError:  # pragma: no cover - package dependency supplies the fast path
    def title_ratio(left: str, right: str) -> float:
        return SequenceMatcher(None, left, right).ratio() * 100

from ..models import (
    MetadataConflict,
    MetadataLookupRequest,
    MetadataLookupResult,
    MetadataLookupStatus,
    MetadataRecord,
    ProviderResult,
    ProviderStatus,
)
from ..utils.identifiers import normalize_arxiv_id, normalize_doi, normalize_pmid
from ..utils.journal import journals_equivalent
from ..utils.text import normalize_title
from ..utils.title_matching import tolerant_title_score
from ..utils.publication_type import canonicalize_publication_type


PROVIDER_PRIORITY = {"pubmed": 0, "crossref": 1, "arxiv": 2, "unpaywall": 3}


class MetadataMergeService:
    def merge(
        self,
        request: MetadataLookupRequest,
        provider_results: Iterable[ProviderResult],
    ) -> MetadataLookupResult:
        results = list(provider_results)
        records = [record for result in results for record in result.records]
        if not records:
            statuses = {result.status for result in results}
            if ProviderStatus.NOT_FOUND in statuses:
                status = MetadataLookupStatus.NOT_FOUND
            elif ProviderStatus.FAILED in statuses:
                status = MetadataLookupStatus.FAILED
            else:
                status = MetadataLookupStatus.UNAVAILABLE
            return MetadataLookupResult(
                status=status,
                providers_used=[result.provider for result in results],
                errors=[error for result in results for error in result.errors],
                provider_results=results,
                selection_reason="No provider returned a usable candidate.",
            )

        candidates, hard_conflict = self._filter_by_request_identifiers(request, records)
        if hard_conflict:
            crossref = any(result.provider == "crossref" for result in results)
            conflict = MetadataConflict(
                "paper_identity",
                {record.canonical_id: self._identity(record) for record in records},
                "crossref_identifier_mismatch"
                if crossref
                else "metadata_identifier_conflict",
            )
            return self._blocked(
                MetadataLookupStatus.CONFLICT,
                results,
                records,
                "Provider candidates conflict with the requested identifier.",
                [conflict],
            )

        groups = self._identity_groups(candidates)
        if request.title and not (request.doi or request.pmid or request.arxiv_id):
            groups = self._title_groups(request, groups)
        if len(groups) != 1:
            return self._blocked(
                MetadataLookupStatus.AMBIGUOUS,
                results,
                candidates,
                "Multiple provider identities remain plausible.",
                [
                    MetadataConflict(
                        "paper_identity",
                        {record.canonical_id: self._identity(record) for record in candidates},
                        (
                            "crossref_query_ambiguous"
                            if any(result.provider == "crossref" for result in results)
                            else "metadata_query_ambiguous"
                        ),
                    )
                ],
            )

        group = groups[0]
        conflicts = self._identity_conflicts(group)
        if (
            any(result.provider == "crossref" for result in results)
            and any(
                item.blocking and item.field_name not in {"doi", "pmid"}
                for item in conflicts
            )
        ):
            conflicts.append(
                MetadataConflict(
                    "paper_identity",
                    {record.canonical_id: self._identity(record) for record in group},
                    "crossref_metadata_conflict",
                    True,
                )
            )
        if any(item.blocking for item in conflicts):
            return self._blocked(
                MetadataLookupStatus.CONFLICT,
                results,
                group,
                "Reliable identifiers conflict across providers.",
                conflicts,
            )
        merged = self._merge_group(group)
        type_result = merged.publication_type_result()
        for warning in type_result.warnings:
            conflicts.append(
                MetadataConflict(
                    "publication_type",
                    {
                        source: list(record.raw_publication_types)
                        for record in group
                        for source in record.source
                        if record.raw_publication_types
                    },
                    warning,
                    False,
                )
            )
        confidence, reason = self._confidence(request, group)
        return MetadataLookupResult(
            status=MetadataLookupStatus.FOUND,
            records=[record.to_dict() for record in group],
            best_record=merged.to_dict(),
            confidence=confidence,
            providers_used=list(merged.source),
            provider_results=results,
            selection_reason=reason,
            conflicts_detail=conflicts,
            conflicts=[item.issue_key for item in conflicts],
        )

    @staticmethod
    def _filter_by_request_identifiers(
        request: MetadataLookupRequest, records: list[MetadataRecord]
    ) -> tuple[list[MetadataRecord], bool]:
        checks = (
            (normalize_pmid(request.pmid), lambda item: item.pmid),
            (normalize_doi(request.doi), lambda item: item.doi),
            (normalize_arxiv_id(request.arxiv_id), lambda item: item.arxiv_id),
        )
        selected = records
        for expected, getter in checks:
            if not expected:
                continue
            exact = [item for item in selected if getter(item) == expected]
            with_value = [item for item in selected if getter(item)]
            if exact:
                if any(
                    getter(item) != expected
                    and any(MetadataMergeService._linked(item, match) for match in exact)
                    for item in with_value
                ):
                    return records, True
                selected = [
                    item
                    for item in selected
                    if getter(item) == expected
                    or (
                        not getter(item)
                        and any(
                            MetadataMergeService._linked(item, match)
                            for match in exact
                        )
                    )
                ]
            elif with_value:
                return records, True
        return selected, False

    @classmethod
    def _identity_groups(
        cls, records: list[MetadataRecord]
    ) -> list[list[MetadataRecord]]:
        groups: list[list[MetadataRecord]] = []
        for record in records:
            matching = [group for group in groups if any(cls._linked(record, item) for item in group)]
            if not matching:
                groups.append([record])
                continue
            primary = matching[0]
            primary.append(record)
            for extra in matching[1:]:
                primary.extend(extra)
                groups.remove(extra)
        return groups

    @staticmethod
    def _linked(left: MetadataRecord, right: MetadataRecord) -> bool:
        return any(
            first and second and first == second
            for first, second in (
                (left.pmid, right.pmid),
                (left.doi, right.doi),
                (left.arxiv_id, right.arxiv_id),
            )
        )

    @staticmethod
    def _title_groups(
        request: MetadataLookupRequest,
        groups: list[list[MetadataRecord]],
    ) -> list[list[MetadataRecord]]:
        query = normalize_title(request.title)
        plausible = []
        for group in groups:
            score = max(tolerant_title_score(query, item.title) for item in group)
            if score < 0.92:
                continue
            support_requested = bool(request.year or request.authors or request.journal)
            support_matches = False
            if request.year and str(request.year).isdigit():
                support_matches = support_matches or any(
                    item.year.isdigit()
                    and abs(int(item.year) - int(request.year)) <= 1
                    for item in group
                )
            if request.authors:
                query_author = MetadataMergeService._first_author(request.authors)
                support_matches = support_matches or bool(
                    query_author
                    and any(
                        MetadataMergeService._first_author("; ".join(item.authors))
                        == query_author
                        for item in group
                        if item.authors
                    )
                )
            if request.journal:
                support_matches = support_matches or any(
                    journals_equivalent(request.journal, item.journal)
                    or journals_equivalent(request.journal, item.journal_abbrev)
                    for item in group
                    if item.journal or item.journal_abbrev
                )
            if support_requested and not support_matches:
                continue
            plausible.append(group)
        return plausible

    @staticmethod
    def _identity_conflicts(group: list[MetadataRecord]) -> list[MetadataConflict]:
        conflicts: list[MetadataConflict] = []
        for field, issue in (
            ("doi", "metadata_identifier_conflict"),
            ("pmid", "metadata_identifier_conflict"),
        ):
            values = {
                source: value
                for record in group
                for source in record.source
                if (value := getattr(record, field))
            }
            if len(set(values.values())) > 1:
                conflicts.append(MetadataConflict(field, values, issue, True))
        titles = {
            source: record.title
            for record in group
            for source in record.source
            if record.title
        }
        if len(titles) > 1:
            values = list(titles.values())
            if min(
                title_ratio(normalize_title(values[0]), normalize_title(item))
                for item in values[1:]
            ) < 85:
                conflicts.append(
                    MetadataConflict("title", titles, "metadata_title_conflict", True)
                )
        return conflicts

    @classmethod
    def _merge_group(cls, group: list[MetadataRecord]) -> MetadataRecord:
        ordered = sorted(
            group,
            key=lambda record: min(
                (PROVIDER_PRIORITY.get(source, 99) for source in record.source),
                default=99,
            ),
        )
        merged = MetadataRecord()
        scalar_fields = (
            "title",
            "year",
            "journal",
            "journal_abbrev",
            "doi",
            "pmid",
            "arxiv_id",
            "abstract",
            "language",
            "published_date",
            "updated_date",
            "oa_status",
            "best_oa_url",
            "pdf_url",
            "landing_page_url",
        )
        for field in scalar_fields:
            for record in ordered:
                value = getattr(record, field)
                if value:
                    setattr(merged, field, deepcopy(value))
                    break
        for field in ("authors", "keywords", "mesh_terms", "categories"):
            values = []
            for record in ordered:
                values.extend(getattr(record, field))
            setattr(merged, field, list(dict.fromkeys(values)))
        raw_publication_types = []
        for record in ordered:
            raw_publication_types.extend(record.raw_publication_types)
            if record.publication_type:
                raw_publication_types.append(record.publication_type)
        type_result = canonicalize_publication_type(raw_publication_types)
        merged.raw_publication_types = list(type_result.raw_types)
        merged.publication_type = type_result.canonical_type
        merged.source = list(dict.fromkeys(source for record in ordered for source in record.source))
        merged.source_ids = {
            key: value for record in ordered for key, value in record.source_ids.items()
        }
        merged.provenance = [item for record in ordered for item in record.provenance]
        candidates = []
        seen_candidates: set[tuple[str, str]] = set()
        for record in ordered:
            for candidate in record.download_candidates:
                key = (candidate.provider, candidate.source_url)
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                candidates.append(deepcopy(candidate))
        merged.download_candidates = sorted(candidates, key=lambda item: item.priority)
        merged.is_preprint = any(record.is_preprint for record in ordered)
        merged.is_published = any(record.is_published for record in ordered)
        merged.canonical_id = (
            f"PMID:{merged.pmid}"
            if merged.pmid
            else f"DOI:{merged.doi}"
            if merged.doi
            else f"ARXIV:{merged.arxiv_id}"
            if merged.arxiv_id
            else ordered[0].canonical_id
        )
        return merged

    @staticmethod
    def _confidence(
        request: MetadataLookupRequest, group: list[MetadataRecord]
    ) -> tuple[str, str]:
        if request.pmid and any(item.pmid == normalize_pmid(request.pmid) for item in group):
            return "exact_identifier", "PMID matched exactly."
        if request.doi and any(item.doi == normalize_doi(request.doi) for item in group):
            return "exact_identifier", "DOI matched exactly."
        if request.arxiv_id and any(
            item.arxiv_id == normalize_arxiv_id(request.arxiv_id) for item in group
        ):
            return "exact_identifier", "arXiv identifier matched exactly."
        supporting = bool(request.year or request.authors or request.journal)
        return (
            ("exact_title_supported", "Normalized title has supporting metadata.")
            if supporting
            else ("exact_title_only", "Only an exact normalized title supports selection.")
        )

    @staticmethod
    def _blocked(
        status: MetadataLookupStatus,
        results: list[ProviderResult],
        records: list[MetadataRecord],
        reason: str,
        conflicts: list[MetadataConflict],
    ) -> MetadataLookupResult:
        return MetadataLookupResult(
            status=status,
            records=[record.to_dict() for record in records],
            confidence="conflict" if status == MetadataLookupStatus.CONFLICT else "ambiguous",
            providers_used=[result.provider for result in results],
            provider_results=results,
            selection_reason=reason,
            conflicts=[item.issue_key for item in conflicts],
            conflicts_detail=conflicts,
        )

    @staticmethod
    def _identity(record: MetadataRecord) -> dict[str, str]:
        return {
            "pmid": record.pmid,
            "doi": record.doi,
            "arxiv_id": record.arxiv_id,
            "title": record.title,
        }

    @staticmethod
    def _first_author(value: str) -> str:
        first = value.split(";", 1)[0].split(",", 1)[0].strip().casefold()
        parts = [part for part in first.replace(".", " ").split() if part]
        return parts[-1] if parts else ""
