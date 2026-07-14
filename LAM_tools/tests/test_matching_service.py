from lam.models import IdentifierCandidate, MatchStatus, PdfInspection, TitleCandidate
from lam.services.catalogue_service import CatalogueService
from lam.services.matching_service import MatchingService


def inspection(*, doi=None, pmid=None, title=None, years=None, supplement=False):
    return PdfInspection(
        relative_path="Inbox/incoming.pdf",
        filename="incoming.pdf",
        size=100,
        mtime_ns=1,
        is_readable=True,
        doi_candidates=[IdentifierCandidate(doi)] if doi else [],
        pmid_candidates=[IdentifierCandidate(pmid)] if pmid else [],
        title_candidates=[TitleCandidate(title, "high", "metadata")] if title else [],
        year_candidates=years or [],
        is_probable_supplement=supplement,
    )


def records_for(library_factory, rows):
    root = library_factory(rows)
    return root, CatalogueService(root / "catalogue.xlsx").load()


def test_path_and_filename_exact_matching(library_factory):
    root, records = records_for(
        library_factory,
        [
            {
                "id": "P1",
                "title": "Example",
                "pdf_filename": "standard.pdf",
                "pdf_relative_path": "Inbox/current.pdf",
            }
        ],
    )
    matcher = MatchingService()
    by_path = matcher.match(
        records, relative_path="Inbox/current.pdf", filename="other.pdf"
    )
    by_name = matcher.match(
        records, relative_path="Inbox/other.pdf", filename="standard.pdf"
    )
    assert by_path.matched_catalogue_id == "P1"
    assert by_path.method == "pdf_relative_path"
    assert by_name.matched_catalogue_id == "P1"
    assert by_name.method == "pdf_filename"


def test_doi_and_pmid_unique_matching(library_factory):
    root, records = records_for(
        library_factory,
        [
            {"id": "P1", "title": "One", "doi": "10.1000/one"},
            {"id": "P2", "title": "Two", "pmid": "12345678"},
        ],
    )
    matcher = MatchingService()
    doi = matcher.match(
        records,
        relative_path="Inbox/a.pdf",
        filename="a.pdf",
        inspection=inspection(doi="10.1000/one"),
    )
    pmid = matcher.match(
        records,
        relative_path="Inbox/b.pdf",
        filename="b.pdf",
        inspection=inspection(pmid="12345678"),
    )
    assert doi.matched_catalogue_id == "P1"
    assert doi.confidence == "exact_identifier"
    assert pmid.matched_catalogue_id == "P2"


def test_title_year_disambiguates_duplicate_titles(library_factory):
    root, records = records_for(
        library_factory,
        [
            {"id": "P1", "title": "Shared Biomedical Title", "year": "2024"},
            {"id": "P2", "title": "Shared Biomedical Title", "year": "2025"},
        ],
    )
    result = MatchingService().match(
        records,
        relative_path="Inbox/a.pdf",
        filename="a.pdf",
        inspection=inspection(title="Shared Biomedical Title", years=["2025"]),
    )
    assert result.status == MatchStatus.EXACT
    assert result.matched_catalogue_id == "P2"


def test_duplicate_title_without_support_is_ambiguous(library_factory):
    root, records = records_for(
        library_factory,
        [
            {"id": "P1", "title": "Shared Biomedical Title", "year": "2024"},
            {"id": "P2", "title": "Shared Biomedical Title", "year": "2025"},
        ],
    )
    result = MatchingService().match(
        records,
        relative_path="Inbox/a.pdf",
        filename="a.pdf",
        inspection=inspection(title="Shared Biomedical Title"),
    )
    assert result.status == MatchStatus.AMBIGUOUS
    assert result.issue_key == "paper_identity_ambiguous"


def test_identifier_conflict_blocks(library_factory):
    root, records = records_for(
        library_factory,
        [
            {"id": "P1", "title": "One", "doi": "10.1000/one"},
            {"id": "P2", "title": "Two", "pmid": "12345678"},
        ],
    )
    result = MatchingService().match(
        records,
        relative_path="Inbox/a.pdf",
        filename="a.pdf",
        inspection=inspection(doi="10.1000/one", pmid="12345678"),
    )
    assert result.status == MatchStatus.CONFLICT
    assert result.issue_key == "identifier_conflict"


def test_fuzzy_title_never_auto_registers(library_factory):
    root, records = records_for(
        library_factory,
        [{"id": "P1", "title": "A Detailed Biomedical Research Article"}],
    )
    result = MatchingService().match(
        records,
        relative_path="Inbox/a.pdf",
        filename="a.pdf",
        inspection=inspection(title="A Detailed Biomedical Research Articles"),
    )
    assert result.status == MatchStatus.AMBIGUOUS
    assert result.conflicts == ["fuzzy_title_requires_review"]


def test_user_confirmation_cannot_override_identifier_conflict(library_factory):
    root, records = records_for(
        library_factory,
        [
            {"id": "P1", "title": "One", "doi": "10.1000/one"},
            {"id": "P2", "title": "Two", "doi": "10.1000/two"},
        ],
    )
    result = MatchingService().match(
        records,
        relative_path="Inbox/a.pdf",
        filename="a.pdf",
        inspection=inspection(doi="10.1000/one"),
        confirmed_catalogue_id="P2",
    )
    assert result.status == MatchStatus.CONFLICT


def test_standard_filename_title_matches_without_pdf_inspection(library_factory):
    root, records = records_for(
        library_factory,
        [{"id": "P1", "title": "Standard Filename Paper", "year": "2025"}],
    )
    result = MatchingService().match(
        records,
        relative_path="Inbox/Std J, 2025 - Standard Filename Paper.pdf",
        filename="Std J, 2025 - Standard Filename Paper.pdf",
    )
    assert result.status == MatchStatus.HIGH_CONFIDENCE
    assert result.matched_catalogue_id == "P1"
    assert result.method == "standard_filename_title"
