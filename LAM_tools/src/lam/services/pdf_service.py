from __future__ import annotations

import copy
import re
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

from pypdf import PdfReader

from ..config import Settings
from ..models import IdentifierCandidate, OcrInspection, PdfInspection, TitleCandidate
from ..utils.identifiers import extract_doi_candidates, extract_pmid_candidates
from ..utils.text import (
    clean_metadata_title,
    is_probable_supplement,
    normalize_title,
    title_candidates_from_page,
)


class PdfService:
    def __init__(self, settings: Settings, ocr_service=None):
        self.settings = settings
        self.ocr_service = ocr_service
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
        run_id: str | None = None,
        ocr_cache_write: bool = True,
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
            ocr_cache_write,
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
                total_chars = 0
                pages_to_read = min(result.page_count, self.settings.pdf_max_pages)
                for index in range(pages_to_read):
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
            }
            result.text_extraction_method = (
                "pypdf" if result.pypdf_text_available else "metadata_only"
            )

            trigger_reason = self._ocr_trigger_reason(result, ocr_mode, extract_text)
            if trigger_reason:
                service = self.ocr_service
                if service is None:
                    from .ocr_service import OcrService

                    service = OcrService(self.settings)
                    self.ocr_service = service
                config = replace(
                    self.settings.ocr,
                    languages=ocr_languages or self.settings.ocr.languages,
                    dpi=ocr_dpi or self.settings.ocr.dpi,
                    gpu=ocr_gpu or self.settings.ocr.gpu,
                )
                ocr = service.inspect_first_page(
                    resolved,
                    run_id=run_id or f"inspect-{stat.st_mtime_ns}",
                    trigger_reason=trigger_reason,
                    config=config,
                    cache_write=ocr_cache_write,
                )
                result.ocr_result = ocr
                self._merge_ocr(result, ocr, pypdf_dois, pypdf_pmids, pypdf_titles)
                if ocr.status == "success":
                    result.text_extraction_method = (
                        "pypdf+ocr" if result.pypdf_text_available else "ocr"
                    )

            filename_title = self._title_from_filename(resolved.name)
            if filename_title:
                result.title_candidates.append(
                    TitleCandidate(filename_title, "medium", "filename", None)
                )
            result.doi_candidates = self._unique_identifiers(result.doi_candidates)
            result.pmid_candidates = self._unique_identifiers(result.pmid_candidates)
            result.title_candidates = self._unique_titles(result.title_candidates)
            result.year_candidates = sorted(
                set(re.findall(r"\b(?:19|20)\d{2}\b", metadata_text + "\n" + result.sampled_text))
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
        return self._remember(key, result)

    def _ocr_trigger_reason(
        self, inspection: PdfInspection, mode: str, extract_text: bool
    ) -> str:
        if not extract_text or mode == "never" or not self.settings.ocr.enabled:
            return ""
        if mode == "always":
            return "user_forced"
        text = inspection.first_page_text.strip()
        if not text:
            return "first_page_text_empty"
        if len(text) < self.settings.ocr.min_text_chars:
            return "first_page_text_too_short"
        printable = sum(character.isprintable() for character in text) / max(1, len(text))
        if printable < 0.85 or text.count("�") > max(2, len(text) // 20):
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
            inspection.warnings.append("pdf_text_ocr_conflict")
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
                inspection.warnings.append("pdf_text_ocr_conflict")
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
    def _title_from_filename(filename: str) -> str:
        stem = Path(filename).stem
        match = re.match(r"^.+?,\s*(?:19|20)\d{2}(?:,\s*[^-]+)?\s+-\s+(.+)$", stem)
        value = match.group(1) if match else ""
        value = re.sub(
            r"\s+-\s+(?:Supplementary|Supporting)\b.*$", "", value, flags=re.I
        )
        return value.strip()

    @staticmethod
    def _unique_identifiers(
        candidates: list[IdentifierCandidate],
    ) -> list[IdentifierCandidate]:
        unique: dict[str, IdentifierCandidate] = {}
        for candidate in candidates:
            existing = unique.get(candidate.value)
            if existing is None or (
                existing.confidence != "high" and candidate.confidence == "high"
            ):
                unique[candidate.value] = candidate
        return list(unique.values())

    @staticmethod
    def _unique_titles(candidates: list[TitleCandidate]) -> list[TitleCandidate]:
        unique: dict[str, TitleCandidate] = {}
        for candidate in candidates:
            key = normalize_title(candidate.value)
            if key and key not in unique:
                unique[key] = candidate
        return list(unique.values())
