from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

from ..config import Settings
from ..exceptions import NetworkError, ProviderError
from ..http.client import HttpClient
from ..http.rate_limiter import RateLimiter
from ..http.retry import RetryPolicy
from ..models import (
    DownloadCandidate,
    MetadataLookupRequest,
    MetadataRecord,
    ProviderResult,
    ProviderStats,
    ProviderStatus,
)
from ..services.metadata_cache_service import MetadataCacheService
from ..utils.identifiers import normalize_doi
from ..utils.publication_type import canonicalize_publication_type
from .common import (
    cache_provider_result,
    offline_result,
    provenance,
    provider_result_from_cache,
)


class UnpaywallProvider:
    name = "unpaywall"

    def __init__(
        self,
        settings: Settings,
        cache: MetadataCacheService,
        http_client: HttpClient | None = None,
    ):
        self.settings = settings
        self.config = settings.unpaywall
        self.cache = cache
        self.http = http_client or HttpClient(
            self.name,
            settings.network,
            RateLimiter(self.config.min_interval_seconds),
            RetryPolicy(settings.network.max_retries),
        )

    def lookup(self, request: MetadataLookupRequest) -> ProviderResult:
        query_value = request.doi or ""
        normalized = normalize_doi(query_value)
        if not self.config.enabled:
            return ProviderResult(self.name, ProviderStatus.DISABLED, "doi", query_value)
        if not normalized:
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                "doi",
                query_value,
                errors=["Unpaywall requires a valid DOI."],
            )
        if not request.refresh:
            cached = self.cache.get(self.name, "doi", normalized)
            if cached:
                return provider_result_from_cache(self.name, "doi", query_value, cached)
        if request.offline:
            return offline_result(self.name, "doi", query_value)
        if not self.config.email:
            return ProviderResult(
                self.name,
                ProviderStatus.UNAVAILABLE,
                "doi",
                query_value,
                errors=["UNPAYWALL_EMAIL is required for Unpaywall requests."],
            )
        if not self.cache.consume_daily_quota(
            self.name,
            self.config.daily_limit,
            persist=request.cache_write,
        ):
            return ProviderResult(
                self.name,
                ProviderStatus.UNAVAILABLE,
                "doi",
                query_value,
                errors=["Configured Unpaywall daily request limit has been reached."],
            )

        stats = ProviderStats()
        try:
            response = self.http.get(
                f"{self.config.base_url}/{quote(normalized, safe='')}",
                params={"email": self.config.email},
            )
            stats.request_count = response.request_count
            stats.retries = response.retries
            stats.rate_limit_wait_seconds = response.rate_limit_wait_seconds
            if response.status_code == 404:
                result = ProviderResult(
                    self.name,
                    ProviderStatus.NOT_FOUND,
                    "doi",
                    query_value,
                    stats=stats,
                )
            elif response.status_code >= 400:
                raise ProviderError(f"Unpaywall returned HTTP {response.status_code}")
            else:
                record = self.parse_json(response.content)
                if record.doi and record.doi != normalized:
                    raise ProviderError("Unpaywall DOI does not match the request")
                stats.records_returned = 1
                result = ProviderResult(
                    self.name,
                    ProviderStatus.FOUND,
                    "doi",
                    query_value,
                    records=[record],
                    stats=stats,
                )
        except NetworkError as exc:
            return ProviderResult(
                self.name,
                ProviderStatus.UNAVAILABLE,
                "doi",
                query_value,
                errors=[str(exc)],
                stats=stats,
            )
        except (ProviderError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            stats.parse_errors += 1
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                "doi",
                query_value,
                errors=[f"Unpaywall response parse failed: {type(exc).__name__}"],
                stats=stats,
            )

        ttl = (
            self.settings.cache.not_found_ttl_seconds
            if result.status == ProviderStatus.NOT_FOUND
            else self.config.ttl_seconds
        )
        cache_provider_result(
            self.cache,
            result,
            normalized,
            ttl_seconds=ttl,
            http_status=response.status_code,
            raw_response=response.content,
            cache_write=request.cache_write,
        )
        return result

    @staticmethod
    def parse_json(content: bytes) -> MetadataRecord:
        payload = json.loads(content.decode("utf-8"))
        doi = normalize_doi(payload.get("doi"))
        if not doi:
            raise ValueError("missing DOI")
        best = payload.get("best_oa_location") or {}
        locations = payload.get("oa_locations") or []
        if not isinstance(best, dict):
            best = {}
        if not isinstance(locations, list):
            locations = []
        pdf_url = str(best.get("url_for_pdf") or "").strip()
        best_url = str(best.get("url") or best.get("url_for_landing_page") or "").strip()
        authors = []
        for item in payload.get("z_authors") or []:
            if not isinstance(item, dict):
                continue
            name = " ".join(
                part
                for part in (str(item.get("given") or "").strip(), str(item.get("family") or "").strip())
                if part
            )
            if name:
                authors.append(name)
        raw_genres = [str(payload.get("genre") or "").strip()] if payload.get("genre") else []
        canonical_type = canonicalize_publication_type(raw_genres)
        record = MetadataRecord(
            canonical_id=f"DOI:{doi}",
            title=str(payload.get("title") or "").strip(),
            authors=authors,
            year=str(payload.get("year") or "").strip(),
            journal=str(payload.get("journal_name") or "").strip(),
            doi=doi,
            publication_type=canonical_type.canonical_type,
            raw_publication_types=raw_genres,
            source=["unpaywall"],
            source_ids={"unpaywall": doi},
            is_published=True,
            oa_status=str(payload.get("oa_status") or "").strip(),
            best_oa_url=best_url,
            pdf_url=pdf_url,
            landing_page_url=str(payload.get("doi_url") or "").strip(),
        )
        candidates: list[DownloadCandidate] = []
        seen_urls: set[str] = set()
        ordered_locations = [best, *locations]
        for index, location in enumerate(ordered_locations):
            if not isinstance(location, dict):
                continue
            direct_url = str(location.get("url_for_pdf") or "").strip()
            if not direct_url or direct_url in seen_urls:
                continue
            seen_urls.add(direct_url)
            candidates.append(
                DownloadCandidate(
                    provider="unpaywall",
                    source_url=direct_url,
                    landing_page_url=str(
                        location.get("url_for_landing_page") or location.get("url") or ""
                    ).strip(),
                    expected_doi=doi,
                    host_type=str(location.get("host_type") or "").strip(),
                    license=str(location.get("license") or "").strip(),
                    version=str(location.get("version") or "").strip(),
                    is_direct_pdf=True,
                    priority=20 + index,
                    selection_reason="Explicit Unpaywall url_for_pdf OA location.",
                )
            )
        record.download_candidates = candidates
        retrieved = datetime.now().astimezone().isoformat(timespec="seconds")
        record.provenance = provenance(record, "unpaywall", doi, retrieved)
        return record
