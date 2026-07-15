from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

from ..models import CatalogueRecord, MatchResult, MatchStatus, PdfInspection
from ..utils.identifiers import normalize_doi, normalize_pmid
from ..utils.normalize import normalized_relative_path, normalized_text
from ..utils.text import normalize_title
from ..utils.title_matching import tolerant_title_score, titles_tolerantly_equivalent


class MatchingService:
    def match(
        self,
        records: Iterable[CatalogueRecord],
        *,
        relative_path: str,
        filename: str,
        inspection: PdfInspection | None = None,
        confirmed_catalogue_id: str | None = None,
    ) -> MatchResult:
        rows = list(records)
        path_matches = self._matches(
            rows,
            "pdf_relative_path",
            normalized_relative_path(relative_path),
            normalized_relative_path,
        )
        filename_matches = self._matches(
            rows, "pdf_filename", normalized_text(filename), normalized_text
        )
        parsed_filename_title = self._title_from_standard_filename(filename)
        filename_title_matches = {
            row.row_number
            for row in rows
            if parsed_filename_title
            and normalize_title(row.get("title")) == parsed_filename_title
        }

        doi_matches: set[int] = set()
        pmid_matches: set[int] = set()
        title_matches: set[int] = set()
        tolerant_title_matches: set[int] = set()
        years: set[str] = set()
        if inspection:
            doi_values = {
                item.value
                for item in inspection.doi_candidates
                if item.source_type != "ocr_corrected"
            }
            pmid_values = {item.value for item in inspection.pmid_candidates}
            title_values = {
                normalize_title(item.value)
                for item in inspection.title_candidates
                if normalize_title(item.value)
            }
            years = set(inspection.year_candidates)
            doi_matches = {
                row.row_number
                for row in rows
                if normalize_doi(row.get("doi")) in doi_values
                and normalize_doi(row.get("doi"))
            }
            pmid_matches = {
                row.row_number
                for row in rows
                if normalize_pmid(row.get("pmid")) in pmid_values
                and normalize_pmid(row.get("pmid"))
            }
            title_matches = {
                row.row_number
                for row in rows
                if normalize_title(row.get("title")) in title_values
                and normalize_title(row.get("title"))
            }
            tolerant_title_matches = {
                row.row_number
                for row in rows
                if row.get("title")
                and any(
                    titles_tolerantly_equivalent(row.get("title"), item.value)
                    for item in inspection.title_candidates
                )
            }

        if confirmed_catalogue_id:
            confirmed = [
                row
                for row in rows
                if normalized_text(row.get("id")) == normalized_text(confirmed_catalogue_id)
            ]
            if len(confirmed) != 1:
                return self._blocked(
                    "paper_identity_ambiguous",
                    [row.row_number for row in confirmed],
                    "confirmed_catalogue_id_not_unique",
                )
            row = confirmed[0]
            identifier_rows = doi_matches | pmid_matches
            if identifier_rows and row.row_number not in identifier_rows:
                return self._conflict(
                    "identifier_conflict",
                    sorted(identifier_rows | {row.row_number}),
                    "user_confirmation_conflicts_with_identifier",
                )
            return self._matched(row, "user_confirmed", "exact_identifier")

        if len(doi_matches) > 1 or len(pmid_matches) > 1:
            return self._conflict(
                "catalogue_duplicate_identifier",
                sorted(doi_matches | pmid_matches),
                "identifier_matches_multiple_catalogue_rows",
            )
        if doi_matches and pmid_matches and doi_matches != pmid_matches:
            return self._conflict(
                "identifier_conflict",
                sorted(doi_matches | pmid_matches),
                "doi_and_pmid_point_to_different_rows",
            )

        identifier_rows = doi_matches | pmid_matches
        local_rows = set(path_matches) | set(filename_matches)
        if identifier_rows and local_rows and identifier_rows.isdisjoint(local_rows):
            return self._conflict(
                "identifier_conflict",
                sorted(identifier_rows | local_rows),
                "filename_or_path_conflicts_with_pdf_identifier",
            )
        if len(identifier_rows) == 1:
            row_number = next(iter(identifier_rows))
            if title_matches and row_number not in title_matches:
                return self._conflict(
                    "identifier_conflict",
                    sorted(identifier_rows | title_matches),
                    "identifier_and_title_point_to_different_rows",
                )
            row = self._row(rows, row_number)
            method = "doi" if doi_matches else "pmid"
            return self._matched(row, method, "exact_identifier")

        if len(path_matches) == 1:
            return self._matched(
                self._row(rows, next(iter(path_matches))),
                "pdf_relative_path",
                "exact_title_supported",
            )
        if len(path_matches) > 1:
            return self._blocked(
                "paper_identity_ambiguous", sorted(path_matches), "duplicate_pdf_relative_path"
            )
        if len(filename_matches) == 1:
            return self._matched(
                self._row(rows, next(iter(filename_matches))),
                "pdf_filename",
                "exact_title_supported",
            )
        if len(filename_matches) > 1:
            return self._blocked(
                "paper_identity_ambiguous", sorted(filename_matches), "duplicate_pdf_filename"
            )
        if len(filename_title_matches) == 1:
            return self._matched(
                self._row(rows, next(iter(filename_title_matches))),
                "standard_filename_title",
                "exact_title_only",
            )
        if len(filename_title_matches) > 1:
            return self._blocked(
                "paper_identity_ambiguous",
                sorted(filename_title_matches),
                "standard_filename_title_matches_multiple_rows",
            )

        if title_matches:
            supported = set(title_matches)
            if years:
                by_year = {
                    row_number
                    for row_number in title_matches
                    if str(self._row(rows, row_number).get("year") or "").strip() in years
                }
                if by_year:
                    supported = by_year
            if len(supported) == 1:
                row = self._row(rows, next(iter(supported)))
                confidence = "exact_title_supported" if years else "exact_title_only"
                if inspection and inspection.is_probable_supplement and not years:
                    return self._blocked(
                        "supplement_parent_unknown",
                        sorted(title_matches),
                        "supplement_requires_supported_parent_match",
                    )
                return self._matched(row, "normalized_title", confidence)
            return self._blocked(
                "paper_identity_ambiguous",
                sorted(supported),
                "normalized_title_matches_multiple_rows",
            )

        if tolerant_title_matches and years:
            supported = {
                row_number
                for row_number in tolerant_title_matches
                if str(self._row(rows, row_number).get("year") or "").strip() in years
            }
            if len(supported) == 1:
                return self._matched(
                    self._row(rows, next(iter(supported))),
                    "tolerant_title",
                    "exact_title_supported",
                )
            if supported:
                return self._blocked(
                    "paper_identity_ambiguous",
                    sorted(supported),
                    "tolerant_title_matches_multiple_rows",
                )

        fuzzy = self._fuzzy_title_candidates(rows, inspection)
        if fuzzy:
            return self._blocked(
                "paper_identity_ambiguous", fuzzy, "fuzzy_title_requires_review"
            )
        return MatchResult(
            status=MatchStatus.NOT_FOUND,
            confidence="insufficient",
            method="none",
            requires_metadata_lookup=True,
            issue_key="paper_identity_not_found",
        )

    @staticmethod
    def _matches(rows, field_name, key, normalizer) -> set[int]:
        if not key:
            return set()
        return {
            row.row_number
            for row in rows
            if normalizer(row.get(field_name)) == key
        }

    @staticmethod
    def _row(rows: list[CatalogueRecord], row_number: int) -> CatalogueRecord:
        return next(row for row in rows if row.row_number == row_number)

    @staticmethod
    def _matched(
        row: CatalogueRecord,
        method: str,
        confidence: str,
    ) -> MatchResult:
        return MatchResult(
            status=(
                MatchStatus.EXACT
                if confidence in {"exact_identifier", "exact_title_supported"}
                else MatchStatus.HIGH_CONFIDENCE
            ),
            matched_row_id=row.row_number,
            matched_catalogue_id=str(row.get("id") or ""),
            confidence=confidence,
            method=method,
            candidate_rows=[row.row_number],
        )

    @staticmethod
    def _blocked(issue_key: str, rows: list[int], reason: str) -> MatchResult:
        return MatchResult(
            status=MatchStatus.AMBIGUOUS,
            confidence="ambiguous",
            method="blocked",
            candidate_rows=rows,
            conflicts=[reason],
            requires_metadata_lookup=False,
            issue_key=issue_key,
        )

    @staticmethod
    def _conflict(issue_key: str, rows: list[int], reason: str) -> MatchResult:
        return MatchResult(
            status=MatchStatus.CONFLICT,
            confidence="conflict",
            method="conflict",
            candidate_rows=rows,
            conflicts=[reason],
            requires_metadata_lookup=False,
            issue_key=issue_key,
        )

    @staticmethod
    def _fuzzy_title_candidates(
        rows: list[CatalogueRecord], inspection: PdfInspection | None
    ) -> list[int]:
        if not inspection:
            return []
        titles = [item.value for item in inspection.title_candidates if item.value]
        scored: list[tuple[float, int]] = []
        for row in rows:
            catalogue_title = str(row.get("title") or "")
            if not catalogue_title:
                continue
            score = max(
                (tolerant_title_score(title, catalogue_title) for title in titles),
                default=0.0,
            )
            if score >= 0.9:
                scored.append((score, row.row_number))
        return [row for _, row in sorted(scored, reverse=True)[:5]]

    @staticmethod
    def _title_from_standard_filename(filename: str) -> str:
        stem = Path(filename).stem
        match = re.match(
            r"^.+?,\s*(?:19|20)\d{2}(?:,\s*[^-]+)?\s+-\s+(.+)$",
            stem,
            re.I,
        )
        if not match:
            return ""
        title = re.sub(
            r"\s+-\s+(?:Supplementary|Supporting)\b.*$",
            "",
            match.group(1),
            flags=re.I,
        )
        return normalize_title(title)
