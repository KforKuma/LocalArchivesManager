from __future__ import annotations

from dataclasses import replace

from ..config import Settings
from ..models import (
    MetadataLookupRequest,
    MetadataLookupResult,
    ProviderResult,
    ProviderStats,
    ProviderStatus,
)
from ..providers.arxiv import ArxivProvider
from ..providers.base import MetadataLookupService, MetadataProvider
from ..providers.crossref import CrossrefProvider
from ..providers.pubmed import PubMedProvider
from ..providers.unavailable import UnavailableMetadataService
from ..providers.unpaywall import UnpaywallProvider
from .metadata_cache_service import MetadataCacheService
from .metadata_merge_service import MetadataMergeService


class CompositeMetadataLookupService:
    def __init__(
        self,
        settings: Settings,
        providers: dict[str, MetadataProvider] | None = None,
        merge_service: MetadataMergeService | None = None,
    ):
        cache = MetadataCacheService(settings.metadata_cache_dir, settings.cache)
        self.providers = providers or {
            "pubmed": PubMedProvider(settings, cache),
            "crossref": CrossrefProvider(settings, cache),
            "arxiv": ArxivProvider(settings, cache),
            "unpaywall": UnpaywallProvider(settings, cache),
        }
        self.merge_service = merge_service or MetadataMergeService()

    def lookup(self, request: MetadataLookupRequest) -> MetadataLookupResult:
        names = self._initial_providers(request)
        results: list[ProviderResult] = []
        if (
            request.provider == "auto"
            and request.title
            and not (request.pmid or request.doi or request.arxiv_id)
            and "crossref" in names
            and "crossref" in self.providers
        ):
            results.append(self.providers["crossref"].lookup(request))
            preliminary = self.merge_service.merge(request, results)
            if (
                preliminary.status.value == "found"
                and preliminary.confidence == "exact_title_supported"
            ):
                return preliminary
            names = [name for name in names if name != "crossref"]
        results.extend(
            self.providers[name].lookup(request)
            for name in names
            if name in self.providers
        )
        if request.provider == "auto":
            discovered_doi = next(
                (
                    record.doi
                    for result in results
                    for record in result.records
                    if record.doi
                ),
                "",
            )
            incomplete_bibliography = any(
                not (record.title and record.authors and record.year and record.journal)
                for result in results
                for record in result.records
                if record.doi == discovered_doi
            )
            if (
                discovered_doi
                and incomplete_bibliography
                and "crossref" not in names
                and "crossref" in self.providers
            ):
                enriched = replace(request, pmid=None, doi=discovered_doi)
                results.append(self.providers["crossref"].lookup(enriched))
            if discovered_doi and "unpaywall" not in names and "unpaywall" in self.providers:
                enriched = replace(request, pmid=None, doi=discovered_doi)
                results.append(self.providers["unpaywall"].lookup(enriched))
        return self.merge_service.merge(request, results)

    def lookup_many(
        self, requests: list[MetadataLookupRequest]
    ) -> list[MetadataLookupResult]:
        """Batch exact PubMed PMID retrieval; conservatively fall back otherwise."""
        if (
            requests
            and all(item.pmid and item.provider == "auto" and not item.offline for item in requests)
            and "pubmed" in self.providers
            and hasattr(self.providers["pubmed"], "fetch_pmids")
        ):
            batch = self.providers["pubmed"].fetch_pmids(
                [item.pmid for item in requests if item.pmid]
            )
            by_pmid = {record.pmid: record for record in batch.records if record.pmid}
            merged: list[MetadataLookupResult] = []
            for index, request in enumerate(requests):
                record = by_pmid.get(str(request.pmid))
                provider_result = ProviderResult(
                    "pubmed",
                    ProviderStatus.FOUND if record else (
                        batch.status if batch.status != ProviderStatus.FOUND else ProviderStatus.NOT_FOUND
                    ),
                    "pmid",
                    str(request.pmid),
                    records=[record] if record else [],
                    errors=list(batch.errors),
                    stats=batch.stats if index == 0 else ProviderStats(),
                )
                merged.append(self.merge_service.merge(request, [provider_result]))
            return merged
        return [self.lookup(request) for request in requests]

    @staticmethod
    def _initial_providers(request: MetadataLookupRequest) -> list[str]:
        if request.provider != "auto":
            return [request.provider]
        if request.pmid:
            return ["pubmed"]
        if request.arxiv_id:
            return ["arxiv"]
        if request.doi:
            return ["crossref", "unpaywall"]
        if request.title:
            return ["crossref", "pubmed", "arxiv"]
        return []

__all__ = [
    "CompositeMetadataLookupService",
    "MetadataLookupService",
    "UnavailableMetadataService",
]
