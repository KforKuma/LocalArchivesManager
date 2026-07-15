from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CanonicalTypeResult:
    raw_types: tuple[str, ...]
    canonical_type: str | None
    warnings: tuple[str, ...] = ()
    recognized_special_types: tuple[str, ...] = ()

    @property
    def conflict(self) -> bool:
        return "publication_type_conflict" in self.warnings


_SPECIAL_ALIASES = {
    "review": "Review",
    "systematic review": "Systematic Review",
    "meta analysis": "Meta-analysis",
    "meta-analysis": "Meta-analysis",
    "published erratum": "Erratum",
    "erratum": "Erratum",
    "correction": "Erratum",
    "retraction": "Retraction",
    "retracted publication": "Retraction",
    "retraction of publication": "Retraction",
    "retraction notice": "Retraction",
    "editorial": "Editorial",
    "commentary": "Commentary",
    "letter": "Letter",
    "guideline": "Guideline",
    "practice guideline": "Guideline",
    "protocol": "Protocol",
    "case report": "Case Report",
    "case reports": "Case Report",
}

# These are known provider/index terms that are intentionally not article
# genres for LAM filenames. The special-genre whitelist above remains the only
# route by which a value can enter catalogue publication_type or a filename.
_KNOWN_ORDINARY_OR_INDEX = {
    "article",
    "journal article",
    "journal-article",
    "research article",
    "comparative study",
    "multicenter study",
    "observational study",
    "clinical trial",
    "validation study",
    "evaluation study",
    "randomized controlled trial",
    "controlled clinical trial",
    "clinical study",
    "preprint",
    "research support",
    "research support, non-u.s. gov't",
    "research support, u.s. gov't, non-p.h.s.",
    "research support, u.s. gov't, p.h.s.",
    "research support, n.i.h., extramural",
    "research support, n.i.h., intramural",
}

_PRIORITY = {
    "Erratum": 0,
    "Retraction": 0,
    "Meta-analysis": 1,
    "Systematic Review": 2,
    "Review": 3,
    "Guideline": 4,
    "Protocol": 4,
    "Editorial": 5,
    "Commentary": 5,
    "Letter": 5,
    "Case Report": 6,
}


def _raw_values(raw_types: Any) -> list[str]:
    if raw_types in (None, ""):
        return []
    if isinstance(raw_types, str):
        candidates: Iterable[Any] = raw_types.split(";")
    elif isinstance(raw_types, Iterable):
        candidates = raw_types
    else:
        candidates = (raw_types,)
    values: list[str] = []
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        parts = str(candidate).split(";")
        for part in parts:
            value = re.sub(r"\s+", " ", part).strip()
            if value and value.casefold() not in {item.casefold() for item in values}:
                values.append(value)
    return values


def _key(value: str) -> str:
    normalized = value.casefold().replace("–", "-").replace("—", "-")
    normalized = re.sub(r"[_\s]+", " ", normalized)
    return re.sub(r"\s*-\s*", "-", normalized).strip()


def canonicalize_publication_type(raw_types: Any) -> CanonicalTypeResult:
    raw = _raw_values(raw_types)
    special: list[str] = []
    unrecognized: list[str] = []
    for value in raw:
        key = _key(value)
        canonical = _SPECIAL_ALIASES.get(key)
        if canonical:
            if canonical not in special:
                special.append(canonical)
            continue
        if key in _KNOWN_ORDINARY_OR_INDEX:
            continue
        if re.fullmatch(r"clinical trial(?:, phase [ivx]+)?", key):
            continue
        if value:
            unrecognized.append(value)

    warnings: list[str] = []
    canonical_type: str | None = None
    if special:
        best_priority = min(_PRIORITY[item] for item in special)
        best = [item for item in special if _PRIORITY[item] == best_priority]
        if len(best) == 1:
            canonical_type = best[0]
        else:
            warnings.append("publication_type_conflict")
    if unrecognized:
        warnings.append("publication_type_unrecognized")
    return CanonicalTypeResult(
        raw_types=tuple(raw),
        canonical_type=canonical_type,
        warnings=tuple(warnings),
        recognized_special_types=tuple(special),
    )
