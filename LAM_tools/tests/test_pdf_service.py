from pathlib import Path
from copy import deepcopy
from dataclasses import replace

from lam.config import Settings
from lam.models import IdentifierCandidate, OcrInspection, TitleCandidate
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


class StubOcrService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def inspect_first_page(self, path, **kwargs):
        self.calls.append((path, kwargs))
        result = deepcopy(self.result)
        result.trigger_reason = kwargs["trigger_reason"]
        result.dpi = kwargs["config"].dpi
        return result


def ocr_enabled(root, *, min_chars=80):
    base = Settings.from_root(root)
    return replace(
        base,
        ocr=replace(base.ocr, enabled=True, min_text_chars=min_chars, gpu="false"),
    )


def successful_ocr():
    return OcrInspection(
        status="success",
        title_candidates=[
            TitleCandidate("Scanned Biomedical Paper", "high", "ocr_page_top", 1)
        ],
        doi_candidates=[
            IdentifierCandidate("10.1000/scan", 1, "doi", "high", "ocr")
        ],
        year_candidates=["2025"],
        combined_text="Scanned Biomedical Paper\ndoi:10.1000/scan\n2025",
        gpu_mode="cpu",
    )


def test_ocr_trigger_modes_and_skip_text_semantics(library_factory):
    root = library_factory([])
    sufficient = root / "Inbox" / "sufficient.pdf"
    blank = root / "Inbox" / "blank.pdf"
    write_text_pdf(
        sufficient,
        ["A sufficiently long biomedical paper title " + "supporting text " * 8],
    )
    write_text_pdf(blank, [""])
    service = StubOcrService(successful_ocr())
    pdfs = PdfService(ocr_enabled(root, min_chars=40), service)

    enough = pdfs.inspect(sufficient, ocr_mode="auto")
    assert enough.ocr_result is None
    automatic = pdfs.inspect(blank, ocr_mode="auto")
    assert automatic.ocr_result.status == "success"
    assert automatic.text_extraction_method == "ocr"
    assert automatic.ocr_result.trigger_reason == "first_page_text_empty"

    never = PdfService(ocr_enabled(root), service).inspect(blank, ocr_mode="never")
    assert never.ocr_result is None
    forced = PdfService(ocr_enabled(root), service).inspect(
        sufficient, ocr_mode="always"
    )
    assert forced.ocr_result.trigger_reason == "user_forced"
    skipped = PdfService(ocr_enabled(root), service).inspect(
        blank, extract_text=False, ocr_mode="always"
    )
    assert skipped.ocr_result is None


def test_ocr_candidates_merge_and_conflicts_are_reported(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "mixed.pdf"
    write_text_pdf(
        path,
        ["Embedded Paper Title\ndoi:10.1000/embedded\n" + "text " * 30],
    )
    ocr = successful_ocr()
    service = StubOcrService(ocr)
    inspection = PdfService(ocr_enabled(root), service).inspect(
        path, ocr_mode="always"
    )
    assert "10.1000/embedded" in [item.value for item in inspection.doi_candidates]
    assert "10.1000/scan" in [item.value for item in inspection.doi_candidates]
    assert "pdf_text_ocr_conflict" in inspection.warnings
    summary = inspection.report_summary()
    assert "combined_text" not in summary["ocr"]
    assert summary["ocr"]["doi_candidates"] == ["10.1000/scan"]
