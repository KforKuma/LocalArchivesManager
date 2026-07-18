from __future__ import annotations

from dataclasses import replace

from PIL import Image, ImageDraw

from lam.config import Settings
from lam.models import OcrAvailability, PdfVisualType, VisualPdfInspection
from lam.services.ocr_service import OcrService
from lam.services.pdf_service import PdfService
from lam.services.pdf_visual_service import PdfVisualService, metadata_region_boxes


def _page(index: int, *, chrome: bool) -> Image.Image:
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    if chrome:
        draw.rectangle((0, 0, 600, 75), fill="#30343b")
        draw.text((15, 20), "Document viewer", fill="white")
        draw.rectangle((0, 725, 600, 800), fill="#eeeeee")
        draw.text((15, 750), "https://source.example/document", fill="black")
    else:
        draw.rectangle((0, 0, 600, 75), fill=(60 * index, 20, 180))
        draw.text((15, 20), f"Article page {index}", fill="white")
        draw.rectangle((0, 725, 600, 800), fill=(10, 60 * index, 100))
    draw.rectangle((50, 120, 550, 680), fill=(240 - 20 * index, 240, 240))
    draw.text((80, 180 + 25 * index), f"Different page body {index}", fill="black")
    return image


def test_visual_classifies_native_scanned_screenshot_and_unknown(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "visual.pdf"
    path.write_bytes(b"visual")
    settings = Settings.from_root(root)
    chrome_pages = [_page(index, chrome=True) for index in (1, 2, 3)]
    screenshot = PdfVisualService(
        settings, renderer=lambda *args, **kwargs: chrome_pages
    ).inspect(
        path,
        native_text_chars=0,
        page_count=3,
        page_image_signals=[{"large_page_image": True}] * 3,
        run_id="screenshot",
    )
    assert screenshot.pdf_visual_type == PdfVisualType.SCREENSHOT_WRAPPED
    assert screenshot.repeated_chrome_detected is True
    assert screenshot.content_crop_applied is True
    assert screenshot.content_crop == (0.0, 0.1, 1.0, 0.9)

    scan_pages = [_page(index, chrome=False) for index in (1, 2, 3)]
    scanned = PdfVisualService(
        settings, renderer=lambda *args, **kwargs: scan_pages
    ).inspect(
        path,
        native_text_chars=0,
        page_count=3,
        page_image_signals=[{"large_page_image": True}] * 3,
        run_id="scan",
    )
    assert scanned.pdf_visual_type == PdfVisualType.SCANNED_ARTICLE
    assert scanned.repeated_chrome_detected is False

    native = PdfVisualService(
        settings, renderer=lambda *args, **kwargs: [_page(1, chrome=False)]
    ).inspect(
        path,
        native_text_chars=1200,
        page_count=1,
        page_image_signals=[],
        run_id="native",
    )
    assert native.pdf_visual_type == PdfVisualType.NATIVE_TEXT

    unknown = PdfVisualService(settings, renderer=lambda *args, **kwargs: []).inspect(
        path,
        native_text_chars=0,
        page_count=1,
        page_image_signals=[],
        run_id="unknown",
    )
    assert unknown.pdf_visual_type == PdfVisualType.UNKNOWN_IMAGE


def test_screenshot_region_plan_excludes_full_page_and_preserves_footer():
    visual = VisualPdfInspection(
        pdf_visual_type=PdfVisualType.SCREENSHOT_WRAPPED,
        repeated_chrome_detected=True,
        content_crop_applied=True,
        content_crop=(0.0, 0.1, 1.0, 0.9),
    )
    regions = dict(metadata_region_boxes(visual))
    assert "title_author" in regions
    assert "article_doi_region" in regions
    assert "viewer_footer_url" in regions
    assert regions["viewer_footer_url"] == (0.0, 0.88, 1.0, 1.0)
    assert all(box != (0.0, 0.0, 1.0, 1.0) for box in regions.values())


def test_identical_blank_page_margins_are_not_browser_chrome(library_factory):
    root = library_factory([])
    path = root / "Inbox" / "blank-margins.pdf"
    path.write_bytes(b"visual")
    pages = []
    for index in (1, 2, 3):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 150, 560, 650), fill=(220 - index * 20, 220, 220))
        draw.text((80, 200), f"Scanned article page {index}", fill="black")
        pages.append(image)
    inspection = PdfVisualService(
        Settings.from_root(root), renderer=lambda *args, **kwargs: pages
    ).inspect(
        path,
        native_text_chars=0,
        page_count=3,
        page_image_signals=[{"large_page_image": True}] * 3,
        run_id="blank-margins",
    )
    assert inspection.pdf_visual_type == PdfVisualType.SCANNED_ARTICLE
    assert inspection.repeated_chrome_detected is False


def test_self_generated_screenshot_wrapped_pdf_integration_fixture(library_factory):
    root = library_factory([])
    source = root / "Inbox" / "screenshot-fixture.pdf"
    pages = [_page(index, chrome=True) for index in (1, 2, 3)]
    pages[0].save(source, "PDF", save_all=True, append_images=pages[1:])
    settings = Settings.from_root(root)
    visual_service = PdfVisualService(
        settings, renderer=lambda *args, **kwargs: pages
    )
    inspection = PdfService(
        settings, visual_service=visual_service
    ).inspect(
        source,
        ocr_mode="never",
        visual_analysis=True,
    )
    assert inspection.pypdf_text_available is False
    assert any(
        item["large_page_image"]
        for item in inspection.pypdf_result["page_image_signals"]
    )
    assert inspection.visual_inspection.pdf_visual_type == PdfVisualType.SCREENSHOT_WRAPPED


class SequenceReader:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def readtext(self, image, **kwargs):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        height, width = image.shape[:2]
        return [
            (
                [[5, 5], [max(10, width - 5), 5], [max(10, width - 5), 30], [5, 30]],
                text,
                0.96,
            )
        ]


def _available(*, deep=False):
    return OcrAvailability(
        available=True,
        pdf2image_available=True,
        poppler_available=True,
        easyocr_available=True,
        temporary_directory_writable=True,
        status="available",
    )


def test_regional_ocr_extracts_title_doi_footer_and_never_runs_full_page(
    library_factory, monkeypatch
):
    root = library_factory([])
    source = root / "Inbox" / "wrapped.pdf"
    source.write_bytes(b"wrapped")
    base = Settings.from_root(root)
    settings = replace(
        base,
        ocr=replace(
            base.ocr,
            enabled=True,
            min_text_chars=10,
            gpu="false",
            cache_enabled=False,
        ),
    )
    reader = SequenceReader(
        [
            "Interdisciplinary Studies 2024",
            "A Screenshot Wrapped Research Article by Alice Smith",
            "Volume 12 Issue 3 Published 2024",
            "Identifier 1O.1234/ocr.sample",
            "Abstract section header text",
            "https://doi.org/10.1234/footer.sample",
        ]
    )
    service = OcrService(
        settings,
        renderer=lambda *args, **kwargs: [Image.new("RGB", (600, 800), "white")],
        reader_factory=lambda *args, **kwargs: reader,
    )
    monkeypatch.setattr(service, "check_availability", _available)
    visual = VisualPdfInspection(
        pdf_visual_type=PdfVisualType.SCREENSHOT_WRAPPED,
        full_page_image_detected=True,
        repeated_chrome_detected=True,
        content_crop_applied=True,
        content_crop=(0.0, 0.1, 1.0, 0.9),
    )
    result = service.inspect_first_page(
        source,
        run_id="regional",
        trigger_reason="provider_not_found_local_metadata_incomplete",
        visual_inspection=visual,
    )
    assert result.status == "success"
    assert len(result.metadata_regions) == 6
    assert reader.calls <= 18
    assert "ocr_metadata_regions_only" in result.warnings
    assert any("Screenshot Wrapped" in item.value for item in result.title_candidates)
    assert any(
        item.value == "10.1234/ocr.sample"
        and item.source_type == "ocr_corrected"
        for item in result.doi_candidates
    )
    assert any(
        item.value == "10.1234/footer.sample"
        and item.source_type == "footer_url_ocr"
        for item in result.doi_candidates
    )
    assert visual.footer_url_detected is True
    assert "Different page body" not in result.combined_text
