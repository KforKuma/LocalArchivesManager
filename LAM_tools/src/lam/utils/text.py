from __future__ import annotations

import html
import re
import unicodedata

from ..models import TitleCandidate


INVALID_METADATA_TITLES = {
    "adobe pdf",
    "article",
    "full text pdf",
    "main document",
    "manuscript",
    "microsoft word",
    "untitled",
}

SUPPLEMENT_SIGNALS = re.compile(
    r"\b(supplement(?:ary)?|supporting information|supporting material|"
    r"additional file|data supplement|table s\d+|figure s\d+)\b",
    re.I,
)

TITLE_LAYOUT_NOISE = re.compile(
    r"^(?:vol(?:ume)?\.?\s*\d+.*|no\.?\s*\d+.*|第\s*\d+\s*卷.*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}.*|"
    r"\d+\s*[-–—]\s*\d+|p(?:p)?\.?\s*\d+.*|issn\b.*|doi\b.*|"
    r"https?://.*|www\..*|abstract|摘\s*要|keywords?|关\s*键\s*词)$",
    re.I,
)


def is_title_noise_line(value: object) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    header_geometry = re.search(
        r"(?:\bv\s*ol(?:ume)?\s*\.?\s*\d+|\bn\s*o\s*\.?\s*\d+|第\s*\d+\s*卷)",
        text,
        re.I,
    )
    return bool(not text or TITLE_LAYOUT_NOISE.match(text) or header_geometry)


def normalize_title(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(str.maketrans({"–": "-", "—": "-", "−": "-", "“": '"', "”": '"'}))
    text = re.sub(r"[^\w\s+\-/:]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip(" -")


def clean_metadata_title(value: object, *, min_length: int = 8, max_length: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    normalized = normalize_title(text)
    if (
        not text
        or normalized in INVALID_METADATA_TITLES
        or len(text) < min_length
        or len(text) > max_length
        or text.isdigit()
        or re.match(r"^(?:https?://|[A-Za-z]:[\\/])", text, re.I)
    ):
        return ""
    return text


def title_candidates_from_page(
    text: str,
    *,
    page: int,
    min_length: int = 8,
    max_length: int = 300,
) -> list[TitleCandidate]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    candidates: list[TitleCandidate] = []
    for line in lines[:25]:
        low = line.casefold()
        if (
            is_title_noise_line(line)
            or "copyright" in low
            or re.fullmatch(r"(?:research )?article|review|editorial", low)
        ):
            continue
        if re.fullmatch(r"(?:abstract|摘\s*要|keywords?|关\s*键\s*词)\s*[:：]?", line, re.I):
            break
        if re.search(r"\b(?:university|department|correspondence|@)\b", low):
            break
        if len(line) <= 3:
            continue
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", line))
        word_count = len(re.findall(r"[A-Za-z][A-Za-z'-]*", line))
        if cjk_count < 4 and word_count < 4:
            continue
        if min_length <= len(line) <= max_length:
            candidates.append(
                TitleCandidate(
                    value=line,
                    confidence="high" if page == 1 and (len(line) >= 20 or cjk_count >= 8) else "medium",
                    source_type="page_top",
                    page=page,
                    evidence=f"page_{page}_top_lines",
                )
            )
    unique: dict[str, TitleCandidate] = {}
    for candidate in candidates:
        unique.setdefault(normalize_title(candidate.value), candidate)
    return list(unique.values())[:5]


def is_probable_supplement(*values: object) -> bool:
    return any(SUPPLEMENT_SIGNALS.search(str(value or "")) for value in values)
