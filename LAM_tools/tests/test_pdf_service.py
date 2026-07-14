from pathlib import Path

from lam.config import Settings
from lam.services.pdf_service import PdfService

from conftest import write_text_pdf


def test_pdf_metadata_text_identifiers_and_title_candidates(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "paper.pdf"
    write_text_pdf(
        path,
        [
            "A Multiline Biomedical Paper Title\nContinued Across the Second Line\n"
            "Authors\ndoi:10.1000/main.1\nPMID: 12345678"
        ],
        metadata={"/Title": "A Metadata Biomedical Paper Title", "/Author": "A. Author"},
    )
    inspection = PdfService(Settings.from_root(root)).inspect(path)
    assert inspection.is_readable is True
    assert inspection.page_count == 1
    assert inspection.metadata_title == "A Metadata Biomedical Paper Title"
    assert [item.value for item in inspection.doi_candidates] == ["10.1000/main.1"]
    assert [item.value for item in inspection.pmid_candidates] == ["12345678"]
    assert inspection.title_candidates


def test_invalid_metadata_title_is_not_used(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "paper.pdf"
    write_text_pdf(path, ["Useful Extracted Article Title\nAuthor"], metadata={"/Title": "untitled"})
    inspection = PdfService(Settings.from_root(root)).inspect(path)
    assert inspection.metadata_title == ""
    assert "metadata_title_invalid" in inspection.warnings


def test_no_text_encrypted_and_corrupt_pdf_states(library_factory):
    root = library_factory([])
    blank = root / "Inbox" / "blank.pdf"
    encrypted = root / "Inbox" / "encrypted.pdf"
    corrupt = root / "Inbox" / "corrupt.pdf"
    write_text_pdf(blank, [""])
    write_text_pdf(encrypted, ["Secret title"], password="secret")
    corrupt.write_bytes(b"not a pdf")
    service = PdfService(Settings.from_root(root))
    assert "text_unavailable" in service.inspect(blank).warnings
    assert service.inspect(encrypted).errors == ["pdf_encrypted"]
    assert service.inspect(corrupt).errors[0].startswith("pdf_unreadable:")


def test_page_and_character_limits_are_enforced(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "long.pdf"
    write_text_pdf(path, ["A" * 100, "B" * 100, "C" * 100, "D" * 100])
    base = Settings.from_root(root)
    settings = Settings(
        **{
            field: getattr(base, field)
            for field in base.__dataclass_fields__
            if field not in {"pdf_max_pages", "pdf_max_chars_per_page", "pdf_max_total_chars"}
        },
        pdf_max_pages=2,
        pdf_max_chars_per_page=40,
        pdf_max_total_chars=60,
    )
    inspection = PdfService(settings).inspect(path)
    assert len(inspection.sampled_text) <= 60
    assert "C" not in inspection.sampled_text
    assert "D" not in inspection.sampled_text


def test_in_memory_cache_hits_and_file_change_invalidates(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "paper.pdf"
    write_text_pdf(path, ["First title"])
    service = PdfService(Settings.from_root(root))
    first = service.inspect(path)
    second = service.inspect(path)
    assert first.cache_hit is False
    assert second.cache_hit is True
    write_text_pdf(path, ["Changed title and content"])
    third = service.inspect(path)
    assert third.cache_hit is False
