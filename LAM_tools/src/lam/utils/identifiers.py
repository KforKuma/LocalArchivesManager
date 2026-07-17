from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from ..models import IdentifierCandidate


DOI_PATTERN = re.compile(r"(?<![\w.])(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)
DOI_MIN_SUFFIX_ALNUM = 3
DOI_MAX_LENGTH = 200
DOI_SUFFIX_PATTERN = re.compile(r"^[-._;()/:A-Z0-9]+$", re.I)
PMID_PATTERN = re.compile(
    r"\b(?:pubmed\s+)?pmid\s*[:#]?\s*(\d{5,9})\b",
    re.I,
)
ARXIV_PATTERN = re.compile(
    r"^(?:arxiv:)?(?P<base>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
    r"(?P<version>v\d+)?$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class DoiCandidateParse:
    raw_value: str
    value: str = ""
    status: str = "rejected"
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.status == "complete" and bool(self.value)


def parse_doi_candidate(
    value: object,
    *,
    min_suffix_alnum: int = DOI_MIN_SUFFIX_ALNUM,
    max_length: int = DOI_MAX_LENGTH,
) -> DoiCandidateParse:
    raw = str(value or "")
    text = unquote(raw).strip().replace("，", ",").replace("。", ".")
    text = re.sub(r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.I)
    start = re.search(r"10\s*[.,]\s*\d{4,9}\s*/", text, re.I)
    if not start:
        return DoiCandidateParse(raw, rejection_reasons=("doi_structure_invalid",))
    candidate = re.sub(r"\s+", "", text[start.start() :])
    candidate = re.sub(r"^10,", "10.", candidate, flags=re.I)
    candidate = candidate.rstrip("\"'<>[]{}")
    candidate = candidate.rstrip(",;:")
    candidate = candidate.rstrip(".")
    match = re.fullmatch(r"10\.(\d{4,9})/(.*)", candidate, re.I)
    if not match:
        return DoiCandidateParse(raw, rejection_reasons=("doi_structure_invalid",))
    suffix = match.group(2).rstrip(")]")
    normalized = f"10.{match.group(1)}/{suffix}".casefold()
    reasons: list[str] = []
    if len(normalized) > max_length:
        reasons.append("doi_length_rejected")
    if not suffix:
        reasons.append("doi_suffix_empty")
    if suffix and not DOI_SUFFIX_PATTERN.fullmatch(suffix):
        reasons.append("doi_suffix_invalid")
    if suffix.endswith(("/", ".", "-", "_", ";", ":")):
        reasons.append("doi_truncated")
    suffix_alnum = len(re.findall(r"[A-Z0-9]", suffix, re.I))
    if suffix_alnum < min_suffix_alnum:
        return DoiCandidateParse(
            raw,
            value=normalized,
            status="prefix_only",
            rejection_reasons=("doi_prefix_only",),
        )
    if reasons:
        return DoiCandidateParse(
            raw,
            value=normalized,
            status="rejected",
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )
    return DoiCandidateParse(raw, value=normalized, status="complete")


def normalize_doi(value: object) -> str:
    parsed = parse_doi_candidate(value)
    return parsed.value if parsed.complete else ""


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
        parsed = parse_doi_candidate(match.group(1))
        value = parsed.value if parsed.complete else ""
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


def merge_doi_fragments(
    lines: list[str] | tuple[str, ...],
    *,
    min_suffix_alnum: int = DOI_MIN_SUFFIX_ALNUM,
    max_length: int = DOI_MAX_LENGTH,
) -> list[DoiCandidateParse]:
    """Reassemble only adjacent DOI-looking fragments, never arbitrary prose."""
    normalized_lines = [re.sub(r"\s+", " ", str(item or "")).strip() for item in lines]
    results: list[DoiCandidateParse] = []
    seen: set[str] = set()
    for index, first in enumerate(normalized_lines):
        if not re.search(r"(?:doi\.org/)?10\s*[.,]\s*\d{4,9}\s*/", first, re.I):
            continue
        for width in (1, 2, 3):
            group = normalized_lines[index : index + width]
            if len(group) != width:
                continue
            if width > 1 and not all(
                DOI_SUFFIX_PATTERN.fullmatch(re.sub(r"\s+", "", item))
                for item in group[1:]
            ):
                break
            combined = "".join(group)
            parsed = parse_doi_candidate(
                combined,
                min_suffix_alnum=min_suffix_alnum,
                max_length=max_length,
            )
            continuation = bool(
                width < 3
                and index + width < len(normalized_lines)
                and group[-1].rstrip().endswith(("/", ".", "-", "_"))
            )
            if parsed.complete and continuation:
                continue
            key = f"{parsed.status}:{parsed.value}"
            if parsed.value and key not in seen:
                seen.add(key)
                results.append(parsed)
            if parsed.complete:
                break
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
