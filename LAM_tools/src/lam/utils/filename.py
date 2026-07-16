from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .publication_type import canonicalize_publication_type
from .supplementary import (
    canonical_supplementary_type,
    format_supplementary_sequence,
    is_supported_document_extension,
)


WINDOWS_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(
    filename: str,
    max_length: int = 180,
    *,
    preserve_extension_case: bool = False,
) -> str:
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
    safe_suffix = suffix if preserve_extension_case else suffix.lower()
    return f"{stem or 'untitled'}{safe_suffix}"


@dataclass(frozen=True, slots=True)
class StandardFilenameResult:
    filename: str | None
    publication_type: str | None
    warnings: tuple[str, ...] = ()
    title_truncated: bool = False


def _normalized_extension(value: object) -> str:
    extension = str(value or "").strip()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    if not re.fullmatch(r"\.[A-Za-z0-9]+", extension):
        return ""
    return extension if is_supported_document_extension(extension) else ""


def _sanitize_component(value: object) -> str:
    text = WINDOWS_UNSAFE.sub("-", re.sub(r"\s+", " ", str(value or "")).strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return re.sub(r"-{2,}", "-", text)


def standard_pdf_filename_result(
    *,
    title: object,
    year: object,
    journal_abbrev: object = "",
    journal: object = "",
    publication_type: object = "",
    supplementary_material_type: object = "",
    max_length: int = 180,
) -> StandardFilenameResult:
    clean_title = _sanitize_component(title)
    clean_year = str(year or "").strip()
    clean_journal = _sanitize_component(journal_abbrev or journal)
    if not clean_title or not re.fullmatch(r"(?:19|20)\d{2}", clean_year) or not clean_journal:
        return StandardFilenameResult(None, None)

    type_result = canonicalize_publication_type(publication_type)
    canonical_type = type_result.canonical_type
    type_part = f", {canonical_type}" if canonical_type else ""
    supplement = _sanitize_component(supplementary_material_type)
    supplement_part = f" - {supplement}" if supplement else ""
    prefix = f"{clean_journal}, {clean_year}{type_part} - "
    suffix = f"{supplement_part}.pdf"
    title_budget = max_length - len(prefix) - len(suffix)
    if title_budget < 1:
        warnings = tuple(dict.fromkeys((*type_result.warnings, "filename_prefix_too_long")))
        return StandardFilenameResult(None, canonical_type, warnings)
    title_truncated = len(clean_title) > title_budget
    shortened_title = (
        clean_title[:title_budget].rstrip(" .-")
        if title_truncated
        else clean_title.rstrip(" .-")
    ) or clean_title[:1]
    filename = f"{prefix}{shortened_title}{suffix}"
    safe = sanitize_filename(filename, max_length=max_length)
    return StandardFilenameResult(
        safe,
        canonical_type,
        type_result.warnings,
        title_truncated,
    )


def standard_pdf_filename(
    *,
    title: object,
    year: object,
    journal_abbrev: object = "",
    journal: object = "",
    publication_type: object = "",
    supplementary_material_type: object = "",
    max_length: int = 180,
) -> str | None:
    return standard_pdf_filename_result(
        title=title,
        year=year,
        journal_abbrev=journal_abbrev,
        journal=journal,
        publication_type=publication_type,
        supplementary_material_type=supplementary_material_type,
        max_length=max_length,
    ).filename


def standard_supplementary_filename_result(
    *,
    title: object,
    year: object,
    journal_abbrev: object = "",
    journal: object = "",
    publication_type: object = "",
    supplementary_type: object = "Supplementary",
    sequence: object = None,
    extension: object = ".pdf",
    max_length: int = 180,
) -> StandardFilenameResult:
    """Build a canonical supplementary name for any managed document type."""
    clean_title = _sanitize_component(title)
    clean_year = str(year or "").strip()
    clean_journal = _sanitize_component(journal_abbrev or journal)
    clean_extension = _normalized_extension(extension)
    canonical_supplement = canonical_supplementary_type(
        supplementary_type, default="Supplementary"
    )
    raw_sequence = "" if sequence in (None, "") else str(sequence).strip()
    formatted_sequence = format_supplementary_sequence(sequence)
    if (
        not clean_title
        or not re.fullmatch(r"(?:19|20)\d{2}", clean_year)
        or not clean_journal
        or not clean_extension
        or (raw_sequence and not formatted_sequence)
    ):
        return StandardFilenameResult(None, None)

    type_result = canonicalize_publication_type(publication_type)
    canonical_type = type_result.canonical_type
    type_part = f", {canonical_type}" if canonical_type else ""
    prefix = f"{clean_journal}, {clean_year}{type_part} - "
    suffix = f" - {canonical_supplement}{formatted_sequence}{clean_extension}"
    title_budget = max_length - len(prefix) - len(suffix)
    if title_budget < 1:
        warnings = tuple(dict.fromkeys((*type_result.warnings, "filename_prefix_too_long")))
        return StandardFilenameResult(None, canonical_type, warnings)
    title_truncated = len(clean_title) > title_budget
    shortened_title = (
        clean_title[:title_budget].rstrip(" .-")
        if title_truncated
        else clean_title.rstrip(" .-")
    ) or clean_title[:1]
    filename = f"{prefix}{shortened_title}{suffix}"
    safe = sanitize_filename(
        filename,
        max_length=max_length,
        preserve_extension_case=True,
    )
    return StandardFilenameResult(
        safe,
        canonical_type,
        type_result.warnings,
        title_truncated,
    )


def standard_supplementary_filename(**kwargs) -> str | None:
    return standard_supplementary_filename_result(**kwargs).filename


def standard_document_filename_result(**kwargs) -> StandardFilenameResult:
    """Compatibility name for callers treating supplementary items as Documents."""
    return standard_supplementary_filename_result(**kwargs)


def standard_document_filename(**kwargs) -> str | None:
    return standard_supplementary_filename(**kwargs)
