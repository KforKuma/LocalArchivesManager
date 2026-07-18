from __future__ import annotations

import pytest

from lam.config import Settings
from lam.models import MetadataLookupRequest, ProviderStatus
from lam.providers.arxiv import ArxivProvider
from lam.providers.crossref import CrossrefProvider
from lam.providers.pubmed import PubMedProvider
from lam.providers.unpaywall import UnpaywallProvider
from lam.services.metadata_cache_service import MetadataCacheService


pytestmark = pytest.mark.live


def test_live_exact_provider_queries(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    cache = MetadataCacheService(settings.metadata_cache_dir, settings.cache)

    if not settings.pubmed.email:
        pytest.skip("NCBI_EMAIL is not configured")
    pubmed = PubMedProvider(settings, cache).lookup(
        MetadataLookupRequest(pmid="34265844", refresh=True)
    )
    assert pubmed.status == ProviderStatus.FOUND
    assert pubmed.records[0].pmid == "34265844"

    arxiv = ArxivProvider(settings, cache).lookup(
        MetadataLookupRequest(arxiv_id="1706.03762", refresh=True)
    )
    assert arxiv.status == ProviderStatus.FOUND
    assert arxiv.records[0].arxiv_id == "1706.03762"

    if not settings.unpaywall.email:
        pytest.skip("UNPAYWALL_EMAIL is not configured")
    unpaywall = UnpaywallProvider(settings, cache).lookup(
        MetadataLookupRequest(doi="10.1038/s41586-021-03819-2", refresh=True)
    )
    assert unpaywall.status == ProviderStatus.FOUND
    assert unpaywall.records[0].doi == "10.1038/s41586-021-03819-2"


def test_live_crossref_exact_query(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    cache = MetadataCacheService(settings.metadata_cache_dir, settings.cache)
    crossref = CrossrefProvider(settings, cache).lookup(
        MetadataLookupRequest(doi="10.1038/s41586-021-03819-2", refresh=True)
    )
    assert crossref.status == ProviderStatus.FOUND
    assert crossref.records[0].doi == "10.1038/s41586-021-03819-2"
