from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PIL import Image

from lam.config import Settings
from lam.models import OcrAvailability
from lam.services.ocr_service import OcrService


def enabled_settings(root: Path):
    base = Settings.from_root(root)
    return replace(
        base, ocr=replace(base.ocr, enabled=True, min_text_chars=10, gpu="false")
    )


def available(*, deep=False):
    return OcrAvailability(
        available=True,
        pdf2image_available=True,
        poppler_available=True,
        easyocr_available=True,
        temporary_directory_writable=True,
        status="available",
    )


class FakeReader:
    def __init__(self, results, *, error=None):
        self.results = results
        self.error = error
        self.calls = 0

    def readtext(self, image, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.results


def block(x, y, width, text, confidence=0.9):
    return ([[x, y], [x + width, y], [x + width, y + 20], [x, y + 20]], text, confidence)


def test_first_page_render_sort_candidates_and_cleanup(library_factory, monkeypatch):
    root = library_factory([])
    source = root / "Inbox" / "scan.pdf"
    source.write_bytes(b"scan-v1")
    render_calls = []

    def renderer(path, **kwargs):
        render_calls.append(kwargs)
        (Path(kwargs["output_folder"]) / "page.png").write_bytes(b"temporary")
        return [Image.new("RGB", (1000, 1200), "white")]

    reader = FakeReader(
        [
            block(20, 300, 300, "doi:10.1000/ocr.1"),
            block(20, 140, 700, "Recognition", 0.95),
            block(20, 100, 700, "A Scanned Biomedical Paper Title", 0.98),
            block(20, 350, 200, "PMID: 12345678"),
            block(20, 400, 100, "2025"),
        ]
    )
    service = OcrService(
        enabled_settings(root), renderer=renderer, reader_factory=lambda *a, **k: reader
    )
    monkeypatch.setattr(service, "check_availability", available)
    inspection = service.inspect_first_page(
        source, run_id="ocr-test", trigger_reason="first_page_text_empty"
    )
    assert inspection.status == "success"
    assert render_calls[0]["first_page"] == render_calls[0]["last_page"] == 1
    assert render_calls[0]["thread_count"] == 1
    assert inspection.ordered_lines[0].startswith("A Scanned")
    assert inspection.raw_blocks[0].bounding_box
    assert inspection.raw_blocks[0].confidence == 0.9
    assert inspection.doi_candidates[0].value == "10.1000/ocr.1"
    assert inspection.pmid_candidates[0].value == "12345678"
    assert inspection.year_candidates == ["2025"]
    assert any("Scanned Biomedical" in item.value for item in inspection.title_candidates)
    assert not (root / ".library_state" / "tmp" / "ocr-test" / "ocr").exists()


def test_ocr_cache_hit_and_file_change_invalidates(library_factory, monkeypatch):
    root = library_factory([])
    source = root / "Inbox" / "scan.pdf"
    source.write_bytes(b"scan-v1")
    calls = []

    def renderer(path, **kwargs):
        calls.append(path)
        return [Image.new("RGB", (800, 1000), "white")]

    reader = FakeReader([block(10, 50, 600, "Cached Scanned Paper Title")])
    service = OcrService(
        enabled_settings(root), renderer=renderer, reader_factory=lambda *a, **k: reader
    )
    monkeypatch.setattr(service, "check_availability", available)
    first = service.inspect_first_page(source, run_id="one", trigger_reason="empty")
    second = service.inspect_first_page(source, run_id="two", trigger_reason="empty")
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(calls) == 1
    source.write_bytes(b"scan-v2")
    third = service.inspect_first_page(source, run_id="three", trigger_reason="empty")
    assert third.cache_hit is False
    assert len(calls) == 2


def test_cache_can_be_read_but_dry_run_write_is_disabled(library_factory, monkeypatch):
    root = library_factory([])
    source = root / "Inbox" / "scan.pdf"
    source.write_bytes(b"dry-scan")
    service = OcrService(
        enabled_settings(root),
        renderer=lambda *a, **k: [Image.new("RGB", (800, 1000), "white")],
        reader_factory=lambda *a, **k: FakeReader(
            [block(10, 50, 600, "Dry Run OCR Paper Title")]
        ),
    )
    monkeypatch.setattr(service, "check_availability", available)
    result = service.inspect_first_page(
        source, run_id="dry-cache", trigger_reason="empty", cache_write=False
    )
    assert result.status == "success"
    assert not list((root / ".library_state" / "ocr_cache").glob("*.json"))


def test_corrected_doi_is_lower_confidence_and_marked(library_factory, monkeypatch):
    root = library_factory([])
    source = root / "Inbox" / "scan.pdf"
    source.write_bytes(b"scan")
    reader = FakeReader(
        [
            block(10, 40, 700, "Corrected Identifier Paper Title"),
            block(10, 100, 500, "1O.1234/abc.def"),
        ]
    )
    service = OcrService(
        enabled_settings(root),
        renderer=lambda *a, **k: [Image.new("RGB", (800, 1000), "white")],
        reader_factory=lambda *a, **k: reader,
    )
    monkeypatch.setattr(service, "check_availability", available)
    result = service.inspect_first_page(source, run_id="correct", trigger_reason="empty")
    corrected = next(item for item in result.doi_candidates if item.value == "10.1234/abc.def")
    assert corrected.source_type == "ocr_corrected"
    assert corrected.confidence == "medium"
    assert "ocr_identifier_corrected" in result.warnings


def test_gpu_inference_falls_back_once_to_cpu(library_factory, monkeypatch):
    root = library_factory([])
    source = root / "Inbox" / "scan.pdf"
    source.write_bytes(b"scan")
    created = []
    cpu_reader = FakeReader([block(10, 50, 600, "CPU Fallback Paper Title")])

    def factory(languages, **kwargs):
        created.append(kwargs["gpu"])
        return FakeReader([], error=RuntimeError("gpu failure")) if kwargs["gpu"] else cpu_reader

    settings = enabled_settings(root)
    settings = replace(settings, ocr=replace(settings.ocr, gpu="auto", cache_enabled=False))
    service = OcrService(
        settings,
        renderer=lambda *a, **k: [Image.new("RGB", (800, 1000), "white")],
        reader_factory=factory,
    )
    monkeypatch.setattr(service, "check_availability", available)
    monkeypatch.setattr(service, "_gpu_requested", lambda cfg: True)
    result = service.inspect_first_page(source, run_id="gpu", trigger_reason="forced")
    assert result.status == "success"
    assert result.gpu_mode == "gpu_fallback_to_cpu"
    assert "ocr_gpu_fallback" in result.warnings
    assert created == [True, False]
    second_source = root / "Inbox" / "scan-2.pdf"
    second_source.write_bytes(b"scan-2")
    second = service.inspect_first_page(
        second_source, run_id="gpu-2", trigger_reason="forced"
    )
    assert second.gpu_mode == "gpu_fallback_to_cpu"
    assert created == [True, False]


def test_unavailable_poppler_and_oversized_image_are_distinct(library_factory, monkeypatch):
    root = library_factory([])
    source = root / "Inbox" / "scan.pdf"
    source.write_bytes(b"scan")
    settings = enabled_settings(root)
    service = OcrService(settings)
    monkeypatch.setattr(service, "_poppler_executable", lambda: None)
    unavailable = service.inspect_first_page(source, run_id="missing", trigger_reason="empty")
    assert unavailable.status == "ocr_unavailable_poppler"
    assert "pdf_unreadable" not in unavailable.errors

    settings = replace(settings, ocr=replace(settings.ocr, max_image_pixels=1_000_000))
    service = OcrService(
        settings,
        renderer=lambda *a, **k: [Image.new("RGB", (2000, 2000), "white")],
        reader_factory=lambda *a, **k: FakeReader([]),
    )
    monkeypatch.setattr(service, "check_availability", available)
    oversized = service.inspect_first_page(source, run_id="large", trigger_reason="empty")
    assert oversized.status == "ocr_image_too_large"


def test_broken_easyocr_import_is_reported_without_importing_in_process(
    library_factory, monkeypatch
):
    root = library_factory([])
    service = OcrService(enabled_settings(root))
    monkeypatch.setattr(service, "_poppler_executable", lambda: Path("pdftoppm"))
    monkeypatch.setattr(service, "_probe_easyocr_import", lambda: False)
    availability = service.check_availability()
    assert availability.available is False
    assert availability.status == "ocr_unavailable_easyocr"
    assert availability.details["easyocr_import_probe"] is False


def test_reader_reused_and_download_disabled(library_factory, monkeypatch):
    root = library_factory([])
    settings = enabled_settings(root)
    settings = replace(settings, ocr=replace(settings.ocr, cache_enabled=False))
    created = []

    def factory(languages, **kwargs):
        created.append(kwargs)
        return FakeReader([block(10, 50, 500, "Reusable Reader Paper Title")])

    service = OcrService(
        settings,
        renderer=lambda *a, **k: [Image.new("RGB", (800, 1000), "white")],
        reader_factory=factory,
    )
    monkeypatch.setattr(service, "check_availability", available)
    for index in range(2):
        source = root / "Inbox" / f"scan-{index}.pdf"
        source.write_bytes(f"scan-{index}".encode())
        service.inspect_first_page(source, run_id=f"reuse-{index}", trigger_reason="empty")
    assert len(created) == 1
    assert created[0]["download_enabled"] is False
