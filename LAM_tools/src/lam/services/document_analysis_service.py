from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Protocol

from ..config import Settings
from ..models import (
    AnalysisCandidate,
    CandidateConfidence,
    DocumentAnalysisRequest,
    DocumentAnalysisResult,
    PdfVisualType,
)
from ..utils.candidate_cleaning import classify_title_candidate
from ..utils.identifiers import extract_doi_candidates, parse_doi_candidate
from ..utils.local_pdf_metadata import extract_local_pdf_metadata
from ..utils.text import title_candidates_from_page


class DocumentAnalysisBackend(Protocol):
    name: str
    capabilities: set[str]

    def analyze(self, request: DocumentAnalysisRequest) -> DocumentAnalysisResult:
        ...


class NativePdfBackend:
    """Adapter for bounded native PDF metadata/text already read by PdfService."""

    name = "native"
    capabilities = {"native_text", "metadata", "title", "doi", "year"}

    def __init__(self, settings: Settings):
        self.settings = settings

    def analyze(self, request: DocumentAnalysisRequest) -> DocumentAnalysisResult:
        started = time.monotonic()
        titles: list[AnalysisCandidate] = []
        if request.metadata_title:
            titles.append(
                classify_title_candidate(
                    request.metadata_title,
                    source="metadata",
                    page=None,
                    region="pdf_metadata",
                    evidence="pdf_document_metadata",
                )
            )
        for candidate in title_candidates_from_page(
            request.native_text,
            page=1,
            min_length=self.settings.pdf_title_min_length,
            max_length=self.settings.pdf_title_max_length,
        ):
            titles.append(
                classify_title_candidate(
                    candidate.value,
                    source="page_top",
                    page=candidate.page,
                    region="native_page_top",
                    evidence=candidate.evidence,
                )
            )
        dois = [
            AnalysisCandidate(
                value=item.value,
                field="doi",
                source=item.source_type,
                confidence=CandidateConfidence.TRUSTED,
                page=item.page,
                evidence=item.line_or_context,
            )
            for item in extract_doi_candidates(
                request.native_text, page=1, source_type="first_page"
            )
        ]
        years = [
            AnalysisCandidate(
                value=value,
                field="year",
                source="native_text",
                confidence=CandidateConfidence.USABLE,
                page=1,
            )
            for value in sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", request.native_text)))
        ]
        warnings = []
        if any(
            "metadata_title_contaminated" in item.rejection_reasons for item in titles
        ):
            warnings.append("metadata_title_contaminated")
        return DocumentAnalysisResult(
            backend=self.name,
            status="success",
            capabilities_used=self.capabilities & request.requested_fields
            or self.capabilities,
            title_candidates=titles,
            year_candidates=years,
            doi_candidates=dois,
            warnings=warnings,
            duration_ms=round((time.monotonic() - started) * 1000),
        )


class EasyOcrRegionBackend:
    name = "easyocr"
    capabilities = {
        "regional_ocr",
        "title",
        "author",
        "journal",
        "year",
        "doi",
        "footer_url",
    }

    def __init__(self, settings: Settings, ocr_service=None):
        self.settings = settings
        if ocr_service is None:
            from .ocr_service import OcrService

            ocr_service = OcrService(settings)
        self.ocr_service = ocr_service

    def analyze(self, request: DocumentAnalysisRequest) -> DocumentAnalysisResult:
        started = time.monotonic()
        config = request.resource_limits.get("ocr_config") or replace(
            self.settings.ocr, languages=request.language_hints or self.settings.ocr.languages
        )
        ocr = self.ocr_service.inspect_first_page(
            request.file_path,
            run_id=request.run_id,
            trigger_reason=request.trigger_reason,
            config=config,
            cache_write=request.cache_write,
            visual_inspection=request.visual_inspection,
        )
        titles: list[AnalysisCandidate] = []
        for candidate in ocr.title_candidates:
            titles.append(
                classify_title_candidate(
                    candidate.value,
                    source=(
                        "ocr_title_merged"
                        if "merged" in candidate.source_type
                        else candidate.source_type
                    ),
                    page=candidate.page,
                    region="title_author",
                    evidence=candidate.evidence,
                )
            )
        titles.extend(ocr.rejected_title_candidates)
        dois: list[AnalysisCandidate] = []
        for candidate in ocr.doi_candidates:
            parsed = parse_doi_candidate(
                candidate.value,
                min_suffix_alnum=self.settings.document_analysis.doi_min_suffix_alnum,
                max_length=self.settings.document_analysis.doi_max_length,
            )
            confidence = (
                CandidateConfidence.USABLE
                if parsed.complete
                else CandidateConfidence.REJECTED
            )
            dois.append(
                AnalysisCandidate(
                    value=parsed.value or candidate.value,
                    field="doi",
                    source=candidate.source_type,
                    confidence=confidence,
                    page=candidate.page,
                    region=(
                        "viewer_footer_url"
                        if candidate.source_type == "footer_url_ocr"
                        else "article_doi_region"
                    ),
                    evidence=candidate.line_or_context,
                    rejection_reasons=list(parsed.rejection_reasons),
                )
            )
        dois.extend(ocr.rejected_doi_candidates)
        local = extract_local_pdf_metadata(
            first_page_text="",
            filename=request.file_path.name,
            title_candidates=[item.value for item in titles if item.query_eligible],
            doi_candidates=[item.value for item in dois if item.query_eligible],
            pmid_candidates=[],
            ocr_text=ocr.combined_text,
        )
        authors = [
            AnalysisCandidate(
                value=value,
                field="author",
                source="region_ocr",
                confidence=CandidateConfidence.USABLE,
                page=1,
                region="title_author",
            )
            for value in local.authors
        ]
        journals = (
            [
                AnalysisCandidate(
                    value=local.journal,
                    field="journal",
                    source="region_ocr",
                    confidence=CandidateConfidence.USABLE,
                    page=1,
                    region="journal_header",
                )
            ]
            if local.journal
            else []
        )
        years = [
            AnalysisCandidate(
                value=value,
                field="year",
                source="region_ocr",
                confidence=CandidateConfidence.WEAK,
                page=1,
            )
            for value in ocr.year_candidates
        ]
        return DocumentAnalysisResult(
            backend=self.name,
            status=ocr.status,
            capabilities_used=set(self.capabilities),
            title_candidates=titles,
            author_candidates=authors,
            journal_candidates=journals,
            year_candidates=years,
            doi_candidates=dois,
            warnings=list(ocr.warnings),
            errors=list(ocr.errors),
            duration_ms=round((time.monotonic() - started) * 1000),
            raw_result=ocr,
        )


class DocumentAnalysisService:
    """Select interchangeable document-analysis backends without hidden fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        backends: dict[str, DocumentAnalysisBackend] | None = None,
        ocr_service=None,
    ):
        self.settings = settings
        self.backends = backends or {
            "native": NativePdfBackend(settings),
            "easyocr": EasyOcrRegionBackend(settings, ocr_service),
        }

    def analyze(
        self,
        request: DocumentAnalysisRequest,
        *,
        backend_name: str | None = None,
    ) -> DocumentAnalysisResult:
        selected = backend_name or self.settings.document_analysis.backend
        if selected == "auto":
            selected = (
                "native"
                if request.pdf_visual_type == PdfVisualType.NATIVE_TEXT
                and bool(request.native_text.strip())
                else "easyocr"
            )
        backend = self.backends.get(selected)
        if backend is None:
            return DocumentAnalysisResult(
                backend=selected,
                status="backend_unavailable",
                warnings=["document_analysis_backend_unavailable"],
                errors=[f"Document analysis backend is not installed: {selected}"],
            )
        return backend.analyze(request)


__all__ = [
    "DocumentAnalysisBackend",
    "DocumentAnalysisService",
    "EasyOcrRegionBackend",
    "NativePdfBackend",
]
