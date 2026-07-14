from __future__ import annotations

import re

from ..models import IdentifierCandidate


DOI_PATTERN = re.compile(r"(?<![\w.])(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)
PMID_PATTERN = re.compile(
    r"\b(?:pubmed\s+)?pmid\s*[:#]?\s*(\d{5,9})\b",
    re.I,
)
ARXIV_PATTERN = re.compile(
    r"^(?:arxiv:)?(?P<base>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
    r"(?P<version>v\d+)?$",
    re.I,
)


def normalize_doi(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.I)
    text = text.strip().rstrip(".,;:)]}>\"'")
    match = DOI_PATTERN.search(text)
    return match.group(1).casefold().rstrip(".,;:)]}>") if match else ""


def normalize_pmid(value: object) -> str:
    text = str(value or "").strip()
    labelled = PMID_PATTERN.search(text)
    if labelled:
        return labelled.group(1)
    return text if re.fullmatch(r"\d{5,9}", text) else ""


def normalize_arxiv_id(value: object, *, keep_version: bool = False) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    match = ARXIV_PATTERN.fullmatch(text)
    if not match:
        return ""
    base = match.group("base")
    version = match.group("version") or ""
    return f"{base}{version if keep_version else ''}"


def extract_doi_candidates(
    text: str,
    *,
    page: int | None = None,
    source_type: str = "text",
) -> list[IdentifierCandidate]:
    results: list[IdentifierCandidate] = []
    seen: set[str] = set()
    for match in DOI_PATTERN.finditer(text or ""):
        value = normalize_doi(match.group(1))
        if not value or value in seen:
            continue
        seen.add(value)
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        context = re.sub(r"\s+", " ", text[start:end]).strip()
        results.append(
            IdentifierCandidate(
                value=value,
                page=page,
                line_or_context=context,
                confidence="high" if source_type in {"metadata", "first_page"} else "medium",
                source_type=source_type,
            )
        )
    return results


def extract_pmid_candidates(
    text: str,
    *,
    page: int | None = None,
    source_type: str = "text",
) -> list[IdentifierCandidate]:
    results: list[IdentifierCandidate] = []
    seen: set[str] = set()
    for match in PMID_PATTERN.finditer(text or ""):
        value = match.group(1)
        if value in seen:
            continue
        seen.add(value)
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        context = re.sub(r"\s+", " ", text[start:end]).strip()
        results.append(
            IdentifierCandidate(
                value=value,
                page=page,
                line_or_context=context,
                confidence="high" if source_type in {"metadata", "first_page"} else "medium",
                source_type=source_type,
            )
        )
    return results
