from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..models import (
    AnalysisCandidate,
    CandidateConfidence,
    OcrTextBlock,
)
from .text import is_title_noise_line, normalize_title


TITLE_CONTAMINATION = re.compile(
    r"(?:anna['’]s\s+archive|pdf\.js|science\s*direct|"
    r"contents\s+lists?\s+available\s+at|journal\s+homepage|"
    r"\belsevier\b|article\s+info(?:rmation)?|download(?:ed)?\s+from|"
    r"document\s+viewer|page\s+\d+\s+(?:of|/)\s+\d+)",
    re.I,
)
TITLE_STOP = re.compile(
    r"^(?:article\s+info(?:rmation)?|abstract|摘要|keywords?|关键词|"
    r"correspondence|affiliations?)\b",
    re.I,
)
AUTHOR_OR_AFFILIATION = re.compile(
    r"(?:\b(?:university|department|institute|school|faculty|corresponding)\b|@)",
    re.I,
)
URL_LIKE = re.compile(r"(?:https?://|www\.|doi\.org/)", re.I)


def classify_title_candidate(
    value: object,
    *,
    source: str,
    page: int | None = 1,
    region: str = "",
    evidence: str = "",
    repeated_across_pages: bool = False,
    after_author_or_abstract: bool = False,
) -> AnalysisCandidate:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    reasons: list[str] = []
    if not text:
        reasons.append("title_empty")
    if TITLE_CONTAMINATION.search(text):
        reasons.append(
            "metadata_title_contaminated"
            if source == "metadata"
            else "title_navigation_or_publisher_noise"
        )
    if is_title_noise_line(text):
        reasons.append("title_layout_noise")
    if URL_LIKE.search(text):
        reasons.append("title_url_like")
    if repeated_across_pages:
        reasons.append("title_repeated_chrome")
    if after_author_or_abstract:
        reasons.append("title_after_author_or_abstract")
    normalized = normalize_title(text)
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    cjk = re.findall(r"[\u3400-\u9fff]", text)
    digits = re.findall(r"\d", text)
    if len(text) < 8 or (len(words) < 3 and len(cjk) < 4):
        reasons.append("title_too_short")
    if len(text) > 300:
        reasons.append("title_too_long")
    if text and len(digits) / len(text) > 0.55:
        reasons.append("title_mostly_numeric")
    if not normalized:
        reasons.append("title_empty_after_normalization")
    if reasons:
        confidence = CandidateConfidence.REJECTED
    elif source in {"filename", "user_confirmation"} and len(text) >= 20:
        confidence = CandidateConfidence.TRUSTED
    elif (
        source in {"metadata", "page_top"} or source.startswith("ocr")
    ) and len(text) >= 20:
        confidence = CandidateConfidence.USABLE
    else:
        confidence = CandidateConfidence.WEAK
    return AnalysisCandidate(
        value=text,
        field="title",
        source=source,
        confidence=confidence,
        page=page,
        region=region,
        evidence=evidence,
        rejection_reasons=list(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    top: float
    bottom: float
    left: float
    right: float
    height: float


def merge_title_blocks(
    blocks: Iterable[OcrTextBlock],
    *,
    page: int = 1,
    region: str = "title_author",
) -> tuple[list[AnalysisCandidate], dict[str, int]]:
    lines = _ordered_block_lines(blocks)
    usable_lines: list[_Line] = []
    for line in lines:
        if TITLE_STOP.search(line.text):
            break
        if AUTHOR_OR_AFFILIATION.search(line.text) and usable_lines:
            break
        if usable_lines and _looks_like_author_line(line.text):
            break
        usable_lines.append(line)

    candidates: list[AnalysisCandidate] = []
    merged_count = 0
    hyphenation_count = 0
    for start in range(len(usable_lines)):
        for width in range(1, min(4, len(usable_lines) - start) + 1):
            group = usable_lines[start : start + width]
            if not _spatially_adjacent(group):
                break
            combined, repaired = _join_title_lines([item.text for item in group])
            candidate = classify_title_candidate(
                combined,
                source="ocr_title_merged" if width > 1 else "ocr_title_line",
                page=page,
                region=region,
                evidence=f"{width}_line_title_combination",
            )
            candidates.append(candidate)
            if width > 1 and candidate.query_eligible:
                merged_count += 1
                hyphenation_count += repaired

    unique: dict[str, AnalysisCandidate] = {}
    rank = {
        CandidateConfidence.TRUSTED: 0,
        CandidateConfidence.USABLE: 1,
        CandidateConfidence.WEAK: 2,
        CandidateConfidence.REJECTED: 3,
    }
    for candidate in candidates:
        key = normalize_title(candidate.value)
        current = unique.get(key)
        if not key or (
            current is not None
            and rank[current.confidence] <= rank[candidate.confidence]
        ):
            continue
        unique[key] = candidate
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            rank[item.confidence],
            -len(item.value),
        ),
    )
    return ordered, {
        "title_lines_merged": merged_count,
        "hyphenation_repaired": hyphenation_count,
    }


def _ordered_block_lines(blocks: Iterable[OcrTextBlock]) -> list[_Line]:
    positioned: list[_Line] = []
    for block in blocks:
        if not block.bounding_box:
            continue
        xs = [point[0] for point in block.bounding_box]
        ys = [point[1] for point in block.bounding_box]
        positioned.append(
            _Line(
                text=re.sub(r"\s+", " ", block.text).strip(),
                top=min(ys),
                bottom=max(ys),
                left=min(xs),
                right=max(xs),
                height=max(1.0, max(ys) - min(ys)),
            )
        )
    positioned.sort(key=lambda item: ((item.top + item.bottom) / 2, item.left))
    return positioned


def _spatially_adjacent(lines: list[_Line]) -> bool:
    if len(lines) < 2:
        return True
    for previous, current in zip(lines, lines[1:]):
        gap = current.top - previous.bottom
        typical = max(previous.height, current.height)
        if gap < -typical * 0.6 or gap > typical * 1.8:
            return False
        width_overlap = min(previous.right, current.right) - max(
            previous.left, current.left
        )
        if width_overlap < -typical * 3 and abs(previous.left - current.left) > typical * 4:
            return False
    return True


def _join_title_lines(lines: list[str]) -> tuple[str, int]:
    combined = ""
    repaired = 0
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if combined.endswith("-") and re.match(r"^[a-z]", value):
            combined = combined[:-1] + value
            repaired += 1
        else:
            combined = f"{combined} {value}".strip()
    return re.sub(r"\s+", " ", combined).strip(), repaired


def _looks_like_author_line(value: str) -> bool:
    text = re.sub(r"\s+", " ", value).strip()
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    if not 2 <= len(words) <= 10 or len(text) > 100:
        return False
    if re.search(r"[:;!?]", text):
        return False
    capitalized = sum(word[:1].isupper() for word in words)
    return bool(
        capitalized >= max(2, len(words) - 1)
        and ("," in text or len(words) <= 3)
    )
