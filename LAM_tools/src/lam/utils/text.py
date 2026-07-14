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
    buffer: list[str] = []
    for line in lines[:25]:
        low = line.casefold()
        if (
            "doi" in low
            or "copyright" in low
            or re.fullmatch(r"(?:research )?article|review|editorial", low)
            or re.search(r"\b(?:vol(?:ume)?|issue)\b", low)
        ):
            if buffer:
                break
            continue
        if re.search(r"\b(?:university|department|correspondence|@)\b", low):
            break
        if len(line) <= 3:
            continue
        buffer.append(line)
        joined = " ".join(buffer)
        if min_length <= len(joined) <= max_length:
            candidates.append(
                TitleCandidate(
                    value=joined,
                    confidence="high" if page == 1 and len(joined) >= 20 else "medium",
                    source_type="page_top",
                    page=page,
                )
            )
        if len(joined) >= max_length:
            break
    unique: dict[str, TitleCandidate] = {}
    for candidate in candidates:
        unique.setdefault(normalize_title(candidate.value), candidate)
    return list(unique.values())[-5:]


def is_probable_supplement(*values: object) -> bool:
    return any(SUPPLEMENT_SIGNALS.search(str(value or "")) for value in values)
