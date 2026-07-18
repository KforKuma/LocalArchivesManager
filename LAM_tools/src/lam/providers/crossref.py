from __future__ import annotations

import html
import json
import re
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
from ..utils.text import normalize_title
from .common import (
    cache_provider_result,
    offline_result,
    provenance,
    provider_result_from_cache,
)


class CrossrefProvider:
    name = "crossref"

    def __init__(
        self,
        settings: Settings,
        cache: MetadataCacheService,
        http_client: HttpClient | None = None,
    ):
        self.settings = settings
        self.config = settings.crossref
        self.cache = cache
        self.http = http_client or HttpClient(
            self.name,
            settings.network,
            RateLimiter(self.config.min_interval_seconds),
            RetryPolicy(settings.network.max_retries),
        )

    def lookup(self, request: MetadataLookupRequest) -> ProviderResult:
        query_type, query_value, normalized = self._query(request)
        if not self.config.enabled:
            return ProviderResult(
                self.name, ProviderStatus.DISABLED, query_type, query_value
            )
        if not normalized:
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                query_type,
                query_value,
                errors=["Crossref query is missing or invalid."],
            )
        if not request.refresh:
            cached = self.cache.get(self.name, query_type, normalized)
            if cached:
                return provider_result_from_cache(
                    self.name, query_type, query_value, cached
                )
        if request.offline:
            return offline_result(self.name, query_type, query_value)

        stats = ProviderStats()
        try:
            if query_type == "doi":
                response = self.http.get(
                    f"{self.config.base_url}/works/{quote(normalized, safe='')}",
                    params=self._polite_params(),
                )
            else:
                params = {
                    **self._polite_params(),
                    "query.bibliographic": self._bibliographic_query(request),
                    "rows": str(
                        max(
                            1,
                            min(
                                request.max_results,
                                self.config.max_results,
                                100,
                            ),
                        )
                    ),
                    "select": (
                        "DOI,title,author,published,published-print,published-online,"
                        "issued,container-title,short-container-title,type,abstract,"
                        "subject,language,URL,ISSN,volume,issue,page"
                    ),
                }
                if request.authors:
                    params["query.author"] = request.authors
                response = self.http.get(
                    f"{self.config.base_url}/works", params=params
                )
            stats.request_count = response.request_count
            stats.retries = response.retries
            stats.rate_limit_wait_seconds = response.rate_limit_wait_seconds
            if response.status_code == 404:
                result = ProviderResult(
                    self.name,
                    ProviderStatus.NOT_FOUND,
                    query_type,
                    query_value,
                    stats=stats,
                )
            elif response.status_code >= 400:
                raise ProviderError(
                    f"Crossref returned HTTP {response.status_code}"
                )
            else:
                records = self.parse_json(response.content, query_type=query_type)
                if query_type == "doi":
                    exact = [item for item in records if item.doi == normalized]
                    if records and not exact:
                        return ProviderResult(
                            self.name,
                            ProviderStatus.FAILED,
                            query_type,
                            query_value,
                            records=records,
                            errors=["crossref_identifier_mismatch"],
                            stats=stats,
                        )
                    records = exact
                stats.records_returned = len(records)
                result = ProviderResult(
                    self.name,
                    ProviderStatus.FOUND if records else ProviderStatus.NOT_FOUND,
                    query_type,
                    query_value,
                    records=records,
                    stats=stats,
                )
        except NetworkError as exc:
            return ProviderResult(
                self.name,
                ProviderStatus.UNAVAILABLE,
                query_type,
                query_value,
                errors=[str(exc)],
                stats=stats,
            )
        except (ProviderError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            stats.parse_errors += 1
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                query_type,
                query_value,
                errors=[f"Crossref response parse failed: {type(exc).__name__}"],
                stats=stats,
            )

        ttl = (
            self.config.exact_ttl_seconds
            if query_type == "doi"
            else self.config.search_ttl_seconds
        )
        if result.status == ProviderStatus.NOT_FOUND:
            ttl = self.settings.cache.not_found_ttl_seconds
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
    def _query(request: MetadataLookupRequest) -> tuple[str, str, str]:
        if request.doi:
            return "doi", request.doi, normalize_doi(request.doi)
        if request.title:
            normalized = "|".join(
                (
                    normalize_title(request.title),
                    normalize_title(request.authors),
                    str(request.year or "").strip(),
                    normalize_title(request.journal),
                    str(request.max_results),
                )
            )
            return "bibliographic", request.title, normalized
        return "unknown", "", ""

    @staticmethod
    def _bibliographic_query(request: MetadataLookupRequest) -> str:
        return " ".join(
            item
            for item in (
                str(request.title or "").strip(),
                str(request.year or "").strip(),
                str(request.journal or "").strip(),
            )
            if item
        )

    def _polite_params(self) -> dict[str, str]:
        return {"mailto": self.config.email} if self.config.email else {}

    @classmethod
    def parse_json(cls, content: bytes, *, query_type: str) -> list[MetadataRecord]:
        payload = json.loads(content.decode("utf-8"))
        message = payload.get("message")
        raw_items = [message] if query_type == "doi" else (message or {}).get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("Crossref response does not contain records")
        retrieved = datetime.now().astimezone().isoformat(timespec="seconds")
        records: list[MetadataRecord] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            doi = normalize_doi(item.get("DOI"))
            title = cls._first(item.get("title"))
            if not doi or not title:
                continue
            authors = []
            for author in item.get("author") or []:
                if not isinstance(author, dict):
                    continue
                name = " ".join(
                    part
                    for part in (
                        str(author.get("given") or "").strip(),
                        str(author.get("family") or "").strip(),
                        str(author.get("name") or "").strip(),
                    )
                    if part
                )
                if name:
                    authors.append(name)
            raw_type = str(item.get("type") or "").strip()
            type_result = canonicalize_publication_type([raw_type])
            record = MetadataRecord(
                canonical_id=f"DOI:{doi}",
                title=title,
                authors=authors,
                year=cls._year(item),
                journal=cls._first(item.get("container-title")),
                journal_abbrev=cls._first(item.get("short-container-title")),
                doi=doi,
                publication_type=type_result.canonical_type,
                raw_publication_types=[raw_type] if raw_type else [],
                abstract=cls._clean_markup(item.get("abstract")),
                keywords=[
                    str(value).strip()
                    for value in item.get("subject") or []
                    if str(value).strip()
                ],
                language=str(item.get("language") or "").strip(),
                published_date=cls._date(item),
                source=["crossref"],
                source_ids={"crossref": doi},
                is_published=True,
                landing_page_url=str(item.get("URL") or "").strip(),
            )
            record.provenance = provenance(record, "crossref", doi, retrieved)
            license_url = cls._first(
                [
                    value.get("URL")
                    for value in item.get("license") or []
                    if isinstance(value, dict) and value.get("URL")
                ]
            )
            for index, link in enumerate(item.get("link") or []):
                if not isinstance(link, dict):
                    continue
                url = str(link.get("URL") or "").strip()
                content_type = str(link.get("content-type") or "").casefold()
                if not url or content_type != "application/pdf":
                    continue
                record.download_candidates.append(
                    DownloadCandidate(
                        provider="crossref",
                        source_url=url,
                        landing_page_url=record.landing_page_url,
                        expected_doi=doi,
                        host_type="publisher_or_depositor",
                        license=license_url,
                        version=str(link.get("content-version") or "").strip(),
                        is_direct_pdf=True,
                        priority=30 + index,
                        selection_reason="Crossref member-submitted application/pdf link",
                    )
                )
            records.append(record)
        return records

    @classmethod
    def _date(cls, item: dict) -> str:
        for key in ("published-print", "published-online", "published", "issued"):
            parts = ((item.get(key) or {}).get("date-parts") or [])
            if parts and isinstance(parts[0], list) and parts[0]:
                values = [str(value) for value in parts[0][:3]]
                return "-".join(
                    value.zfill(2) if index else value
                    for index, value in enumerate(values)
                )
        return ""

    @classmethod
    def _year(cls, item: dict) -> str:
        value = cls._date(item)
        return value[:4] if re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", value) else ""

    @staticmethod
    def _first(value: object) -> str:
        if isinstance(value, list):
            return str(value[0] or "").strip() if value else ""
        return str(value or "").strip()

    @staticmethod
    def _clean_markup(value: object) -> str:
        text = re.sub(r"<[^>]+>", " ", str(value or ""))
        return re.sub(r"\s+", " ", html.unescape(text)).strip()
