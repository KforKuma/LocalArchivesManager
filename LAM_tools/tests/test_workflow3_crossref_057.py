from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

from lam.config import Settings
from lam.models import (
    IdentifierCandidate,
    MetadataLookupResult,
    MetadataLookupStatus,
    OcrInspection,
    ProviderResult,
    ProviderStatus,
    TitleCandidate,
    WorkflowStatus,
)
from lam.workflows.inbox_register import InboxRegisterWorkflow

from conftest import write_text_pdf


class RecordingMetadataService:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def lookup(self, request):
        self.requests.append(request)
        return self.result


class FixedOcrService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def inspect_first_page(self, path, **kwargs):
        self.calls.append((path, kwargs))
        value = deepcopy(self.result)
        value.trigger_reason = kwargs["trigger_reason"]
        return value


def _crossref_found():
    record = {
        "canonical_id": "DOI:10.5555/cross-disciplinary",
        "title": "A cross disciplinary paper about public infrastructure",
        "authors": ["Alice Smith", "Bob Jones"],
        "year": "2024",
        "journal": "Interdisciplinary Studies",
        "journal_abbrev": "Interdiscip Stud",
        "doi": "10.5555/cross-disciplinary",
        "source": ["crossref"],
        "source_ids": {"crossref": "10.5555/cross-disciplinary"},
    }
    provider = ProviderResult(
        "crossref",
        ProviderStatus.FOUND,
        "bibliographic",
        record["title"],
    )
    return MetadataLookupResult(
        status=MetadataLookupStatus.FOUND,
        records=[record],
        best_record=record,
        confidence="exact_title_supported",
        providers_used=["crossref"],
        provider_results=[provider],
        selection_reason="Crossref title and year matched.",
    )


def _not_found():
    return MetadataLookupResult(
        status=MetadataLookupStatus.NOT_FOUND,
        confidence="insufficient",
        providers_used=["crossref", "pubmed", "arxiv"],
        provider_results=[
            ProviderResult(
                "crossref", ProviderStatus.NOT_FOUND, "bibliographic", "title"
            )
        ],
        selection_reason="No provider returned a usable candidate.",
    )


def test_title_lookup_precedes_ocr_and_crossref_match_skips_ocr(library_factory):
    root = library_factory([])
    source = root / "Inbox" / "new-paper.pdf"
    write_text_pdf(
        source,
        [
            "A cross disciplinary paper about public infrastructure\n"
            "Alice Smith and Bob Jones\nInterdisciplinary Studies 2024"
        ],
    )
    metadata = RecordingMetadataService(_crossref_found())
    ocr = FixedOcrService(OcrInspection(status="success"))
    settings = Settings.from_root(root)
    settings = replace(settings, ocr=replace(settings.ocr, enabled=True))
    result = InboxRegisterWorkflow(
        settings, metadata_service=metadata, ocr_service=ocr
    ).run(dry_run=True)
    item = result.details["files"][0]
    assert result.status == WorkflowStatus.SUCCESS
    assert metadata.requests and metadata.requests[0].title
    assert item["title_lookup_before_ocr"] is True
    assert item["crossref_queries"] == 1
    assert item["ocr_skipped_after_provider_match"] is True
    assert item["ocr_triggered"] is False
    assert not ocr.calls
    assert source.exists()


def test_crossref_not_found_is_normal_ocr_degradation_and_dry_run_is_read_only(
    library_factory, monkeypatch
):
    root = library_factory([])
    source = (
        root
        / "Inbox"
        / "Interdisciplinary Studies, 2024 - A screenshot wrapped paper with uncertain identity.pdf"
    )
    write_text_pdf(
        source,
        [""],
    )
    before = hashlib.sha256((root / "catalogue.xlsx").read_bytes()).hexdigest()
    ocr_result = OcrInspection(
        status="success",
        title_candidates=[
            TitleCandidate(
                "A screenshot wrapped paper with uncertain identity",
                "high",
                "ocr_title_author_region",
                1,
            )
        ],
        doi_candidates=[
            IdentifierCandidate(
                "10.5555/unverified",
                1,
                "corrected",
                "medium",
                "ocr_corrected",
            )
        ],
        year_candidates=["2024"],
        combined_text=(
            "A screenshot wrapped paper with uncertain identity\n"
            "1O.5555/unverified\n2024"
        ),
        warnings=["ocr_identifier_corrected", "ocr_metadata_regions_only"],
        gpu_mode="cpu",
    )
    metadata = RecordingMetadataService(_not_found())
    ocr = FixedOcrService(ocr_result)

    from lam.models import PdfVisualType, VisualPdfInspection

    monkeypatch.setattr(
        "lam.services.pdf_visual_service.PdfVisualService.inspect",
        lambda *args, **kwargs: VisualPdfInspection(
            pdf_visual_type=PdfVisualType.SCREENSHOT_WRAPPED,
            full_page_image_detected=True,
            repeated_chrome_detected=True,
            content_crop_applied=True,
            content_crop=(0.0, 0.1, 1.0, 0.9),
        ),
    )
    settings = Settings.from_root(root)
    settings = replace(settings, ocr=replace(settings.ocr, enabled=True))
    result = InboxRegisterWorkflow(
        settings, metadata_service=metadata, ocr_service=ocr
    ).run(dry_run=True)
    item = result.details["files"][0]
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert item["title_lookup_before_ocr"] is True
    assert ocr.calls, (
        item.get("metadata_lookup_status"),
        item.get("ocr_trigger_reason"),
        item.get("inspection"),
    )
    assert item["ocr_triggered"] is True
    assert item["pdf_visual_type"] == "screenshot_wrapped_pdf"
    assert item["ocr_candidate_corrected"] is True
    assert item["action"] == "provisional"
    assert not item.get("target_path")
    assert source.exists()
    assert hashlib.sha256((root / "catalogue.xlsx").read_bytes()).hexdigest() == before
