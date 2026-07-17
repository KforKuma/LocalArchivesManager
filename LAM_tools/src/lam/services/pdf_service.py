from __future__ import annotations

import copy
import logging
import re
from dataclasses import asdict
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

from pypdf import PdfReader

from ..config import Settings
from ..models import (
    CandidateConfidence,
    DocumentAnalysisRequest,
    IdentifierCandidate,
    LocalIdentityEvidence,
    OcrInspection,
    PdfInspection,
    TitleCandidate,
    PdfVisualType,
)
from ..utils.candidate_cleaning import classify_title_candidate
from ..utils.identifiers import extract_doi_candidates, extract_pmid_candidates
from ..utils.local_pdf_metadata import extract_local_pdf_metadata
from ..utils.text import (
    clean_metadata_title,
    is_probable_supplement,
    normalize_title,
    title_candidates_from_page,
)


class PdfService:
    def __init__(
        self,
        settings: Settings,
        ocr_service=None,
        visual_service=None,
        analysis_service=None,
    ):
        self.settings = settings
        self.ocr_service = ocr_service
        self.visual_service = visual_service
        self.analysis_service = analysis_service
        self._cache: dict[tuple[object, ...], PdfInspection] = {}

    def inspect(
        self,
        path: Path,
        *,
        extract_text: bool = True,
        ocr_mode: str = "auto",
        ocr_languages: tuple[str, ...] | None = None,
        ocr_dpi: int | None = None,
        ocr_gpu: str | None = None,
        ocr_trigger_reason: str | None = None,
        run_id: str | None = None,
        ocr_cache_write: bool = True,
        visual_analysis: bool = False,
    ) -> PdfInspection:
        if ocr_mode not in {"auto", "never", "always"}:
            raise ValueError("ocr_mode must be auto, never, or always")
        resolved = path.resolve()
        stat = resolved.stat()
        relative = resolved.relative_to(self.settings.library_root).as_posix()
        key = (
            str(resolved).casefold(),
            stat.st_size,
            stat.st_mtime_ns,
            extract_text,
            ocr_mode,
            ocr_languages,
            ocr_dpi,
            ocr_gpu,
            ocr_trigger_reason,
            ocr_cache_write,
            visual_analysis,
        )
        if self.settings.inspection_cache_enabled and key in self._cache:
            cached = copy.deepcopy(self._cache[key])
            cached.cache_hit = True
            return cached

        result = PdfInspection(
            relative_path=relative,
            filename=resolved.name,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        parser_warnings: list[str] = []
        handler = _PypdfWarningHandler(parser_warnings)
        pypdf_logger = logging.getLogger("pypdf")
        previous_propagate = pypdf_logger.propagate
        pypdf_logger.propagate = False
        pypdf_logger.addHandler(handler)
        reader = None
        try:
            reader = PdfReader(resolved, strict=False)
            result.is_encrypted = bool(reader.is_encrypted)
            if reader.is_encrypted:
                try:
                    if reader.decrypt("") == 0:
                        result.errors.append("pdf_encrypted")
                        return self._remember(key, result)
                except Exception:
                    result.errors.append("pdf_encrypted")
                    return self._remember(key, result)

            result.page_count = len(reader.pages)
            metadata = reader.metadata or {}
            raw_title = str(metadata.get("/Title") or "").strip()
            result.metadata_title = clean_metadata_title(
                raw_title,
                min_length=self.settings.pdf_title_min_length,
                max_length=self.settings.pdf_title_max_length,
            )
            if raw_title and not result.metadata_title:
                result.warnings.append("metadata_title_invalid")
            result.metadata_author = str(metadata.get("/Author") or "").strip()
            result.metadata_subject = str(metadata.get("/Subject") or "").strip()
            result.metadata_creator = str(metadata.get("/Creator") or "").strip()
            if result.metadata_title:
                result.title_candidates.append(
                    TitleCandidate(
                        result.metadata_title,
                        "high",
                        "metadata",
                        None,
                        "pdf_document_metadata",
                    )
                )

            metadata_text = "\n".join(
                value
                for value in (
                    result.metadata_title,
                    result.metadata_subject,
                    result.metadata_author,
                )
                if value
            )
            result.doi_candidates.extend(
                extract_doi_candidates(metadata_text, source_type="metadata")
            )
            result.pmid_candidates.extend(
                extract_pmid_candidates(metadata_text, source_type="metadata")
            )

            if extract_text:
                extracted: list[str] = []
                page_image_signals: list[dict[str, object]] = []
                total_chars = 0
                pages_to_read = min(result.page_count, self.settings.pdf_max_pages)
                for index in range(pages_to_read):
                    page_image_signals.append(self._page_image_signal(reader.pages[index]))
                    try:
                        page_text = reader.pages[index].extract_text() or ""
                    except Exception as exc:
                        result.warnings.append(f"page_{index + 1}_extract_failed:{type(exc).__name__}")
                        continue
                    page_text = page_text[: self.settings.pdf_max_chars_per_page]
                    remaining = self.settings.pdf_max_total_chars - total_chars
                    if remaining <= 0:
                        break
                    page_text = page_text[:remaining]
                    total_chars += len(page_text)
                    extracted.append(page_text)
                    if index == 0:
                        result.first_page_text = page_text
                    elif index == 1:
                        result.second_page_text = page_text
                    source_type = "first_page" if index == 0 else "sampled_page"
                    result.doi_candidates.extend(
                        extract_doi_candidates(
                            page_text, page=index + 1, source_type=source_type
                        )
                    )
                    result.pmid_candidates.extend(
                        extract_pmid_candidates(
                            page_text, page=index + 1, source_type=source_type
                        )
                    )
                    result.title_candidates.extend(
                        title_candidates_from_page(
                            page_text,
                            page=index + 1,
                            min_length=self.settings.pdf_title_min_length,
                            max_length=self.settings.pdf_title_max_length,
                        )
                    )
                result.sampled_text = "\n".join(extracted)[: self.settings.pdf_max_total_chars]
                if not result.sampled_text.strip():
                    result.warnings.append("text_unavailable")

            self._clean_native_title_candidates(result)
            pypdf_dois = list(result.doi_candidates)
            pypdf_pmids = list(result.pmid_candidates)
            pypdf_titles = list(result.title_candidates)
            result.pypdf_text_available = bool(result.sampled_text.strip())
            result.pypdf_result = {
                "text_available": result.pypdf_text_available,
                "sampled_character_count": len(result.sampled_text),
                "title_candidates": [item.value for item in pypdf_titles[:5]],
                "doi_candidates": [item.value for item in pypdf_dois],
                "pmid_candidates": [item.value for item in pypdf_pmids],
                "page_image_signals": page_image_signals if extract_text else [],
            }
            result.text_extraction_method = (
                "pypdf" if result.pypdf_text_available else "metadata_only"
            )

            if visual_analysis:
                service = self.visual_service
                if service is None:
                    from .pdf_visual_service import PdfVisualService

                    service = PdfVisualService(self.settings)
                    self.visual_service = service
                result.visual_inspection = service.inspect(
                    resolved,
                    native_text_chars=len(result.sampled_text.strip()),
                    page_count=result.page_count,
                    page_image_signals=(
                        list(result.pypdf_result.get("page_image_signals") or [])
                    ),
                    run_id=run_id or f"visual-{stat.st_mtime_ns}",
                )

            trigger_reason = self._ocr_trigger_reason(
                result, ocr_mode, extract_text, ocr_trigger_reason
            )
            if trigger_reason:
                config = replace(
                    self.settings.ocr,
                    languages=ocr_languages or self.settings.ocr.languages,
                    dpi=ocr_dpi or self.settings.ocr.dpi,
                    gpu=ocr_gpu or self.settings.ocr.gpu,
                )
                analysis = self._analysis_service().analyze(
                    DocumentAnalysisRequest(
                        file_path=resolved,
                        pdf_visual_type=(
                            result.visual_inspection.pdf_visual_type
                            if result.visual_inspection is not None
                            else PdfVisualType.UNKNOWN_IMAGE
                        ),
                        requested_fields={
                            "title",
                            "author",
                            "journal",
                            "year",
                            "doi",
                            "footer_url",
                        },
                        page_scope=(1,),
                        regions=[],
                        language_hints=config.languages,
                        resource_limits={"ocr_config": config},
                        native_text=result.first_page_text,
                        metadata_title=result.metadata_title,
                        visual_inspection=result.visual_inspection,
                        run_id=run_id or f"inspect-{stat.st_mtime_ns}",
                        trigger_reason=trigger_reason,
                        cache_write=ocr_cache_write,
                    ),
                    backend_name="easyocr",
                )
                result.analysis_results.append(analysis)
                ocr = analysis.raw_result
                if not isinstance(ocr, OcrInspection):
                    ocr = OcrInspection(
                        status=analysis.status,
                        warnings=list(analysis.warnings),
                        errors=list(analysis.errors),
                        trigger_reason=trigger_reason,
                    )
                ocr.title_candidates = [
                    TitleCandidate(
                        item.value,
                        "high"
                        if item.confidence == CandidateConfidence.TRUSTED
                        else "medium",
                        item.source,
                        item.page,
                        item.evidence,
                    )
                    for item in analysis.title_candidates
                    if item.query_eligible
                ]
                ocr.doi_candidates = [
                    IdentifierCandidate(
                        item.value,
                        page=item.page,
                        line_or_context=item.evidence,
                        confidence=(
                            "high"
                            if item.confidence == CandidateConfidence.TRUSTED
                            else "medium"
                        ),
                        source_type=item.source,
                    )
                    for item in analysis.doi_candidates
                    if item.query_eligible
                ]
                result.ocr_result = ocr
                self._merge_ocr(result, ocr, pypdf_dois, pypdf_pmids, pypdf_titles)
                if ocr.status == "success":
                    result.text_extraction_method = (
                        "pypdf+ocr" if result.pypdf_text_available else "ocr"
                    )

            filename_title = self._title_from_filename(resolved.name)
            if filename_title:
                classified_filename = classify_title_candidate(
                    filename_title,
                    source="filename",
                    page=None,
                    region="filename",
                    evidence="cleaned_filename",
                )
                if classified_filename.query_eligible:
                    result.title_candidates.append(
                        TitleCandidate(
                            filename_title,
                            "high",
                            "filename",
                            None,
                            "cleaned_filename",
                        )
                    )
            result.doi_candidates.extend(
                extract_doi_candidates(resolved.stem, source_type="filename")
            )
            result.pmid_candidates.extend(
                extract_pmid_candidates(resolved.stem, source_type="filename")
            )
            result.doi_candidates = self._unique_identifiers(result.doi_candidates)
            result.pmid_candidates = self._unique_identifiers(result.pmid_candidates)
            result.title_candidates = self._unique_titles(result.title_candidates)
            ocr_text = (
                result.ocr_result.combined_text
                if result.ocr_result is not None
                and result.ocr_result.status == "success"
                else ""
            )
            result.year_candidates = sorted(
                set(
                    re.findall(
                        r"\b(?:19|20)\d{2}\b",
                        metadata_text + "\n" + result.sampled_text + "\n" + ocr_text,
                    )
                )
            )
            local_metadata = extract_local_pdf_metadata(
                first_page_text=result.first_page_text,
                filename=result.filename,
                title_candidates=(
                    [
                        item.value
                        for item in result.title_candidates
                        if item.source_type not in {"filename", "ocr_corrected"}
                    ]
                    or [
                        item.value
                        for item in result.title_candidates
                        if item.source_type != "ocr_corrected"
                    ]
                ),
                doi_candidates=[
                    item.value
                    for item in result.doi_candidates
                    if item.source_type != "ocr_corrected"
                ],
                pmid_candidates=[item.value for item in result.pmid_candidates],
                ocr_text=ocr_text,
            )
            result.local_metadata = asdict(local_metadata)
            result.identity_evidence = LocalIdentityEvidence(
                title_candidates=[item.value for item in result.title_candidates],
                author_candidates=list(local_metadata.authors),
                year_candidates=list(result.year_candidates),
                journal_candidates=list(
                    dict.fromkeys(
                        [
                            *result.journal_candidates,
                            *(
                                [local_metadata.journal]
                                if local_metadata.journal
                                else []
                            ),
                        ]
                    )
                ),
                doi_candidates=list(result.doi_candidates),
                source_order=list(
                    dict.fromkeys(
                        item.source_type
                        for item in [*result.title_candidates, *result.doi_candidates]
                    )
                ),
            )
            result.is_probable_supplement = is_probable_supplement(
                result.filename,
                result.metadata_title,
                result.metadata_subject,
                result.first_page_text[:2000],
            )
            result.is_readable = True
        except Exception as exc:
            result.errors.append(f"pdf_unreadable:{type(exc).__name__}")
        finally:
            stream = getattr(reader, "stream", None)
            close = getattr(stream, "close", None)
            if callable(close):
                close()
            pypdf_logger.removeHandler(handler)
            pypdf_logger.propagate = previous_propagate
            result.warnings = list(
                dict.fromkeys([*result.warnings, *parser_warnings])
            )
        return self._remember(key, result)

    def _analysis_service(self):
        if self.analysis_service is None:
            from .document_analysis_service import DocumentAnalysisService

            self.analysis_service = DocumentAnalysisService(
                self.settings, ocr_service=self.ocr_service
            )
        return self.analysis_service

    def _clean_native_title_candidates(self, result: PdfInspection) -> None:
        native_analysis = self._analysis_service().analyze(
            DocumentAnalysisRequest(
                file_path=self.settings.library_root / result.relative_path,
                pdf_visual_type=(
                    PdfVisualType.NATIVE_TEXT
                    if result.first_page_text.strip()
                    else PdfVisualType.UNKNOWN_IMAGE
                ),
                requested_fields={"metadata", "native_text", "title", "doi", "year"},
                page_scope=(1,),
                language_hints=self.settings.ocr.languages,
                resource_limits={
                    "max_chars": self.settings.pdf_max_chars_per_page,
                },
                native_text=result.first_page_text,
                metadata_title=result.metadata_title,
            ),
            backend_name="native",
        )
        result.analysis_results.append(native_analysis)
        cleaned: list[TitleCandidate] = []
        for candidate in result.title_candidates:
            classified = classify_title_candidate(
                candidate.value,
                source=candidate.source_type,
                page=candidate.page,
                region=(
                    "pdf_metadata"
                    if candidate.source_type == "metadata"
                    else "native_page_top"
                ),
                evidence=candidate.evidence,
            )
            if classified.query_eligible:
                cleaned.append(candidate)
            else:
                if (
                    candidate.source_type == "metadata"
                    and "metadata_title_contaminated"
                    in classified.rejection_reasons
                ):
                    result.metadata_title = ""
                    if "metadata_title_contaminated" not in result.warnings:
                        result.warnings.append("metadata_title_contaminated")
        result.title_candidates = cleaned

    def _ocr_trigger_reason(
        self,
        inspection: PdfInspection,
        mode: str,
        extract_text: bool,
        forced_reason: str | None = None,
    ) -> str:
        if not extract_text or mode == "never" or not self.settings.ocr.enabled:
            return ""
        if mode == "always":
            return forced_reason or "user_forced"
        text = inspection.first_page_text.strip()
        if not text:
            return "first_page_text_empty"
        if len(text) < self.settings.ocr.min_text_chars:
            return "first_page_text_too_short"
        printable = sum(character.isprintable() for character in text) / max(1, len(text))
        if printable < 0.85 or text.count("\ufffd") > max(2, len(text) // 20):
            return "first_page_text_abnormal"
        if not (
            inspection.title_candidates
            or inspection.doi_candidates
            or inspection.pmid_candidates
        ):
            return "no_identification_candidates"
        return ""

    @staticmethod
    def _merge_ocr(
        inspection: PdfInspection,
        ocr: OcrInspection,
        pypdf_dois: list[IdentifierCandidate],
        pypdf_pmids: list[IdentifierCandidate],
        pypdf_titles: list[TitleCandidate],
    ) -> None:
        if ocr.status != "success":
            for issue in [*ocr.errors, *ocr.warnings]:
                if issue not in inspection.warnings:
                    inspection.warnings.append(issue)
            return
        pypdf_doi_values = {item.value for item in pypdf_dois}
        ocr_doi_values = {item.value for item in ocr.doi_candidates}
        pypdf_pmid_values = {item.value for item in pypdf_pmids}
        ocr_pmid_values = {item.value for item in ocr.pmid_candidates}
        if (
            pypdf_doi_values
            and ocr_doi_values
            and pypdf_doi_values.isdisjoint(ocr_doi_values)
        ) or (
            pypdf_pmid_values
            and ocr_pmid_values
            and pypdf_pmid_values.isdisjoint(ocr_pmid_values)
        ):
            inspection.warnings.append("candidate_disagreement")
        pypdf_title_values = [normalize_title(item.value) for item in pypdf_titles]
        ocr_title_values = [normalize_title(item.value) for item in ocr.title_candidates]
        if pypdf_title_values and ocr_title_values:
            similarities = [
                SequenceMatcher(None, left, right).ratio()
                for left in pypdf_title_values
                for right in ocr_title_values
                if left and right
            ]
            if similarities and max(similarities) < 0.80:
                inspection.warnings.append("candidate_disagreement")
        inspection.doi_candidates = PdfService._unique_identifiers(
            [*pypdf_dois, *ocr.doi_candidates]
        )
        inspection.pmid_candidates = PdfService._unique_identifiers(
            [*pypdf_pmids, *ocr.pmid_candidates]
        )
        inspection.title_candidates = PdfService._unique_titles(
            [*pypdf_titles, *ocr.title_candidates]
        )
        inspection.year_candidates = sorted(
            set([*inspection.year_candidates, *ocr.year_candidates])
        )
        for warning in ocr.warnings:
            if warning not in inspection.warnings:
                inspection.warnings.append(warning)

    def _remember(
        self,
        key: tuple[str, int, int, bool],
        inspection: PdfInspection,
    ) -> PdfInspection:
        if self.settings.inspection_cache_enabled:
            self._cache[key] = copy.deepcopy(inspection)
        return inspection

    @staticmethod
    def _page_image_signal(page: object) -> dict[str, object]:
        signal: dict[str, object] = {
            "image_count": 0,
            "max_image_pixels": 0,
            "large_page_image": False,
        }
        try:
            resources = page.get("/Resources") or {}
            xobjects = resources.get("/XObject") or {}
            if hasattr(xobjects, "get_object"):
                xobjects = xobjects.get_object()
            widths: list[int] = []
            heights: list[int] = []
            for reference in xobjects.values():
                item = reference.get_object() if hasattr(reference, "get_object") else reference
                if str(item.get("/Subtype") or "") != "/Image":
                    continue
                widths.append(int(item.get("/Width") or 0))
                heights.append(int(item.get("/Height") or 0))
            pixels = [width * height for width, height in zip(widths, heights)]
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)
            signal.update(
                {
                    "image_count": len(pixels),
                    "max_image_pixels": max(pixels, default=0),
                    "large_page_image": any(
                        pixels[index] >= 250_000
                        and widths[index] >= page_width
                        and heights[index] >= page_height
                        for index in range(len(pixels))
                    ),
                }
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        return signal

    @staticmethod
    def _title_from_filename(filename: str) -> str:
        stem = Path(filename).stem
        match = re.match(r"^.+?,\s*(?:19|20)\d{2}(?:,\s*[^-]+)?\s+-\s+(.+)$", stem)
        value = match.group(1) if match else stem
        if not match:
            value = re.sub(
                r"\s+-\s+(?:Anna['’]s Archive|PDF\.js|ScienceDirect).*$",
                "",
                value,
                flags=re.I,
            )
            if "_" in value:
                first, rest = value.split("_", 1)
                value = f"{first}: {rest}"
            value = value.replace("_", " ")
        value = re.sub(
            r"\s+-\s+(?:Supplementary|Supporting)\b.*$", "", value, flags=re.I
        )
        return value.strip()

    @staticmethod
    def _unique_identifiers(
        candidates: list[IdentifierCandidate],
    ) -> list[IdentifierCandidate]:
        source_priority = {
            "metadata": 0,
            "filename": 1,
            "first_page": 2,
            "sampled_page": 2,
            "doi_region_ocr": 3,
            "footer_url_ocr": 4,
            "region_ocr": 5,
            "ocr": 6,
            "ocr_corrected": 7,
        }
        unique: dict[str, IdentifierCandidate] = {}
        for candidate in candidates:
            existing = unique.get(candidate.value)
            if existing is None or (
                source_priority.get(candidate.source_type, 50)
                < source_priority.get(existing.source_type, 50)
            ) or (
                source_priority.get(candidate.source_type, 50)
                == source_priority.get(existing.source_type, 50)
                and existing.confidence != "high"
                and candidate.confidence == "high"
            ):
                unique[candidate.value] = candidate
        return sorted(
            unique.values(),
            key=lambda item: source_priority.get(item.source_type, 50),
        )

    @staticmethod
    def _unique_titles(candidates: list[TitleCandidate]) -> list[TitleCandidate]:
        unique: dict[str, TitleCandidate] = {}
        for candidate in candidates:
            key = normalize_title(candidate.value)
            if key and key not in unique:
                unique[key] = candidate
        return list(unique.values())


class _PypdfWarningHandler(logging.Handler):
    """Collapse noisy pypdf font-dictionary diagnostics into one safe warning."""

    FONT_MARKERS = (
        "/ascent",
        "font dictionary",
        "advanced encoding",
        "multiple definitions",
    )

    def __init__(self, target: list[str]):
        super().__init__(level=logging.WARNING)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage().casefold()
        if any(marker in message for marker in self.FONT_MARKERS):
            if "pypdf_font_dictionary_warning" not in self.target:
                self.target.append("pypdf_font_dictionary_warning")
