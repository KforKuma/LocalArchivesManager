from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Iterable

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
from ..utils.identifiers import normalize_doi, normalize_pmid
from ..utils.publication_type import canonicalize_publication_type
from ..utils.text import normalize_title
from .common import (
    cache_provider_result,
    offline_result,
    provenance,
    provider_result_from_cache,
)


class PubMedProvider:
    name = "pubmed"

    def __init__(
        self,
        settings: Settings,
        cache: MetadataCacheService,
        http_client: HttpClient | None = None,
    ):
        self.settings = settings
        self.config = settings.pubmed
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
            return ProviderResult(self.name, ProviderStatus.DISABLED, query_type, query_value)
        if not normalized:
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                query_type,
                query_value,
                errors=["PubMed query is missing or invalid."],
            )
        if not request.refresh:
            cached = self.cache.get(self.name, query_type, normalized)
            if cached:
                return provider_result_from_cache(
                    self.name, query_type, query_value, cached
                )
        if request.offline:
            return offline_result(self.name, query_type, query_value)
        if not self.config.email:
            return ProviderResult(
                self.name,
                ProviderStatus.UNAVAILABLE,
                query_type,
                query_value,
                errors=["NCBI_EMAIL is required for PubMed requests."],
            )

        stats = ProviderStats()
        raw = b""
        http_status: int | None = None
        try:
            if query_type == "pmid":
                fetch = self._efetch([normalized])
                self._add_http_stats(stats, fetch)
                raw, http_status = fetch.content, fetch.status_code
                self._require_success(fetch, "EFetch")
                records, parse_errors = self.parse_pubmed_xml(fetch.content)
            else:
                term = (
                    f'"{normalized}"[AID]'
                    if query_type == "doi"
                    else f'"{query_value.strip()}"[Title]'
                )
                search = self._esearch(term, request.max_results)
                self._add_http_stats(stats, search)
                http_status = search.status_code
                self._require_success(search, "ESearch")
                ids = self._parse_esearch(search.content)
                if not ids:
                    result = ProviderResult(
                        self.name,
                        ProviderStatus.NOT_FOUND,
                        query_type,
                        query_value,
                        stats=stats,
                    )
                    cache_provider_result(
                        self.cache,
                        result,
                        normalized,
                        ttl_seconds=self.settings.cache.not_found_ttl_seconds,
                        http_status=search.status_code,
                        raw_response=search.content,
                        cache_write=request.cache_write,
                    )
                    return result
                fetch = self._efetch(ids)
                self._add_http_stats(stats, fetch)
                raw, http_status = fetch.content, fetch.status_code
                self._require_success(fetch, "EFetch")
                records, parse_errors = self.parse_pubmed_xml(fetch.content)

            stats.parse_errors += len(parse_errors)
            if query_type == "pmid":
                records = [record for record in records if record.pmid == normalized]
            elif query_type == "doi":
                exact = [record for record in records if record.doi == normalized]
                records = exact or records
            stats.records_returned = len(records)
            status = ProviderStatus.FOUND if records else ProviderStatus.NOT_FOUND
            result = ProviderResult(
                self.name,
                status,
                query_type,
                query_value,
                records=records,
                errors=parse_errors,
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
        except (ProviderError, ET.ParseError, json.JSONDecodeError, KeyError, ValueError) as exc:
            stats.parse_errors += 1
            return ProviderResult(
                self.name,
                ProviderStatus.FAILED,
                query_type,
                query_value,
                errors=[f"PubMed response parse failed: {type(exc).__name__}"],
                stats=stats,
            )

        ttl = (
            self.config.exact_ttl_seconds
            if query_type in {"pmid", "doi"}
            else self.config.search_ttl_seconds
        )
        if result.status == ProviderStatus.NOT_FOUND:
            ttl = self.settings.cache.not_found_ttl_seconds
        cache_provider_result(
            self.cache,
            result,
            normalized,
            ttl_seconds=ttl,
            http_status=http_status,
            raw_response=raw,
            cache_write=request.cache_write,
        )
        return result

    def fetch_pmids(
        self, pmids: Iterable[str], *, offline: bool = False
    ) -> ProviderResult:
        valid = list(dict.fromkeys(normalize_pmid(item) for item in pmids))
        valid = [item for item in valid if item]
        if not valid:
            return ProviderResult(
                self.name, ProviderStatus.NOT_FOUND, "pmid_batch", ""
            )
        if offline:
            return offline_result(self.name, "pmid_batch", ",".join(valid))
        stats = ProviderStats()
        records: list[MetadataRecord] = []
        errors: list[str] = []
        try:
            for start in range(0, len(valid), self.config.batch_size):
                response = self._efetch(valid[start : start + self.config.batch_size])
                self._add_http_stats(stats, response)
                self._require_success(response, "batch EFetch")
                parsed, parse_errors = self.parse_pubmed_xml(response.content)
                records.extend(parsed)
                errors.extend(parse_errors)
        except NetworkError as exc:
            return ProviderResult(
                self.name,
                ProviderStatus.UNAVAILABLE,
                "pmid_batch",
                ",".join(valid),
                records=records,
                errors=[*errors, str(exc)],
                stats=stats,
            )
        stats.records_returned = len(records)
        stats.parse_errors = len(errors)
        return ProviderResult(
            self.name,
            ProviderStatus.FOUND if records else ProviderStatus.NOT_FOUND,
            "pmid_batch",
            ",".join(valid),
            records=records,
            errors=errors,
            stats=stats,
        )

    def _query(self, request: MetadataLookupRequest) -> tuple[str, str, str]:
        if request.pmid:
            return "pmid", request.pmid, normalize_pmid(request.pmid)
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

    def _common_params(self) -> dict[str, str]:
        params = {"db": "pubmed", "tool": self.config.tool, "email": self.config.email}
        if self.config.api_key:
            params["api_key"] = self.config.api_key
        return params

    def _esearch(self, term: str, max_results: int) -> HttpResult:
        return self.http.get(
            f"{self.config.base_url}/esearch.fcgi",
            params={
                **self._common_params(),
                "term": term,
                "retmode": "json",
                "retmax": str(max(1, min(max_results, 100))),
            },
        )

    def _efetch(self, pmids: list[str]) -> HttpResult:
        return self.http.get(
            f"{self.config.base_url}/efetch.fcgi",
            params={
                **self._common_params(),
                "id": ",".join(pmids),
                "retmode": "xml",
            },
        )

    @staticmethod
    def _parse_esearch(content: bytes) -> list[str]:
        payload = json.loads(content.decode("utf-8"))
        return [str(item) for item in payload["esearchresult"].get("idlist", [])]

    @staticmethod
    def _require_success(response: HttpResult, operation: str) -> None:
        if response.status_code >= 400:
            raise ProviderError(f"PubMed {operation} returned HTTP {response.status_code}")

    @staticmethod
    def _add_http_stats(stats: ProviderStats, response: HttpResult) -> None:
        stats.request_count += response.request_count
        stats.retries += response.retries
        stats.rate_limit_wait_seconds += response.rate_limit_wait_seconds

    @classmethod
    def parse_pubmed_xml(
        cls, content: bytes
    ) -> tuple[list[MetadataRecord], list[str]]:
        root = ET.fromstring(content)
        records: list[MetadataRecord] = []
        errors: list[str] = []
        retrieved = datetime.now().astimezone().isoformat(timespec="seconds")
        for index, node in enumerate(root.findall(".//PubmedArticle"), start=1):
            try:
                citation = node.find("MedlineCitation")
                article = citation.find("Article") if citation is not None else None
                if citation is None or article is None:
                    raise ValueError("missing MedlineCitation/Article")
                pmid = cls._text(citation.find("PMID"))
                if not pmid:
                    raise ValueError("missing PMID")
                journal = article.find("Journal")
                pubdate = journal.find("JournalIssue/PubDate") if journal is not None else None
                year = cls._year(pubdate, article)
                authors: list[str] = []
                for author in article.findall("AuthorList/Author"):
                    collective = cls._text(author.find("CollectiveName"))
                    if collective:
                        authors.append(collective)
                        continue
                    last = cls._text(author.find("LastName"))
                    fore = cls._text(author.find("ForeName")) or cls._text(author.find("Initials"))
                    name = " ".join(part for part in (fore, last) if part)
                    if name:
                        authors.append(name)
                abstracts = []
                for abstract in article.findall("Abstract/AbstractText"):
                    text = cls._text(abstract)
                    label = (abstract.get("Label") or "").strip()
                    if text:
                        abstracts.append(f"{label}: {text}" if label else text)
                doi_values = []
                for item in node.findall("PubmedData/ArticleIdList/ArticleId"):
                    if (item.get("IdType") or "").casefold() == "doi":
                        value = normalize_doi(cls._text(item))
                        if value:
                            doi_values.append(value)
                for item in article.findall("ELocationID"):
                    if (item.get("EIdType") or "").casefold() == "doi":
                        value = normalize_doi(cls._text(item))
                        if value:
                            doi_values.append(value)
                doi_values = list(dict.fromkeys(doi_values))
                keywords = [
                    cls._text(item)
                    for item in citation.findall("KeywordList/Keyword")
                    if cls._text(item)
                ]
                mesh = [
                    cls._text(item.find("DescriptorName"))
                    for item in citation.findall("MeshHeadingList/MeshHeading")
                    if cls._text(item.find("DescriptorName"))
                ]
                types = [
                    cls._text(item)
                    for item in article.findall("PublicationTypeList/PublicationType")
                    if cls._text(item)
                ]
                canonical_type = canonicalize_publication_type(types)
                record = MetadataRecord(
                    canonical_id=f"PMID:{pmid}",
                    title=cls._text(article.find("ArticleTitle")),
                    authors=authors,
                    year=year,
                    journal=cls._text(journal.find("Title")) if journal is not None else "",
                    journal_abbrev=(
                        cls._text(journal.find("ISOAbbreviation")) if journal is not None else ""
                    ),
                    doi=doi_values[0] if doi_values else "",
                    pmid=pmid,
                    publication_type=canonical_type.canonical_type,
                    raw_publication_types=types,
                    abstract="\n\n".join(abstracts),
                    keywords=list(dict.fromkeys(keywords)),
                    mesh_terms=list(dict.fromkeys(mesh)),
                    language=cls._text(article.find("Language")),
                    published_date=cls._date(pubdate, year),
                    source=["pubmed"],
                    source_ids={"pubmed": pmid},
                    is_published=True,
                )
                record.provenance = provenance(record, "pubmed", pmid, retrieved)
                records.append(record)
                if len(doi_values) > 1:
                    errors.append(f"PMID {pmid} contains multiple DOI values")
            except Exception as exc:
                errors.append(f"PubMed record {index} parse failed: {type(exc).__name__}")
        return records, errors

    @staticmethod
    def _text(node: ET.Element | None) -> str:
        if node is None:
            return ""
        return re.sub(r"\s+", " ", "".join(node.itertext())).strip()

    @classmethod
    def _year(cls, pubdate: ET.Element | None, article: ET.Element) -> str:
        if pubdate is not None:
            direct = cls._text(pubdate.find("Year"))
            if re.fullmatch(r"(?:19|20)\d{2}", direct):
                return direct
            match = re.search(r"\b(?:19|20)\d{2}\b", cls._text(pubdate.find("MedlineDate")))
            if match:
                return match.group(0)
        article_year = cls._text(article.find("ArticleDate/Year"))
        return article_year if re.fullmatch(r"(?:19|20)\d{2}", article_year) else ""

    @classmethod
    def _date(cls, pubdate: ET.Element | None, year: str) -> str:
        if pubdate is None or not year:
            return year
        month = cls._text(pubdate.find("Month"))
        day = cls._text(pubdate.find("Day"))
        return "-".join(part for part in (year, month, day) if part)
