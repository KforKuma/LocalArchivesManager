from __future__ import annotations

from typing import Protocol

from ..models import MetadataLookupRequest, MetadataLookupResult, ProviderResult


class MetadataLookupService(Protocol):
    def lookup(self, request: MetadataLookupRequest) -> MetadataLookupResult:
        """Return normalized metadata without mutating catalogue or files."""


class MetadataProvider(Protocol):
    name: str

    def lookup(self, request: MetadataLookupRequest) -> ProviderResult:
        """Query one source and return normalized candidates only."""
