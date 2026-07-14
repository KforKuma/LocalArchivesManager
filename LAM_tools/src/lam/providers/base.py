from __future__ import annotations

from typing import Protocol

from ..models import MetadataLookupRequest, MetadataLookupResult


class MetadataLookupService(Protocol):
    def lookup(self, request: MetadataLookupRequest) -> MetadataLookupResult:
        """Return normalized metadata without mutating catalogue or files."""
