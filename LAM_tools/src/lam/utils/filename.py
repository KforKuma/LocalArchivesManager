from __future__ import annotations

import re
from pathlib import Path


WINDOWS_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(filename: str, max_length: int = 180) -> str:
    source = Path(filename)
    suffix = source.suffix if source.suffix else ".pdf"
    stem = source.stem if source.suffix else filename
    stem = WINDOWS_UNSAFE.sub("-", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    stem = re.sub(r"-{2,}", "-", stem)
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    allowed_stem = max(1, max_length - len(suffix))
    if len(stem) > allowed_stem:
        stem = stem[:allowed_stem].rstrip(" .-")
    return f"{stem or 'untitled'}{suffix.lower()}"


def standard_pdf_filename(
    *,
    title: object,
    year: object,
    journal_abbrev: object = "",
    journal: object = "",
    publication_type: object = "",
    max_length: int = 180,
) -> str | None:
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    clean_year = str(year or "").strip()
    clean_journal = re.sub(
        r"\s+", " ", str(journal_abbrev or journal or "")
    ).strip()
    if not clean_title or not re.fullmatch(r"(?:19|20)\d{2}", clean_year) or not clean_journal:
        return None
    publication = re.sub(r"\s+", " ", str(publication_type or "")).strip()
    ordinary = {"", "article", "journal article", "research article"}
    type_part = "" if publication.casefold() in ordinary else f", {publication}"
    return sanitize_filename(
        f"{clean_journal}, {clean_year}{type_part} - {clean_title}.pdf",
        max_length=max_length,
    )
