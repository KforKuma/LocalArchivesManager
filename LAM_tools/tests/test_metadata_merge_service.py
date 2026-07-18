from __future__ import annotations

from lam.models import (
    MetadataLookupRequest,
    MetadataLookupStatus,
    MetadataRecord,
    ProviderResult,
    ProviderStatus,
)
from lam.services.metadata_merge_service import MetadataMergeService


def result(provider, *records):
    return ProviderResult(
        provider,
        ProviderStatus.FOUND,
        "doi",
        "10.1000/test",
        records=list(records),
    )


def test_pubmed_and_unpaywall_same_doi_merge_with_field_priority():
    pubmed = MetadataRecord(
        canonical_id="PMID:12345678",
        title="A Biomedical Paper",
        authors=["Alice Smith"],
        year="2025",
        journal="Biomedical Journal",
        doi="10.1000/test",
        pmid="12345678",
        abstract="PubMed abstract",
        source=["pubmed"],
    )
    unpaywall = MetadataRecord(
        canonical_id="DOI:10.1000/test",
        title="A Biomedical Paper",
        year="2025",
        doi="10.1000/test",
        oa_status="gold",
        pdf_url="https://example.org/open.pdf",
        source=["unpaywall"],
    )
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(doi="10.1000/test"),
        [result("pubmed", pubmed), result("unpaywall", unpaywall)],
    )
    assert merged.status == MetadataLookupStatus.FOUND
    assert merged.confidence == "exact_identifier"
    assert merged.best_record["abstract"] == "PubMed abstract"
    assert merged.best_record["oa_status"] == "gold"
    assert merged.best_record["source"] == ["pubmed", "unpaywall"]


def test_doi_conflict_blocks_merge():
    first = MetadataRecord(
        canonical_id="PMID:1", doi="10.1000/one", pmid="12345678", source=["pubmed"]
    )
    second = MetadataRecord(
        canonical_id="PMID:1", doi="10.1000/two", pmid="12345678", source=["arxiv"]
    )
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(pmid="12345678"),
        [result("pubmed", first), result("arxiv", second)],
    )
    assert merged.status == MetadataLookupStatus.CONFLICT
    assert "metadata_identifier_conflict" in merged.conflicts


def test_similar_preprint_without_shared_identifier_is_not_auto_merged():
    published = MetadataRecord(
        canonical_id="PMID:12345678",
        title="A shared paper title",
        authors=["Alice Smith"],
        year="2025",
        pmid="12345678",
        source=["pubmed"],
    )
    preprint = MetadataRecord(
        canonical_id="ARXIV:2401.12345",
        title="A shared paper title",
        authors=["Alice Smith"],
        year="2024",
        arxiv_id="2401.12345",
        source=["arxiv"],
    )
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(
            title="A shared paper title", authors="Alice Smith", year="2025"
        ),
        [result("pubmed", published), result("arxiv", preprint)],
    )
    assert merged.status == MetadataLookupStatus.AMBIGUOUS


def test_unique_title_with_support_can_be_selected():
    record = MetadataRecord(
        canonical_id="PMID:12345678",
        title="Exact supported title",
        authors=["Alice Smith"],
        year="2025",
        pmid="12345678",
        source=["pubmed"],
    )
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(
            title="Exact supported title", authors="A. Smith", year="2025"
        ),
        [result("pubmed", record)],
    )
    assert merged.status == MetadataLookupStatus.FOUND
    assert merged.confidence == "exact_title_supported"


def test_not_found_plus_provider_unavailable_is_not_claimed_as_conclusive_not_found():
    not_found = ProviderResult(
        "pubmed", ProviderStatus.NOT_FOUND, "title", "missing"
    )
    unavailable = ProviderResult(
        "arxiv", ProviderStatus.UNAVAILABLE, "title", "missing"
    )
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(title="missing"), [not_found, unavailable]
    )
    assert merged.status == MetadataLookupStatus.UNAVAILABLE


def test_all_queried_providers_returning_zero_results_is_not_found():
    results = [
        ProviderResult(name, ProviderStatus.NOT_FOUND, "title", "missing")
        for name in ("crossref", "pubmed", "arxiv")
    ]
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(title="missing"), results
    )
    assert merged.status == MetadataLookupStatus.NOT_FOUND
