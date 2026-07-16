from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .identifiers import normalize_doi, normalize_pmid
from .title_matching import filename_evidence, tolerant_title_score


_CJK = re.compile(r"[\u3400-\u9fff]")
_SECTION = re.compile(
    r"^(?:abstract|摘\s*要|keywords?|key\s*words|关\s*键\s*词|引\s*言|"
    r"introduction|中图分类号|收稿日期|基金项目)\s*[:：]?\s*",
    re.I,
)
_ABSTRACT = re.compile(r"^(abstract|摘\s*要)\s*[:：]?\s*(.*)$", re.I)
_ABSTRACT_STOP = re.compile(
    r"^(?:keywords?|key\s*words|关\s*键\s*词|引\s*言|introduction|"
    r"1\s*[.、]?\s*(?:introduction|引言)|中图分类号|收稿日期|基金项目)\b|"
    r"^(?:关键词|引言|中图分类号|收稿日期|基金项目)\s*[:：]?",
    re.I,
)
_TITLE_NOISE = re.compile(
    r"^(?:vol(?:ume)?\.?\s*\d+.*|第\s*\d+\s*卷.*|no\.?\s*\d+.*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}.*|"
    r"\d+\s*[-–—]\s*\d+|p(?:p)?\.?\s*\d+.*|issn\b.*|doi\b.*|"
    r"https?://.*|www\..*|abstract|摘\s*要|keywords?|关\s*键\s*词|"
    r"received\b.*|accepted\b.*|published\b.*|copyright\b.*)$",
    re.I,
)
_AFFILIATION = re.compile(
    r"(?:university|college|school|department|institute|laboratory|hospital|"
    r"academy|correspondence|作者单位|大学|学院|研究所|实验室|医院|中心|通信作者)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class LocalPdfMetadata:
    title: str = ""
    primary_title: str = ""
    translated_title: str = ""
    local_language_title: str = ""
    english_title: str = ""
    authors: tuple[str, ...] = ()
    year: str = ""
    journal: str = ""
    doi: str = ""
    pmid: str = ""
    publication_type: str = ""
    abstract: str = ""
    field_sources: dict[str, str] = field(default_factory=dict)
    field_confidence: dict[str, str] = field(default_factory=dict)
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
            "abstract": self.abstract,
        }


def _clean_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line and line.strip()
    ]


def _is_title_quality(value: str) -> bool:
    value = re.sub(r"\s+", " ", value).strip(" .,:：;；")
    if (
        not 6 <= len(value) <= 300
        or _TITLE_NOISE.match(value)
        or _SECTION.match(value)
        or re.search(
            r"(?:\bv\s*ol(?:ume)?\s*\.?\s*\d+|\bn\s*o\s*\.?\s*\d+|第\s*\d+\s*卷)",
            value,
            re.I,
        )
    ):
        return False
    if normalize_doi(value) or re.fullmatch(r"[\d\W_]+", value):
        return False
    if _AFFILIATION.search(value) or "@" in value:
        return False
    if ";" in value or "；" in value:
        return False
    if _CJK.search(value):
        return len(_CJK.findall(value)) >= 4
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", value)
    return len(words) >= 3 and sum(len(word) for word in words) >= 14


def _title_structure(
    lines: list[str], filename: str, supplied_candidates: list[str]
) -> tuple[str, str, str, str]:
    section_index = next(
        (index for index, line in enumerate(lines) if _SECTION.match(line)),
        min(len(lines), 30),
    )
    top = lines[: min(section_index, 30)]
    candidates: list[tuple[int, str]] = []
    for index, value in enumerate(top):
        if _is_title_quality(value):
            candidates.append((index, value))
    for value in supplied_candidates:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if _is_title_quality(value) and all(value != item for _, item in candidates):
            candidates.append((50, value))

    local = next((value for _, value in candidates if _CJK.search(value)), "")
    english = next((value for _, value in candidates if not _CJK.search(value)), "")
    evidence = filename_evidence(Path(filename).name)
    filename_title = (
        evidence.title_candidate.value if evidence.title_candidate else ""
    )
    if filename_title and _is_title_quality(filename_title):
        same_language = [
            value
            for _, value in candidates
            if bool(_CJK.search(value)) == bool(_CJK.search(filename_title))
        ]
        if same_language:
            matched = max(
                same_language,
                key=lambda value: tolerant_title_score(value, filename_title),
            )
            if tolerant_title_score(matched, filename_title) >= 0.86:
                if _CJK.search(matched):
                    local = matched
                else:
                    english = matched

    # In bilingual domestic papers the local-language heading is conventionally
    # the body title; the adjacent English heading is retained as a search alias.
    primary = local or english or (filename_title if _is_title_quality(filename_title) else "")
    translated = english if local and english else ""
    return primary, translated, local, english


def _authors(lines: list[str], titles: set[str]) -> tuple[str, ...]:
    title_positions = [
        index for index, line in enumerate(lines) if line in titles and line
    ]
    start = min(title_positions) + 1 if title_positions else 0
    stop = next(
        (index for index, line in enumerate(lines[start:], start) if _SECTION.match(line)),
        min(len(lines), start + 12),
    )
    english: list[str] = []
    chinese: list[str] = []
    for line in lines[start:stop]:
        if line in titles or _AFFILIATION.search(line) or "@" in line or _TITLE_NOISE.match(line):
            continue
        cleaned = re.sub(r"(?<=[A-Za-z\u3400-\u9fff])\s*[1-9*†‡]+(?:[,，][1-9]+)*", "", line)
        if _CJK.search(cleaned):
            parts = re.split(r"\s*[，、;；]\s*", cleaned)
            names = [part.strip() for part in parts if 2 <= len(part.strip()) <= 8]
            if len(names) >= 2 and all(_CJK.search(name) for name in names):
                chinese.extend(names)
        else:
            if not re.search(r"[,;]|(?:and|&)\b", cleaned, re.I):
                continue
            parts = re.split(r"\s*(?:;|\band\b|&)\s*|,\s*(?=[A-Z][a-z])", cleaned, flags=re.I)
            names = []
            for part in parts:
                name = re.sub(r"\s+", " ", part).strip(" ,")
                words = name.split()
                if 2 <= len(words) <= 6 and all(re.search(r"[A-Za-z]", word) for word in words):
                    names.append(name)
            if len(names) >= 2:
                english.extend(names)
    selected = english or chinese
    return tuple(dict.fromkeys(selected))


def _extract_abstracts(lines: list[str]) -> tuple[str, str]:
    english = ""
    chinese = ""
    for index, line in enumerate(lines):
        heading = _ABSTRACT.match(line)
        if not heading:
            continue
        body: list[str] = [heading.group(2).strip()] if heading.group(2).strip() else []
        stopped = False
        for following in lines[index + 1 :]:
            if _ABSTRACT_STOP.match(following):
                stopped = True
                break
            if _ABSTRACT.match(following):
                break
            body.append(following)
            if sum(len(item) for item in body) > 5000:
                break
        value = re.sub(r"\s+", " ", " ".join(body)).strip()
        minimum = 50 if _CJK.search(value) else 80
        if not stopped or not minimum <= len(value) <= 5000:
            continue
        if heading.group(1).casefold() == "abstract":
            english = value
        else:
            chinese = value
    return english, chinese


def _journal(lines: list[str]) -> str:
    for line in lines[:15]:
        if _TITLE_NOISE.match(line) and re.match(r"^(?:vol|no|第\s*\d+\s*卷)", line, re.I):
            continue
        match = re.match(
            r"^(?P<journal>.+?)\s+(?:Vol(?:ume)?\.?\s*\d+|第\s*\d+\s*卷|"
            r"(?:19|20)\d{2}\s*年)",
            line,
            re.I,
        )
        if match:
            value = match.group("journal").strip(" ,，")
            if 2 <= len(value) <= 100 and not _AFFILIATION.search(value):
                return value
        match = re.match(r"^(?P<journal>.{2,50}(?:学报|杂志|期刊|研究))\s+", line)
        if match:
            return match.group("journal").strip()
    return ""


def extract_local_pdf_metadata(
    *,
    first_page_text: str,
    filename: str,
    title_candidates: list[str],
    doi_candidates: list[str],
    pmid_candidates: list[str],
    ocr_text: str = "",
) -> LocalPdfMetadata:
    pypdf_lines = _clean_lines(first_page_text)
    ocr_lines = _clean_lines(ocr_text)
    lines = pypdf_lines if pypdf_lines else ocr_lines
    combined_lines = [*pypdf_lines]
    for line in ocr_lines:
        if line not in combined_lines:
            combined_lines.append(line)

    primary, translated, local_title, english_title = _title_structure(
        combined_lines, filename, title_candidates
    )
    titles = {primary, translated, local_title, english_title} - {""}
    authors = _authors(combined_lines, titles)
    english_abstract, chinese_abstract = _extract_abstracts(combined_lines)
    abstract = english_abstract or chinese_abstract
    journal = _journal(combined_lines)
    evidence = filename_evidence(Path(filename).name)
    years = re.findall(r"\b(?:19|20)\d{2}\b", "\n".join(combined_lines[:20]))
    year = evidence.year or (years[0] if years else "")
    doi = next((normalize_doi(item) for item in doi_candidates if normalize_doi(item)), "")
    pmid = next((normalize_pmid(item) for item in pmid_candidates if normalize_pmid(item)), "")
    publication_type = evidence.publication_type or ""

    field_values = {
            "title": primary,
            "primary_title": primary,
            "translated_title": translated,
            "local_language_title": local_title,
            "english_title": english_title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "doi": doi,
            "pmid": pmid,
            "publication_type": publication_type,
            "abstract": abstract,
    }
    pypdf_blob = re.sub(r"\s+", " ", first_page_text).casefold()

    def evidence_source(value: object) -> str:
        values = value if isinstance(value, (list, tuple)) else (value,)
        probes = [re.sub(r"\s+", " ", str(item)).casefold() for item in values if item]
        if probes and all(probe in pypdf_blob for probe in probes):
            return "pypdf_first_page"
        return "ocr_first_page" if ocr_lines else "pypdf_first_page"

    sources = {
        field_name: evidence_source(value)
        for field_name, value in field_values.items()
        if value
    }
    confidence = {
        field_name: ("high" if field_name in {"doi", "year", "abstract"} else "medium")
        for field_name in sources
    }
    if primary and primary in pypdf_lines:
        confidence["title"] = "high"
        confidence["primary_title"] = "high"
    warnings: list[str] = []
    if local_title and english_title:
        warnings.append("bilingual_title_detected")
    return LocalPdfMetadata(
        title=primary,
        primary_title=primary,
        translated_title=translated,
        local_language_title=local_title,
        english_title=english_title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        pmid=pmid,
        publication_type=publication_type,
        abstract=abstract,
        field_sources=sources,
        field_confidence=confidence,
        warnings=tuple(warnings),
    )
