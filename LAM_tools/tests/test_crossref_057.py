from __future__ import annotations

import json
from dataclasses import replace

from lam.config import Settings
from lam.http.client import HttpResult
from lam.models import (
    MetadataLookupRequest,
    MetadataLookupStatus,
    MetadataRecord,
    ProviderResult,
    ProviderStatus,
)
from lam.providers.crossref import CrossrefProvider
from lam.services.metadata_cache_service import MetadataCacheService
from lam.services.metadata_service import CompositeMetadataLookupService


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


class FakeProvider:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.requests = []

    def lookup(self, request):
        self.requests.append(request)
        return self.result


def _response(payload, status=200):
    return HttpResult(status, json.dumps(payload).encode(), {}, 1, 0, 0.0)


def _item(doi="10.5555/example", title="A cross disciplinary paper"):
    return {
        "DOI": doi,
        "title": [title],
        "author": [
            {"given": "Alice", "family": "Smith"},
            {"given": "Bob", "family": "Jones"},
        ],
        "published-print": {"date-parts": [[2024, 3, 7]]},
        "container-title": ["Interdisciplinary Studies"],
        "short-container-title": ["Interdiscip Stud"],
        "type": "journal-article",
        "abstract": "<jats:p>Provider abstract.</jats:p>",
        "subject": ["Engineering", "Society"],
        "language": "en",
        "URL": f"https://doi.org/{doi}",
    }


def _configured(library_factory):
    root = library_factory([])
    base = Settings.from_root(root)
    settings = replace(
        base,
        crossref=replace(base.crossref, email="contact@example.org", max_results=5),
    )
    return settings, MetadataCacheService(settings.metadata_cache_dir, settings.cache)


def test_crossref_exact_doi_query_validates_parses_and_caches(library_factory):
    settings, cache = _configured(library_factory)
    fake = FakeHttp([_response({"status": "ok", "message": _item()})])
    provider = CrossrefProvider(settings, cache, fake)
    request = MetadataLookupRequest(doi="https://doi.org/10.5555/EXAMPLE")
    first = provider.lookup(request)
    second = provider.lookup(request)
    record = first.records[0]
    assert first.status == ProviderStatus.FOUND
    assert second.cache_hit is True
    assert record.doi == "10.5555/example"
    assert record.authors == ["Alice Smith", "Bob Jones"]
    assert record.year == "2024"
    assert record.journal_abbrev == "Interdiscip Stud"
    assert record.abstract == "Provider abstract."
    assert len(fake.calls) == 1
    assert fake.calls[0][0].endswith("/works/10.5555%2Fexample")
    assert fake.calls[0][1] == {"mailto": "contact@example.org"}


def test_crossref_bibliographic_query_propagates_title_author_year_and_limits(
    library_factory,
):
    settings, cache = _configured(library_factory)
    payload = {"status": "ok", "message": {"items": [_item()]}}
    fake = FakeHttp([_response(payload)])
    result = CrossrefProvider(settings, cache, fake).lookup(
        MetadataLookupRequest(
            title="A cross disciplinary paper",
            authors="Alice Smith",
            year="2024",
            journal="Interdisciplinary Studies",
            max_results=25,
        )
    )
    params = fake.calls[0][1]
    assert result.status == ProviderStatus.FOUND
    assert params["query.author"] == "Alice Smith"
    assert "2024" in params["query.bibliographic"]
    assert params["rows"] == "5"
    assert "language" not in params["select"].split(",")


def test_crossref_identifier_mismatch_is_rejected(library_factory):
    settings, cache = _configured(library_factory)
    fake = FakeHttp(
        [_response({"status": "ok", "message": _item(doi="10.5555/different")})]
    )
    result = CrossrefProvider(settings, cache, fake).lookup(
        MetadataLookupRequest(doi="10.5555/requested")
    )
    assert result.status == ProviderStatus.FAILED
    assert result.errors == ["crossref_identifier_mismatch"]


def test_crossref_title_results_are_merged_not_first_result_selected(
    library_factory,
):
    root = library_factory([])
    records = [
        MetadataRecord(
            canonical_id="DOI:10.1000/wrong",
            doi="10.1000/wrong",
            title="A cross disciplinary paper",
            authors=["Different Author"],
            year="2018",
            journal="Other Journal",
            source=["crossref"],
        ),
        MetadataRecord(
            canonical_id="DOI:10.1000/right",
            doi="10.1000/right",
            title="A cross disciplinary paper",
            authors=["Alice Smith"],
            year="2024",
            journal="Interdisciplinary Studies",
            source=["crossref"],
        ),
    ]
    crossref = FakeProvider(
        "crossref",
        ProviderResult(
            "crossref", ProviderStatus.FOUND, "bibliographic", "title", records=records
        ),
    )
    service = CompositeMetadataLookupService(
        Settings.from_root(root), providers={"crossref": crossref}
    )
    result = service.lookup(
        MetadataLookupRequest(
            title="A cross disciplinary paper", authors="Alice Smith", year="2024"
        )
    )
    assert result.status == MetadataLookupStatus.FOUND
    assert result.best_record["doi"] == "10.1000/right"


def test_crossref_ambiguous_equal_candidates_are_not_auto_selected(library_factory):
    root = library_factory([])
    records = [
        MetadataRecord(
            canonical_id=f"DOI:10.1000/{index}",
            doi=f"10.1000/{index}",
            title="Same exact title for two works",
            authors=["Alice Smith"],
            year="2024",
            source=["crossref"],
        )
        for index in (1, 2)
    ]
    crossref = FakeProvider(
        "crossref",
        ProviderResult(
            "crossref", ProviderStatus.FOUND, "bibliographic", "title", records=records
        ),
    )
    result = CompositeMetadataLookupService(
        Settings.from_root(root), providers={"crossref": crossref}
    ).lookup(
        MetadataLookupRequest(
            title="Same exact title for two works", authors="Alice Smith", year="2024"
        )
    )
    assert result.status == MetadataLookupStatus.AMBIGUOUS
    assert "crossref_query_ambiguous" in result.conflicts


def test_provider_candidates_rejected_by_existing_rules_are_ambiguous_not_not_found(
    library_factory,
):
    root = library_factory([])
    record = MetadataRecord(
        canonical_id="DOI:10.1000/different",
        doi="10.1000/different",
        title="A completely different paper",
        authors=["Different Author"],
        year="2010",
        journal="Other Journal",
        source=["crossref"],
    )
    crossref = FakeProvider(
        "crossref",
        ProviderResult(
            "crossref",
            ProviderStatus.FOUND,
            "bibliographic",
            "A deterministic reference import study",
            records=[record],
        ),
    )
    result = CompositeMetadataLookupService(
        Settings.from_root(root), providers={"crossref": crossref}
    ).lookup(
        MetadataLookupRequest(
            title="A deterministic reference import study",
            authors="Smith J",
            year="2024",
        )
    )
    assert result.status == MetadataLookupStatus.AMBIGUOUS
    assert "crossref_candidates_rejected" in result.conflicts
    assert result.candidate_evaluations[0]["accepted"] is False
    assert result.candidate_evaluations[0]["rejection_reasons"]


def test_mixed_provider_failure_and_zero_results_is_not_misreported_not_found(
    library_factory,
):
    root = library_factory([])
    providers = {
        "crossref": FakeProvider(
            "crossref",
            ProviderResult(
                "crossref",
                ProviderStatus.FAILED,
                "bibliographic",
                "A deterministic reference import study",
                errors=["Crossref returned HTTP 400"],
            ),
        ),
        "pubmed": FakeProvider(
            "pubmed",
            ProviderResult(
                "pubmed",
                ProviderStatus.NOT_FOUND,
                "title",
                "A deterministic reference import study",
            ),
        ),
        "arxiv": FakeProvider(
            "arxiv",
            ProviderResult(
                "arxiv",
                ProviderStatus.NOT_FOUND,
                "title",
                "A deterministic reference import study",
            ),
        ),
    }
    result = CompositeMetadataLookupService(
        Settings.from_root(root), providers=providers
    ).lookup(
        MetadataLookupRequest(
            title="A deterministic reference import study",
            authors="Smith J",
            year="2024",
        )
    )
    assert result.status == MetadataLookupStatus.FAILED


def test_missing_exact_identifier_falls_back_to_supported_title_query(library_factory):
    root = library_factory([])
    title = "A deterministic reference import study"
    record = MetadataRecord(
        canonical_id="DOI:10.1234/ref.one",
        doi="10.1234/ref.one",
        title=title,
        authors=["Smith J", "Doe A"],
        year="2024",
        journal="Journal of Test Systems",
        source=["crossref"],
    )

    class IdentifierThenTitle:
        name = "crossref"

        def __init__(self):
            self.requests = []

        def lookup(self, request):
            self.requests.append(request)
            if request.doi:
                return ProviderResult(
                    "crossref", ProviderStatus.NOT_FOUND, "doi", request.doi
                )
            return ProviderResult(
                "crossref",
                ProviderStatus.FOUND,
                "bibliographic",
                request.title or "",
                records=[record],
            )

    crossref = IdentifierThenTitle()
    unpaywall = FakeProvider(
        "unpaywall",
        ProviderResult(
            "unpaywall", ProviderStatus.NOT_FOUND, "doi", "10.1234/ref.one"
        ),
    )
    result = CompositeMetadataLookupService(
        Settings.from_root(root),
        providers={"crossref": crossref, "unpaywall": unpaywall},
    ).lookup(
        MetadataLookupRequest(
            doi="10.1234/ref.one",
            title=title,
            authors="Smith J, Doe A",
            year="2024",
            journal="Journal of Test Systems",
        )
    )
    assert result.status == MetadataLookupStatus.FOUND
    assert result.confidence == "exact_title_supported"
    assert [request.doi for request in crossref.requests] == [
        "10.1234/ref.one",
        None,
    ]


def test_crossref_refresh_bypasses_negative_title_cache(library_factory):
    settings, cache = _configured(library_factory)
    request = MetadataLookupRequest(
        title="A deterministic reference import study",
        authors="Smith J",
        year="2024",
        refresh=True,
    )
    query_type, _, normalized = CrossrefProvider._query(request)
    cache.put(
        "crossref",
        query_type,
        normalized,
        {"status": "not_found", "records": [], "errors": []},
        ttl_seconds=3600,
        http_status=200,
    )
    fake = FakeHttp(
        [
            _response(
                {
                    "status": "ok",
                    "message": {
                        "items": [
                            _item(
                                doi="10.1234/ref.one",
                                title="A deterministic reference import study",
                            )
                        ]
                    },
                }
            )
        ]
    )
    result = CrossrefProvider(settings, cache, fake).lookup(request)
    assert result.status == ProviderStatus.FOUND
    assert result.cache_hit is False
    assert len(fake.calls) == 1


def test_title_crossref_success_stops_fallback_providers(library_factory):
    root = library_factory([])
    record = MetadataRecord(
        canonical_id="DOI:10.1000/right",
        doi="10.1000/right",
        title="A cross disciplinary paper",
        authors=["Alice Smith"],
        year="2024",
        journal="Interdisciplinary Studies",
        source=["crossref"],
    )
    crossref = FakeProvider(
        "crossref",
        ProviderResult(
            "crossref", ProviderStatus.FOUND, "bibliographic", "title", records=[record]
        ),
    )
    pubmed = FakeProvider(
        "pubmed", ProviderResult("pubmed", ProviderStatus.NOT_FOUND, "title", "title")
    )
    service = CompositeMetadataLookupService(
        Settings.from_root(root), providers={"crossref": crossref, "pubmed": pubmed}
    )
    result = service.lookup(
        MetadataLookupRequest(
            title="A cross disciplinary paper", authors="Alice Smith", year="2024"
        )
    )
    assert result.status == MetadataLookupStatus.FOUND
    assert not pubmed.requests


def test_incomplete_pubmed_record_is_enriched_from_crossref_by_shared_doi(
    library_factory,
):
    root = library_factory([])
    pubmed_record = MetadataRecord(
        canonical_id="PMID:12345678",
        title="A cross disciplinary paper",
        pmid="12345678",
        doi="10.1000/shared",
        source=["pubmed"],
    )
    crossref_record = MetadataRecord(
        canonical_id="DOI:10.1000/shared",
        title="A cross disciplinary paper",
        authors=["Alice Smith"],
        year="2024",
        journal="Interdisciplinary Studies",
        doi="10.1000/shared",
        source=["crossref"],
    )
    pubmed = FakeProvider(
        "pubmed",
        ProviderResult(
            "pubmed", ProviderStatus.FOUND, "pmid", "12345678", records=[pubmed_record]
        ),
    )
    crossref = FakeProvider(
        "crossref",
        ProviderResult(
            "crossref",
            ProviderStatus.FOUND,
            "doi",
            "10.1000/shared",
            records=[crossref_record],
        ),
    )
    service = CompositeMetadataLookupService(
        Settings.from_root(root), providers={"pubmed": pubmed, "crossref": crossref}
    )
    result = service.lookup(MetadataLookupRequest(pmid="12345678"))
    assert result.status == MetadataLookupStatus.FOUND
    assert result.best_record["pmid"] == "12345678"
    assert result.best_record["authors"] == ["Alice Smith"]
    assert result.best_record["journal"] == "Interdisciplinary Studies"
    assert result.providers_used == ["pubmed", "crossref"]
