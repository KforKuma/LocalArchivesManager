from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from ..models import ReferenceBatch, ReferenceCandidate
from ..utils.identifiers import (
    extract_doi_candidates,
    extract_pmid_candidates,
    normalize_arxiv_id,
)
from ..utils.text import normalize_title


REFERENCE_MARKER = re.compile(
    r"^\s*(?:\[(?P<bracket>\d{1,4})\]|(?P<number>\d{1,4})[.)]|(?P<bullet>[•●▪◦*]))\s*",
    re.M,
)
YEAR_PATTERN = re.compile(r"\b(?:18|19|20)\d{2}\b")
ARXIV_INLINE = re.compile(
    r"(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
    re.I,
)
VOLUME_PATTERN = re.compile(
    r"\b(?:vol(?:ume)?\.?\s*)?(\d{1,4})(?:\s*\(\s*\d{1,4}\s*\))?\s*[:;,]\s*(\d{1,6}(?:\s*[-–]\s*\d{1,6})?)\b",
    re.I,
)


class ReferenceTextParser:
    """Deterministic, conservative parser for copied bibliography text."""

    def parse_file(self, path: Path) -> ReferenceBatch:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig", errors="replace")
        normalized = self.normalize(text)
        candidates = self.segment(normalized, source_file=path.name)
        score = self.detection_score(normalized, candidates)
        recognized = len(candidates) >= 2 and score >= 4
        warnings = [] if recognized else ["plain_text_not_recognized_as_reference_list"]
        return ReferenceBatch(
            source_file=path.name,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            recognized=recognized,
            detection_score=score,
            candidates=candidates,
            warnings=warnings,
        )

    @staticmethod
    def normalize(text: str) -> str:
        value = unicodedata.normalize("NFKC", text or "")
        value = re.sub(r"[\u200b-\u200d\ufeff]", "", value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in value.splitlines()).strip()

    def segment(self, text: str, *, source_file: str) -> list[ReferenceCandidate]:
        lines = text.splitlines()
        groups: list[tuple[int, int, list[str]]] = []
        current: list[str] = []
        start = 1
        saw_numbering = False

        def flush(end: int) -> None:
            nonlocal current, start
            if current and any(item.strip() for item in current):
                groups.append((start, end, list(current)))
            current = []

        for line_number, line in enumerate(lines, start=1):
            marker = REFERENCE_MARKER.match(line)
            if marker:
                flush(line_number - 1)
                saw_numbering = True
                start = line_number
                current = [line[marker.end() :].strip()]
                continue
            if not line.strip():
                if current and not saw_numbering:
                    flush(line_number - 1)
                elif current:
                    current.append("")
                continue
            if not current:
                start = line_number
            current.append(line.strip())
        flush(len(lines))

        candidates: list[ReferenceCandidate] = []
        for index, (line_start, line_end, raw_lines) in enumerate(groups, start=1):
            joined = self._join_soft_lines(raw_lines)
            if len(joined) < 12:
                continue
            candidates.append(
                self._candidate(
                    source_file,
                    index,
                    "\n".join(raw_lines).strip(),
                    joined,
                    line_start,
                    line_end,
                )
            )
        return candidates

    @staticmethod
    def _join_soft_lines(lines: list[str]) -> str:
        parts: list[str] = []
        for line in lines:
            text = re.sub(r"\s+", " ", line).strip()
            if not text:
                continue
            if parts and parts[-1].endswith("-") and re.match(r"^[a-z]", text):
                parts[-1] = parts[-1][:-1] + text
            else:
                parts.append(text)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def _candidate(
        self,
        source_file: str,
        index: int,
        raw_text: str,
        text: str,
        line_start: int,
        line_end: int,
    ) -> ReferenceCandidate:
        dois = [item.value for item in extract_doi_candidates(text, source_type="reference_text")]
        pmids = [item.value for item in extract_pmid_candidates(text, source_type="reference_text")]
        arxiv = list(
            dict.fromkeys(
                value
                for match in ARXIV_INLINE.finditer(text)
                if (value := normalize_arxiv_id(match.group(1)))
            )
        )
        years = list(dict.fromkeys(YEAR_PATTERN.findall(text)))
        volumes = []
        pages = []
        for match in VOLUME_PATTERN.finditer(text):
            volumes.append(match.group(1))
            pages.append(re.sub(r"\s+", "", match.group(2)))
        titles, authors, journals, warnings = self._bibliographic_candidates(text)
        if not any((dois, pmids, arxiv, titles)):
            warnings.append("reference_identity_candidate_missing")
        return ReferenceCandidate(
            source_file=source_file,
            reference_index=index,
            raw_text=raw_text,
            normalized_text=text,
            line_start=line_start,
            line_end=line_end,
            doi_candidates=dois,
            pmid_candidates=pmids,
            arxiv_candidates=arxiv,
            title_candidates=titles,
            author_candidates=authors,
            year_candidates=years,
            journal_candidates=journals,
            volume_candidates=list(dict.fromkeys(volumes)),
            page_candidates=list(dict.fromkeys(pages)),
            parse_warnings=list(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _bibliographic_candidates(
        text: str,
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        cleaned = REFERENCE_MARKER.sub("", text)
        cleaned = re.sub(r"https?://(?:dx\.)?doi\.org/\S+", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"\bdoi\s*:\s*10\.\S+", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"\bPMID\s*[:#]?\s*\d+\b", " ", cleaned, flags=re.I)
        quoted = [
            re.sub(r"\s+", " ", value).strip(" \"'[]{}—–-")
            for value in re.findall(r"[\"“‘\[]([^\"”’\]]{18,300})[\"”’\]]", cleaned)
        ]
        segments = [
            re.sub(r"\s+", " ", value).strip(" \"'[]{}—–-")
            for value in re.split(r"(?<=[.!?。])\s+", cleaned)
            if value.strip()
        ]
        scored: list[tuple[int, str]] = []
        for position, segment in enumerate([*quoted, *segments]):
            words = re.findall(r"[A-Za-z\u3400-\u9fff][\w'-]*", segment)
            if not 3 <= len(words) <= 45 or len(segment) > 350:
                continue
            if re.fullmatch(r".*(?:18|19|20)\d{2}\s*;?", segment) and len(words) < 6:
                continue
            if VOLUME_PATTERN.search(segment) or re.match(
                r"^(?:vol(?:ume)?|no\.?|pp?\.?|doi|pmid|arxiv)\b", segment, re.I
            ):
                continue
            comma_density = segment.count(",") / max(1, len(words))
            score = len(words) + (6 if position > 0 else 0) - int(comma_density * 10)
            if re.search(r"\bet\s+al\.?\b", segment, re.I):
                score -= 5
            scored.append((score, segment.rstrip(". ")))
        titles = [value for _, value in sorted(scored, reverse=True)[:2]]
        titles = list(
            dict.fromkeys(value for value in titles if len(normalize_title(value)) >= 12)
        )
        first_title = titles[0] if titles else ""
        prefix = cleaned.split(first_title, 1)[0].strip(" .;,:") if first_title else ""
        authors = [prefix[:300]] if prefix and ("," in prefix or re.search(r"\bet\s+al\b", prefix, re.I)) else []
        suffix = cleaned.split(first_title, 1)[1] if first_title and first_title in cleaned else ""
        journal_match = re.match(r"\s*[.;]\s*([^.;]{3,120})", suffix)
        journals = []
        if journal_match:
            value = YEAR_PATTERN.split(journal_match.group(1), 1)[0].strip(" .;,:")
            if value and not VOLUME_PATTERN.fullmatch(value):
                journals.append(value)
        warnings = [] if titles else ["reference_title_candidate_missing"]
        return titles, authors, journals, warnings

    @staticmethod
    def detection_score(text: str, candidates: list[ReferenceCandidate]) -> int:
        score = 0
        numbered = len(REFERENCE_MARKER.findall(text))
        identifiers = sum(
            bool(item.doi_candidates or item.pmid_candidates or item.arxiv_candidates)
            for item in candidates
        )
        years = sum(bool(item.year_candidates) for item in candidates)
        titles = sum(bool(item.title_candidates) for item in candidates)
        score += 2 if numbered >= 2 else 0
        score += 2 if identifiers >= 2 else int(identifiers > 0)
        score += 2 if years >= 2 else int(years > 0)
        score += 2 if titles >= 2 else int(titles > 0)
        score += int(any(item.volume_candidates or item.page_candidates for item in candidates))
        return score
