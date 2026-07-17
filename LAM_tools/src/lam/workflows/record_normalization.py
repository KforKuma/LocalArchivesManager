from __future__ import annotations

from ..config import Settings
from ..models import MetadataLookupRequest, WorkflowResult
from ..services.metadata_service import CompositeMetadataLookupService
from .metadata_query import MetadataQueryWorkflow


class RecordNormalizationWorkflow:
    """Exact-identifier catalogue migration without PDF movement."""

    def __init__(
        self,
        settings: Settings,
        metadata_service: CompositeMetadataLookupService | None = None,
    ):
        self.delegate = MetadataQueryWorkflow(
            settings,
            metadata_service=metadata_service,
        )

    def run(
        self,
        *,
        dry_run: bool = False,
        max_records: int = 1000,
        offline: bool = False,
        refresh: bool = False,
        cache_write: bool = True,
    ) -> WorkflowResult:
        return self.delegate.run(
            MetadataLookupRequest(
                offline=offline,
                refresh=refresh,
                cache_write=cache_write,
            ),
            dry_run=dry_run,
            normalize_existing=True,
            max_records=max_records,
            workflow_name="record_normalization",
            download=False,
        )
