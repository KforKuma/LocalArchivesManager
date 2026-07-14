from __future__ import annotations

import copy
import re
from pathlib import Path

from pypdf import PdfReader

from ..config import Settings
from ..models import IdentifierCandidate, PdfInspection, TitleCandidate
from ..utils.identifiers import extract_doi_candidates, extract_pmid_candidates
from ..utils.text import (
    clean_metadata_title,
    is_probable_supplement,
    normalize_title,
    title_candidates_from_page,
)


class PdfService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[tuple[str, int, int, bool], PdfInspection] = {}

    def inspect(self, path: Path, *, extract_text: bool = True) -> PdfInspection:
        resolved = path.resolve()
        stat = resolved.stat()
        relative = resolved.relative_to(self.settings.library_root).as_posix()
        key = (str(resolved).casefold(), stat.st_size, stat.st_mtime_ns, extract_text)
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
