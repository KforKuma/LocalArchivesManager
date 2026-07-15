from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from ..models import TitleCandidate
from .identifiers import extract_doi_candidates, extract_pmid_candidates


_DASHES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'",
})
_ACADEMIC_ALIASES = str.maketrans({
    "α": " alpha ", "β": " beta ", "γ": " gamma ", "δ": " delta ",
    "κ": " kappa ", "⁺": "+", "⁻": "-",
})
_STANDARD_NAME = re.compile(
    r"^(?P<journal>.+?),\s*(?P<year>(?:19|20)\d{2})"
    r"(?:,\s*(?P<type>[^-]+?))?\s+-\s+(?P<title>.+)$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class TitleViews:
    original: str
    normalized: str
    transliterated: str
    tokenized: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilenameEvidence:
    title_candidate: TitleCandidate | None
    doi_candidates: tuple[object, ...]
    pmid_candidates: tuple[object, ...]
    year: str = ""
    journal: str = ""
    publication_type: str = ""


def title_views(value: object) -> TitleViews:
    original = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    normalized = unicodedata.normalize("NFKC", original).translate(_DASHES).casefold()
    normalized = normalized.replace("_", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    transliterated = normalized.translate(_ACADEMIC_ALIASES)
    transliterated = re.sub(r"\s+", " ", transliterated).strip()
    tokenized = tuple(
        token
        for token in re.findall(r"[\w]+(?:[+-](?=\b|\s|$))?", transliterated, re.UNICODE)
        if token
    )
    return TitleViews(original, normalized, transliterated, tokenized)


def tolerant_title_score(left: object, right: object) -> float:
    left_views = title_views(left)
    right_views = title_views(right)
    if not left_views.normalized or not right_views.normalized:
        return 0.0
    scores = [
        SequenceMatcher(None, left_views.normalized, right_views.normalized).ratio(),
        SequenceMatcher(None, left_views.transliterated, right_views.transliterated).ratio(),
    ]
    left_tokens = " ".join(left_views.tokenized)
    right_tokens = " ".join(right_views.tokenized)
    if left_tokens and right_tokens:
        scores.append(SequenceMatcher(None, left_tokens, right_tokens).ratio())
    # Charge-bearing biomedical labels remain semantically distinct; ordinary
    # punctuation hyphens are not treated as charge markers.
    charge_pattern = re.compile(r"\b[\w]+[+-](?=\s|$)", re.UNICODE)
    left_charges = set(charge_pattern.findall(left_views.normalized))
    right_charges = set(charge_pattern.findall(right_views.normalized))
    if left_charges != right_charges and (left_charges or right_charges):
        scores = [min(score, 0.89) for score in scores]
    return max(scores)


def titles_tolerantly_equivalent(left: object, right: object) -> bool:
    return tolerant_title_score(left, right) >= 0.96


def filename_evidence(filename: str) -> FilenameEvidence:
    stem = Path(filename).stem
    cleaned = unicodedata.normalize("NFKC", html.unescape(stem)).translate(_DASHES)
    match = _STANDARD_NAME.match(cleaned)
    if match:
        title = match.group("title")
        title = re.sub(
            r"\s+-\s+(?:Supplementary|Supporting)\b.*$", "", title, flags=re.I
        ).strip()
        year = match.group("year") or ""
        journal = (match.group("journal") or "").strip()
        publication_type = (match.group("type") or "").strip()
        confidence = "high" if len(title) >= 12 else "medium"
        evidence = "standard_filename"
    else:
        title = re.sub(r"[_\s]+", " ", cleaned).strip(" .-_–—")
        year_match = re.search(r"\b(?:19|20)\d{2}\b", cleaned)
        year = year_match.group(0) if year_match else ""
        journal = ""
        publication_type = ""
        confidence = "medium" if len(title) >= 12 and len(title.split()) >= 3 else "low"
        evidence = "cleaned_filename"
    candidate = (
        TitleCandidate(title, confidence, "filename", None, evidence) if title else None
    )
    return FilenameEvidence(
        title_candidate=candidate,
        doi_candidates=tuple(extract_doi_candidates(cleaned, source_type="filename")),
        pmid_candidates=tuple(extract_pmid_candidates(cleaned, source_type="filename")),
        year=year,
        journal=journal,
        publication_type=publication_type,
    )
