from __future__ import annotations

import json
from dataclasses import replace

from lam.config import Settings
from lam.http.client import HttpResult
from lam.models import MetadataLookupRequest, ProviderStatus
from lam.providers.arxiv import ArxivProvider
from lam.providers.pubmed import PubMedProvider
from lam.providers.unpaywall import UnpaywallProvider
from lam.services.metadata_cache_service import MetadataCacheService


PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>12345678</PMID>
   <Article>
    <ArticleTitle>A <i>nested</i> biomedical title</ArticleTitle>
    <Abstract>
     <AbstractText Label="BACKGROUND">First section.</AbstractText>
     <AbstractText Label="METHODS">Second section.</AbstractText>
    </Abstract>
    <AuthorList>
     <Author><ForeName>Alice</ForeName><LastName>Smith</LastName></Author>
     <Author><CollectiveName>Study Group</CollectiveName></Author>
    </AuthorList>
    <Journal>
     <ISSN>1234</ISSN><JournalIssue><PubDate><Year>2025</Year><Month>Jan</Month></PubDate></JournalIssue>
     <Title>Biomedical Journal</Title><ISOAbbreviation>Biomed J</ISOAbbreviation>
    </Journal>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType><PublicationType>Research Support</PublicationType></PublicationTypeList>
    <ELocationID EIdType="doi">10.1000/example.1</ELocationID>
    <Language>eng</Language>
   </Article>
   <KeywordList><Keyword>Immune cell</Keyword></KeywordList>
   <MeshHeadingList><MeshHeading><DescriptorName>T Cells</DescriptorName></MeshHeading></MeshHeadingList>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1000/example.1</ArticleId></ArticleIdList></PubmedData>
 </PubmedArticle>
</PubmedArticleSet>"""

ARXIV_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry>
  <id>http://arxiv.org/abs/2401.12345v2</id>
  <updated>2025-02-03T00:00:00Z</updated><published>2024-01-20T00:00:00Z</published>
  <title> An arXiv paper title </title><summary> Abstract text. </summary>
  <author><name>Alice Smith</name></author><author><name>Bob Jones</name></author>
  <arxiv:doi>10.1000/example.1</arxiv:doi><arxiv:journal_ref>Biomedical Journal (2025)</arxiv:journal_ref>
  <arxiv:primary_category term="q-bio.BM"/><category term="cs.LG"/>
  <link rel="alternate" href="https://arxiv.org/abs/2401.12345v2"/>
  <link title="pdf" type="application/pdf" href="https://arxiv.org/pdf/2401.12345v2"/>
 </entry>
</feed>"""


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


def response(content: bytes, status=200):
    return HttpResult(status, content, {}, 1, 0, 0.0)


def configured(library_factory):
    root = library_factory([])
    base = Settings.from_root(root)
    settings = replace(
        base,
        pubmed=replace(base.pubmed, email="test@example.org", api_key="secret"),
        unpaywall=replace(base.unpaywall, email="test@example.org"),
    )
    cache = MetadataCacheService(settings.metadata_cache_dir, settings.cache)
    return settings, cache


def test_pubmed_pmid_parses_structured_xml_and_caches(library_factory):
    settings, cache = configured(library_factory)
    fake = FakeHttp([response(PUBMED_XML)])
    provider = PubMedProvider(settings, cache, fake)
    request = MetadataLookupRequest(pmid="12345678")
    result = provider.lookup(request)
    record = result.records[0]
    assert result.status == ProviderStatus.FOUND
    assert record.title == "A nested biomedical title"
    assert record.authors == ["Alice Smith", "Study Group"]
    assert record.abstract == "BACKGROUND: First section.\n\nMETHODS: Second section."
    assert record.journal_abbrev == "Biomed J"
    assert record.keywords == ["Immune cell"]
    assert record.mesh_terms == ["T Cells"]
    assert record.doi == "10.1000/example.1"
    assert record.publication_type is None
    assert record.raw_publication_types == ["Journal Article", "Research Support"]
    assert any(
        item.field_name == "raw_publication_types"
        and item.value == ["Journal Article", "Research Support"]
        for item in record.provenance
    )
    cached = provider.lookup(request)
    assert cached.cache_hit is True
    assert len(fake.calls) == 1
    assert fake.calls[0][1]["api_key"] == "secret"


def test_pubmed_doi_uses_esearch_then_efetch(library_factory):
    settings, cache = configured(library_factory)
    search = json.dumps({"esearchresult": {"idlist": ["12345678"]}}).encode()
    fake = FakeHttp([response(search), response(PUBMED_XML)])
    result = PubMedProvider(settings, cache, fake).lookup(
        MetadataLookupRequest(doi="https://doi.org/10.1000/EXAMPLE.1")
    )
    assert result.status == ProviderStatus.FOUND
    assert result.records[0].pmid == "12345678"
    assert len(fake.calls) == 2
    assert fake.calls[0][0].endswith("esearch.fcgi")
    assert fake.calls[1][0].endswith("efetch.fcgi")


def test_pubmed_not_found_is_distinct_from_failure(library_factory):
    settings, cache = configured(library_factory)
    search = json.dumps({"esearchresult": {"idlist": []}}).encode()
    result = PubMedProvider(
        settings, cache, FakeHttp([response(search), response(search)])
    ).lookup(
        MetadataLookupRequest(title="No such exact paper")
    )
    assert result.status == ProviderStatus.NOT_FOUND


def test_pubmed_title_query_uses_fielded_token_fallback_before_not_found(
    library_factory,
):
    settings, cache = configured(library_factory)
    empty = json.dumps({"esearchresult": {"idlist": []}}).encode()
    found = json.dumps({"esearchresult": {"idlist": ["12345678"]}}).encode()
    fake = FakeHttp([response(empty), response(found), response(PUBMED_XML)])
    result = PubMedProvider(settings, cache, fake).lookup(
        MetadataLookupRequest(title="A nested biomedical title")
    )
    assert result.status == ProviderStatus.FOUND
    assert len(fake.calls) == 3
    first_term = fake.calls[0][1]["term"]
    fallback_term = fake.calls[1][1]["term"]
    assert first_term == '"A nested biomedical title"[Title]'
    assert "[Title] AND" in fallback_term
    assert "nested" in fallback_term
    assert "secret" not in json.dumps(result.to_dict())


def test_arxiv_id_normalization_atom_and_pdf_url(library_factory):
    settings, cache = configured(library_factory)
    fake = FakeHttp([response(ARXIV_XML)])
    result = ArxivProvider(settings, cache, fake).lookup(
        MetadataLookupRequest(arxiv_id="arXiv:2401.12345v2")
    )
    record = result.records[0]
    assert result.status == ProviderStatus.FOUND
    assert record.arxiv_id == "2401.12345"
    assert record.source_ids["arxiv_version"] == "2401.12345v2"
    assert record.categories == ["q-bio.BM", "cs.LG"]
    assert record.pdf_url == "https://arxiv.org/pdf/2401.12345v2"
    assert record.download_candidates[0].provider == "arxiv"
    assert record.download_candidates[0].expected_arxiv_id == "2401.12345"
    assert fake.calls[0][1]["id_list"] == "2401.12345v2"


def test_unpaywall_best_location_and_landing_page_are_distinct(library_factory):
    settings, cache = configured(library_factory)
    payload = {
        "doi": "10.1000/example.1",
        "title": "A biomedical title",
        "year": 2025,
        "journal_name": "Biomedical Journal",
        "genre": "journal-article",
        "is_oa": True,
        "oa_status": "gold",
        "doi_url": "https://doi.org/10.1000/example.1",
        "best_oa_location": {
            "url": "https://repository.example/item",
            "url_for_landing_page": "https://repository.example/item",
            "url_for_pdf": "https://repository.example/item.pdf",
        },
        "oa_locations": [],
        "z_authors": [{"given": "Alice", "family": "Smith"}],
    }
    fake = FakeHttp([response(json.dumps(payload).encode())])
    result = UnpaywallProvider(settings, cache, fake).lookup(
        MetadataLookupRequest(doi="10.1000/example.1")
    )
    record = result.records[0]
    assert record.pdf_url.endswith(".pdf")
    assert record.best_oa_url == "https://repository.example/item"
    assert record.landing_page_url.startswith("https://doi.org/")
    assert record.download_candidates[0].source_url.endswith("item.pdf")
    assert record.download_candidates[0].expected_doi == "10.1000/example.1"
    assert fake.calls[0][1] == {"email": "test@example.org"}


def test_unpaywall_404_is_not_found(library_factory):
    settings, cache = configured(library_factory)
    result = UnpaywallProvider(
        settings, cache, FakeHttp([response(b"{}", status=404)])
    ).lookup(MetadataLookupRequest(doi="10.1000/missing"))
    assert result.status == ProviderStatus.NOT_FOUND


def test_unpaywall_does_not_infer_pdf_candidate_without_url_for_pdf(library_factory):
    settings, cache = configured(library_factory)
    payload = {
        "doi": "10.1000/no-direct",
        "title": "Landing page only",
        "best_oa_location": {
            "url": "https://repository.example/looks-like-a-paper.pdf",
            "url_for_landing_page": "https://repository.example/item",
            "url_for_pdf": None,
        },
        "oa_locations": [],
    }
    result = UnpaywallProvider(
        settings,
        cache,
        FakeHttp([response(json.dumps(payload).encode())]),
    ).lookup(MetadataLookupRequest(doi="10.1000/no-direct"))
    assert result.records[0].download_candidates == []
