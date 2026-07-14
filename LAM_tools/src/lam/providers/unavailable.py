from __future__ import annotations

from ..models import MetadataLookupRequest, MetadataLookupResult, MetadataLookupStatus


class UnavailableMetadataService:
    def lookup(self, request: MetadataLookupRequest) -> MetadataLookupResult:
        return MetadataLookupResult(
            status=MetadataLookupStatus.UNAVAILABLE,
            providers_used=["unavailable"],
            errors=["Workflow 2 metadata lookup is not implemented in phase 2."],
        )
