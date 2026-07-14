from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from ..config import Settings
from ..exceptions import NetworkError, ProviderError
from ..http.client import HttpClient, HttpResult
from ..http.rate_limiter import RateLimiter
from ..http.retry import RetryPolicy
from ..models import (
    MetadataLookupRequest,
    MetadataRecord,
    ProviderResult,
    ProviderStats,
    ProviderStatus,
)
from ..services.metadata_cache_service import MetadataCacheService
from ..utils.identifiers import normalize_arxiv_id, normalize_doi
from ..utils.text import normalize_title
from .common import (
    cache_provider_result,
    offline_result,
    provenance,
    provider_result_from_cache,
)


ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"


class ArxivProvider:
    name = "arxiv"

    def __init__(
        self,
        settings: Settings,
        cache: MetadataCacheService,
        http_client: HttpClient | None = None,
    ):
        self.settings = settings
        self.config = settings.arxiv
        self.cache = cache
        self.http = http_client or HttpClient(
            self.name,
            settings.network,
            RateLimiter(self.config.min_interval_seconds),
            RetryPolicy(settings.network.max_retries, (30.0, 60.0, 120.0)),
        )

    def lookup(self, request: MetadataLookupRequest) -> ProviderResult:
        query_type, query_value, normalized = self._query(request)
        if not self.config.enabled:
            return ProviderResult(self.name, ProviderStatus.DISABLED, query_type, query_value)
        if not normalized:
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                query_type,
                query_value,
                errors=["arXiv query is missing or invalid."],
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
            params: dict[str, str] = {
                "start": "0",
                "max_results": str(max(1, min(request.max_results, 100))),
            }
            if query_type == "id":
                params["id_list"] = normalize_arxiv_id(query_value, keep_version=True)
            elif query_type == "doi":
                params["search_query"] = f'doi:"{normalized}"'
            else:
                params.update(
                    {
                        "search_query": f'ti:"{query_value.strip()}"',
                        "sortBy": "relevance",
                        "sortOrder": "descending",
                    }
                )
            response = self.http.get(self.config.base_url, params=params)
            self._add_http_stats(stats, response)
            if response.status_code >= 400:
                raise ProviderError(f"arXiv returned HTTP {response.status_code}")
            records = self.parse_atom(response.content)
            if query_type == "id":
                records = [record for record in records if record.arxiv_id == normalized]
            elif query_type == "doi":
                exact = [record for record in records if record.doi == normalized]
                records = exact or records
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
        except (ProviderError, ET.ParseError, ValueError) as exc:
            stats.parse_errors += 1
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                query_type,
                query_value,
                errors=[f"arXiv response parse failed: {type(exc).__name__}"],
                stats=stats,
            )

        ttl = (
            self.config.exact_ttl_seconds
            if query_type in {"id", "doi"}
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
        if request.arxiv_id:
            return "id", request.arxiv_id, normalize_arxiv_id(request.arxiv_id)
        if request.doi:
            return "doi", request.doi, normalize_doi(request.doi)
        if request.title:
            context = "|".join(
                (
                    normalize_title(request.title),
                    normalize_title(request.authors),
                    str(request.year or "").strip(),
                    str(request.max_results),
                )
            )
            return "title", request.title, context
        return "unknown", "", ""

    @staticmethod
    def _add_http_stats(stats: ProviderStats, response: HttpResult) -> None:
        stats.request_count += response.request_count
        stats.retries += response.retries
        stats.rate_limit_wait_seconds += response.rate_limit_wait_seconds

    @classmethod
    def parse_atom(cls, content: bytes) -> list[MetadataRecord]:
        root = ET.fromstring(content)
        retrieved = datetime.now().astimezone().isoformat(timespec="seconds")
        records: list[MetadataRecord] = []
        for entry in root.findall(f"{{{ATOM}}}entry"):
            raw_id = cls._text(entry.find(f"{{{ATOM}}}id")).rsplit("/", 1)[-1]
            arxiv_id = normalize_arxiv_id(raw_id)
            if not arxiv_id:
                continue
            authors = [
                cls._text(item.find(f"{{{ATOM}}}name"))
                for item in entry.findall(f"{{{ATOM}}}author")
            ]
            authors = [item for item in authors if item]
            categories = [
                (item.get("term") or "").strip()
                for item in entry.findall(f"{{{ATOM}}}category")
                if (item.get("term") or "").strip()
            ]
            primary = entry.find(f"{{{ARXIV}}}primary_category")
            primary_category = (primary.get("term") or "").strip() if primary is not None else ""
            if primary_category:
                categories = list(dict.fromkeys([primary_category, *categories]))
            published = cls._text(entry.find(f"{{{ATOM}}}published"))
            updated = cls._text(entry.find(f"{{{ATOM}}}updated"))
            pdf_url = ""
            landing = ""
            for link in entry.findall(f"{{{ATOM}}}link"):
                href = (link.get("href") or "").strip()
                if not href:
                    continue
                if (link.get("title") or "").casefold() == "pdf" or (
                    link.get("type") or ""
                ).casefold() == "application/pdf":
                    pdf_url = href
                elif (link.get("rel") or "").casefold() == "alternate":
                    landing = href
            record = MetadataRecord(
                canonical_id=f"ARXIV:{arxiv_id}",
                title=cls._text(entry.find(f"{{{ATOM}}}title")),
                authors=authors,
                year=published[:4] if re.match(r"\d{4}", published) else "",
                journal=cls._text(entry.find(f"{{{ARXIV}}}journal_ref")),
                doi=normalize_doi(cls._text(entry.find(f"{{{ARXIV}}}doi"))),
                arxiv_id=arxiv_id,
                abstract=cls._text(entry.find(f"{{{ATOM}}}summary")),
                categories=categories,
                published_date=published,
                updated_date=updated,
                source=["arxiv"],
                source_ids={"arxiv": arxiv_id, "arxiv_version": raw_id},
                is_preprint=True,
                is_published=bool(cls._text(entry.find(f"{{{ARXIV}}}journal_ref"))),
                pdf_url=pdf_url,
                landing_page_url=landing,
            )
            record.provenance = provenance(record, "arxiv", raw_id, retrieved)
            records.append(record)
        return records

    @staticmethod
    def _text(node: ET.Element | None) -> str:
        if node is None:
            return ""
        return re.sub(r"\s+", " ", "".join(node.itertext())).strip()
