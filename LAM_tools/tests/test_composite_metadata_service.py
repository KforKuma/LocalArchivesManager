from __future__ import annotations

from lam.config import Settings
from lam.models import (
    MetadataLookupRequest,
    MetadataRecord,
    ProviderResult,
    ProviderStatus,
)
from lam.services.metadata_service import CompositeMetadataLookupService


class FakeProvider:
    def __init__(self, name, record=None, status=ProviderStatus.FOUND):
        self.name = name
        self.record = record
        self.status = status
        self.requests = []

    def lookup(self, request):
        self.requests.append(request)
        return ProviderResult(
            self.name,
            self.status,
            "test",
            request.doi or request.pmid or request.title or request.arxiv_id or "",
            records=[self.record] if self.record else [],
        )


class BatchPubMedProvider(FakeProvider):
    def __init__(self, records):
        super().__init__("pubmed")
        self.records = records
        self.batch_calls = []

    def fetch_pmids(self, pmids):
        self.batch_calls.append(list(pmids))
        return ProviderResult(
            "pubmed",
            ProviderStatus.FOUND,
            "pmid_batch",
            ",".join(pmids),
            records=self.records,
        )


def test_auto_pmid_queries_pubmed_then_enriches_doi_with_unpaywall(library_factory):
    root = library_factory([])
    pubmed_record = MetadataRecord(
        canonical_id="PMID:12345678",
        title="Paper",
        pmid="12345678",
        doi="10.1000/test",
        source=["pubmed"],
    )
    oa_record = MetadataRecord(
        canonical_id="DOI:10.1000/test",
        doi="10.1000/test",
        oa_status="green",
        source=["unpaywall"],
    )
    pubmed = FakeProvider("pubmed", pubmed_record)
    arxiv = FakeProvider("arxiv")
    unpaywall = FakeProvider("unpaywall", oa_record)
    service = CompositeMetadataLookupService(
        Settings.from_root(root),
        providers={"pubmed": pubmed, "arxiv": arxiv, "unpaywall": unpaywall},
    )
    merged = service.lookup(MetadataLookupRequest(pmid="12345678"))
    assert merged.best_record["oa_status"] == "green"
    assert len(pubmed.requests) == 1
    assert len(unpaywall.requests) == 1
    assert unpaywall.requests[0].doi == "10.1000/test"
    assert not arxiv.requests


def test_forced_provider_does_not_query_others(library_factory):
    root = library_factory([])
    record = MetadataRecord(
        canonical_id="ARXIV:2401.12345",
        arxiv_id="2401.12345",
        source=["arxiv"],
    )
    pubmed = FakeProvider("pubmed")
    arxiv = FakeProvider("arxiv", record)
    unpaywall = FakeProvider("unpaywall")
    service = CompositeMetadataLookupService(
        Settings.from_root(root),
        providers={"pubmed": pubmed, "arxiv": arxiv, "unpaywall": unpaywall},
    )
    service.lookup(
        MetadataLookupRequest(arxiv_id="2401.12345", provider="arxiv")
    )
    assert len(arxiv.requests) == 1
    assert not pubmed.requests
    assert not unpaywall.requests


def test_lookup_many_batches_exact_pubmed_pmids(library_factory):
    root = library_factory([])
    records = [
        MetadataRecord(
            canonical_id=f"PMID:{pmid}", pmid=pmid, title=f"Paper {pmid}", source=["pubmed"]
        )
        for pmid in ("12345678", "23456789")
    ]
    pubmed = BatchPubMedProvider(records)
    service = CompositeMetadataLookupService(
        Settings.from_root(root), providers={"pubmed": pubmed}
    )
    results = service.lookup_many(
        [
            MetadataLookupRequest(pmid="12345678"),
            MetadataLookupRequest(pmid="23456789"),
        ]
    )
    assert pubmed.batch_calls == [["12345678", "23456789"]]
    assert [item.best_record["pmid"] for item in results] == ["12345678", "23456789"]
