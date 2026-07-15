from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import normalized_text


@dataclass(frozen=True, slots=True)
class JournalName:
    original: str
    normalized: str
    base_name: str


def normalize_journal_name(value: object) -> JournalName:
    original = str(value or "").strip()
    normalized = normalized_text(original)
    base = re.sub(r"\s*\([^)]*\)\s*$", "", original).strip()
    base = re.sub(r"\b(?:18|19|20)\d{2}\b", " ", base)
    base = re.sub(r"[^\w]+", " ", base, flags=re.UNICODE)
    return JournalName(original, normalized, normalized_text(base))


def journals_equivalent(left: object, right: object) -> bool:
    first = normalize_journal_name(left)
    second = normalize_journal_name(right)
    if not first.original or not second.original:
        return not first.original and not second.original
    return first.normalized == second.normalized or first.base_name == second.base_name


def journal_is_variant(left: object, right: object) -> bool:
    first = normalize_journal_name(left)
    second = normalize_journal_name(right)
    return (
        bool(first.original and second.original)
        and first.normalized != second.normalized
        and first.base_name == second.base_name
    )
