from __future__ import annotations

import json

from lam.models import MetadataLookupRequest, MetadataRecord, ProviderResult, ProviderStatus
from lam.providers.unpaywall import UnpaywallProvider
from lam.services.metadata_merge_service import MetadataMergeService
from lam.utils.filename import standard_pdf_filename, standard_pdf_filename_result
from lam.utils.publication_type import canonicalize_publication_type


def test_ordinary_and_index_types_canonicalize_to_empty():
    result = canonicalize_publication_type(
        [
            "Journal Article",
            "Research Support",
            "Research Support, U.S. Gov't, Non-P.H.S.",
            "Research Support, U.S. Gov't, P.H.S.",
            "journal-article",
        ]
    )
    assert result.canonical_type is None
    assert result.warnings == ()


def test_review_wins_over_ordinary_and_research_support():
    result = canonicalize_publication_type(
        "Journal Article; Review; Research Support, Non-U.S. Gov't"
    )
    assert result.canonical_type == "Review"


def test_published_erratum_is_canonical_erratum():
    result = canonicalize_publication_type(
        ["Published Erratum", "journal-article"]
    )
    assert result.canonical_type == "Erratum"


def test_systematic_review_has_priority_over_review():
    result = canonicalize_publication_type(["Systematic Review", "Review"])
    assert result.canonical_type == "Systematic Review"


def test_meta_analysis_has_priority_over_review():
    result = canonicalize_publication_type(["Meta-Analysis", "Review"])
    assert result.canonical_type == "Meta-analysis"


def test_equally_ranked_incompatible_special_types_conflict():
    result = canonicalize_publication_type(["Erratum", "Retraction"])
    assert result.canonical_type is None
    assert result.warnings == ("publication_type_conflict",)


def test_unrecognized_type_is_omitted_and_warned():
    result = canonicalize_publication_type("Unexpected Provider Genre")
    assert result.canonical_type is None
    assert result.warnings == ("publication_type_unrecognized",)


def test_unpaywall_journal_article_is_raw_but_not_canonical():
    record = UnpaywallProvider.parse_json(
        json.dumps(
            {
                "doi": "10.1000/test",
                "title": "Ordinary Paper",
                "year": 2025,
                "journal_name": "Test Journal",
                "genre": "journal-article",
            }
        ).encode()
    )
    assert record.publication_type is None
    assert record.raw_publication_types == ["journal-article"]


def test_metadata_merge_selects_one_type_without_semicolon_joining():
    pubmed = MetadataRecord(
        canonical_id="PMID:1",
        title="Review Paper",
        doi="10.1000/test",
        pmid="1",
        publication_type="Review",
        raw_publication_types=["Journal Article", "Review", "Research Support"],
        source=["pubmed"],
    )
    unpaywall = MetadataRecord(
        canonical_id="DOI:10.1000/test",
        title="Review Paper",
        doi="10.1000/test",
        raw_publication_types=["journal-article"],
        source=["unpaywall"],
    )
    merged = MetadataMergeService().merge(
        MetadataLookupRequest(doi="10.1000/test"),
        [
            ProviderResult("pubmed", ProviderStatus.FOUND, "doi", "10.1000/test", records=[pubmed]),
            ProviderResult("unpaywall", ProviderStatus.FOUND, "doi", "10.1000/test", records=[unpaywall]),
        ],
    )
    assert merged.best_record["publication_type"] == "Review"
    assert ";" not in merged.best_record["publication_type"]
    assert "journal-article" in merged.best_record["raw_publication_types"]


def test_catalogue_fields_emit_single_canonical_string():
    record = MetadataRecord(
        publication_type="Review",
        raw_publication_types=["Journal Article", "Review", "Research Support"],
    )
    assert record.catalogue_fields()["publication_type"] == "Review"


def test_filename_defends_against_historical_composite_type():
    filename = standard_pdf_filename(
        title="A review title",
        year="2025",
        journal_abbrev="Test J",
        publication_type="Journal Article; Research Support; Review; journal-article",
    )
    assert filename == "Test J, 2025, Review - A review title.pdf"


def test_ordinary_filename_omits_publication_type():
    filename = standard_pdf_filename(
        title="An ordinary title",
        year="2025",
        journal_abbrev="Test J",
        publication_type="Journal Article; journal-article",
    )
    assert filename == "Test J, 2025 - An ordinary title.pdf"


def test_review_and_erratum_enter_filename_canonically():
    review = standard_pdf_filename(
        title="Review title", year="2025", journal="Journal", publication_type="Review"
    )
    erratum = standard_pdf_filename(
        title="Correction title",
        year="2025",
        journal="Journal",
        publication_type="Published Erratum; journal-article",
    )
    assert review == "Journal, 2025, Review - Review title.pdf"
    assert erratum == "Journal, 2025, Erratum - Correction title.pdf"


def test_length_budget_truncates_only_full_canonical_title():
    title = "A canonical title whose meaningful identifying words continue for a long distance"
    result = standard_pdf_filename_result(
        title=title,
        year="2025",
        journal="Journal",
        publication_type="Journal Article; Research Support; Review; journal-article",
        max_length=70,
    )
    assert result.filename.startswith("Journal, 2025, Review - ")
    assert len(result.filename) <= 70
    assert result.title_truncated is True
    assert "Research Support" not in result.filename
