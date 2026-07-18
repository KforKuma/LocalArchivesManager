from __future__ import annotations

from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from lam.config import Settings
from lam.services.ocr_service import OcrService


@pytest.mark.ocr_live
def test_real_first_page_ocr_when_local_models_are_ready(library_factory):
    root = library_factory([])
    image = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(image)
    draw.text((120, 180), "A Real OCR Integration Paper Title", fill="black")
    draw.text((120, 320), "doi:10.1000/ocr.live", fill="black")
    source = root / "Inbox" / "live-scan.pdf"
    image.save(source, "PDF", resolution=150)
    base = Settings.from_root(root)
    settings = replace(
        base,
        ocr=replace(
            base.ocr,
            enabled=True,
            gpu="false",
            download_enabled=False,
            min_text_chars=5,
        ),
    )
    service = OcrService(settings)
    availability = service.check_availability(deep=True)
    if not availability.available:
        pytest.skip(availability.status)
    result = service.inspect_first_page(
        source, run_id="ocr-live", trigger_reason="integration_test"
    )
    assert result.status == "success"
    assert any(item.value == "10.1000/ocr.live" for item in result.doi_candidates)
