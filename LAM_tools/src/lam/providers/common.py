from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import (
    MetadataLookupRequest,
    MetadataProvenance,
    MetadataRecord,
    ProviderResult,
    ProviderStats,
    ProviderStatus,
)
from ..services.metadata_cache_service import MetadataCacheService


def provider_result_from_cache(
    provider: str,
    query_type: str,
    query_value: str,
    payload: dict[str, Any],
) -> ProviderResult:
    parsed = payload["parsed_result"]
    return ProviderResult(
        provider=provider,
        status=ProviderStatus(parsed["status"]),
        query_type=query_type,
        query_value=query_value,
        records=[MetadataRecord.from_dict(item) for item in parsed.get("records", [])],
        errors=list(parsed.get("errors", [])),
        stats=ProviderStats(cache_hits=1, records_returned=len(parsed.get("records", []))),
        cache_hit=True,
    )


def cache_provider_result(
    cache: MetadataCacheService,
    result: ProviderResult,
    normalized_query: str,
    *,
    ttl_seconds: int,
    http_status: int | None,
    raw_response: bytes | None,
    cache_write: bool,
) -> None:
    if not cache_write or result.status in {
        ProviderStatus.FAILED,
        ProviderStatus.UNAVAILABLE,
        ProviderStatus.UNAVAILABLE_OFFLINE,
    }:
        return
    cache.put(
        result.provider,
        result.query_type,
        normalized_query,
        {
            "status": result.status.value,
            "records": [record.to_dict() for record in result.records],
            "errors": result.errors,
        },
        ttl_seconds=ttl_seconds,
        http_status=http_status,
        raw_response=raw_response,
    )


def provenance(
    record: MetadataRecord,
    provider: str,
    source_identifier: str,
    retrieved_at: str,
) -> list[MetadataProvenance]:
    fields = (
        "title",
        "authors",
        "year",
        "journal",
        "journal_abbrev",
        "doi",
        "pmid",
        "arxiv_id",
        "publication_type",
        "abstract",
        "keywords",
        "mesh_terms",
        "categories",
        "oa_status",
        "best_oa_url",
        "pdf_url",
    )
    return [
        MetadataProvenance(
            field_name=name,
            value=getattr(record, name),
            provider=provider,
            source_identifier=source_identifier,
            retrieved_at=retrieved_at,
        )
        for name in fields
        if getattr(record, name)
    ]


def offline_result(provider: str, query_type: str, query_value: str) -> ProviderResult:
    return ProviderResult(
        provider,
        ProviderStatus.UNAVAILABLE_OFFLINE,
        query_type,
        query_value,
        errors=["No valid cached result is available in offline mode."],
    )
