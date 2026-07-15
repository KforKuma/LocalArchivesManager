from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .identifiers import normalize_doi, normalize_pmid
from .text import normalize_title
from .title_matching import filename_evidence, tolerant_title_score


_ADMINISTRATIVE = re.compile(
    r"^(?:accepted|received|published(?:\s+online)?|copyright|©|pii\s*:|doi\s*:)",
    re.I,
)
_AFFILIATION_WORDS = re.compile(
    r"\b(?:university|college|school|department|institute|laboratory|hospital|academy|"
    r"republic|china|korea|usa|address|correspondence|author details)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class LocalPdfMetadata:
    title: str = ""
    authors: tuple[str, ...] = ()
    year: str = ""
    journal: str = ""
    doi: str = ""
    pmid: str = ""
    publication_type: str = ""
    abstract: str = ""
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def catalogue_fields(self) -> dict[str, str]:
        return {
            "title": self.title,
            "authors": "; ".join(self.authors),
            "year": self.year,
            "journal": self.journal,
            "doi": self.doi,
            "pmid": self.pmid,
            "publication_type": self.publication_type,
        }


def _clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def _best_title(lines: list[str], filename: str, title_candidates: list[str]) -> str:
    filename_title = ""
    evidence = filename_evidence(filename)
    if evidence.title_candidate:
        filename_title = evidence.title_candidate.value
    candidates = [
        item.strip()
        for item in title_candidates
        if item.strip()
        and not _ADMINISTRATIVE.search(item.strip())
        and 8 <= len(item.strip()) <= 300
    ]
    if filename_title and candidates:
        best = max(candidates, key=lambda item: tolerant_title_score(item, filename_title))
        if tolerant_title_score(best, filename_title) >= 0.88:
            return best
    if filename_title:
        return filename_title
    return candidates[0] if candidates else ""


def _authors(lines: list[str], title: str) -> tuple[str, ...]:
    abstract_index = next(
        (index for index, line in enumerate(lines) if line.casefold() == "abstract"),
        len(lines),
    )
    title_tokens = set(normalize_title(title).split())
    candidates: list[str] = []
    for line in lines[:abstract_index]:
        normalized = normalize_title(line)
        if not normalized or _ADMINISTRATIVE.search(line):
            continue
        if title_tokens and len(title_tokens & set(normalized.split())) >= max(3, len(title_tokens) // 2):
            continue
        if "@" in line or _AFFILIATION_WORDS.search(line) or "doi.org" in line.casefold():
            continue
        if not re.search(r"[·;]|(?:and|&)|,", line, re.I):
            continue
        candidates.append(line)
    if not candidates:
        return ()
    block = " ".join(candidates[-2:])
    parts = re.split(r"\s*(?:·|;|\band\b|&)\s*|,\s*(?=[A-Z])", block, flags=re.I)
    names: list[str] = []
    for part in parts:
        name = re.sub(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])\d+(?:,\d+)*$", "", part.strip())
        name = re.sub(r"\s+", " ", name).strip(" ,")
        words = name.split()
        if not 2 <= len(words) <= 7 or _AFFILIATION_WORDS.search(name):
            continue
        if not all(re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", word) for word in words):
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def _abstract(text: str) -> str:
    match = re.search(
        r"(?:^|\n)\s*Abstract\s*\n(?P<body>.*?)(?=\n\s*(?:Keywords?|Introduction|1\s+Introduction)\b)",
        text,
        re.I | re.S,
    )
    if not match:
        return ""
    value = re.sub(r"-\s*\n\s*", "", match.group("body"))
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) >= 80 else ""


def _journal(lines: list[str], title: str) -> str:
    title_key = normalize_title(title)
    for line in lines:
        if _ADMINISTRATIVE.search(line) or "doi.org" in line.casefold():
            continue
        if title_key and normalize_title(line) == title_key:
            continue
        match = re.match(r"^(.+?)\s*\((?:19|20)\d{2}\)\s+\d", line)
        if match and not _AFFILIATION_WORDS.search(match.group(1)):
            return match.group(1).strip()
    return ""


def extract_local_pdf_metadata(
    *,
    first_page_text: str,
    filename: str,
    title_candidates: list[str],
    doi_candidates: list[str],
    pmid_candidates: list[str],
) -> LocalPdfMetadata:
    lines = _clean_lines(first_page_text)
    title = _best_title(lines, filename, title_candidates)
    authors = _authors(lines, title)
    abstract = _abstract(first_page_text)
    journal = _journal(lines, title)
    evidence = filename_evidence(Path(filename).name)
    years = re.findall(r"\b(?:19|20)\d{2}\b", "\n".join(lines[:20]))
    year = evidence.year or (years[0] if years else "")
    doi = next((normalize_doi(item) for item in doi_candidates if normalize_doi(item)), "")
    pmid = next((normalize_pmid(item) for item in pmid_candidates if normalize_pmid(item)), "")
    publication_type = evidence.publication_type or ""
    sources = {
        field_name: "pypdf_first_page"
        for field_name, value in {
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi,
            "pmid": pmid,
            "publication_type": publication_type,
            "abstract": abstract,
        }.items()
        if value
    }
    return LocalPdfMetadata(
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        pmid=pmid,
        publication_type=publication_type,
        abstract=abstract,
        field_sources=sources,
    )
