from lam.utils.identifiers import (
    extract_doi_candidates,
    extract_pmid_candidates,
    normalize_doi,
    normalize_pmid,
)


def test_doi_normalization_and_trailing_punctuation():
    assert normalize_doi("https://doi.org/10.1000/ABC.123).") == "10.1000/abc.123"
    assert normalize_doi("doi: 10.1016/j.cell.2025.01.001;") == "10.1016/j.cell.2025.01.001"


def test_doi_candidates_keep_context_and_source():
    candidates = extract_doi_candidates(
        "Primary doi:10.1000/main.1 and reference 10.1000/ref.2.",
        page=1,
        source_type="first_page",
    )
    assert [item.value for item in candidates] == ["10.1000/main.1", "10.1000/ref.2"]
    assert all(item.page == 1 and item.confidence == "high" for item in candidates)


def test_pmid_requires_an_explicit_label():
    assert extract_pmid_candidates("2025 12345678 page 123") == []
    assert [item.value for item in extract_pmid_candidates("PubMed PMID: 12345678")] == [
        "12345678"
    ]


def test_pmid_normalization():
    assert normalize_pmid("PMID: 98765432") == "98765432"
    assert normalize_pmid("98765432") == "98765432"
    assert normalize_pmid("2025") == ""
