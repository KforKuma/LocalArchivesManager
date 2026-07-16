from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".xlsx", ".xls", ".csv"})

_TYPE_ALIASES = {
    "supp": "Supplementary",
    "sup": "Supplementary",
    "supplement": "Supplementary",
    "supplementary": "Supplementary",
    "table": "Table",
    "fig": "Figure",
    "figure": "Figure",
    "methods": "Methods",
    "data": "Data",
    "appendix": "Appendix",
}

_UUID_TYPE_PATTERN = "|".join(
    ("supp", "table", "figure", "methods", "data")
)
_SAME_STEM_TYPE_PATTERN = "|".join(
    ("supplement", "appendix", "figure", "methods", "table", "data", "supp", "sup", "fig")
)
_UUID_PATTERN = (
    r"(?P<paper_uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})"
)
_UUID_SUPPLEMENTARY = re.compile(
    rf"^{_UUID_PATTERN}__(?P<type>{_UUID_TYPE_PATTERN})(?P<sequence>\d+)?$",
    re.IGNORECASE,
)
_SAME_STEM_SUPPLEMENTARY = re.compile(
    rf"^(?P<parent_stem>.+)_(?P<type>{_SAME_STEM_TYPE_PATTERN})(?P<sequence>\d+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SupplementaryFilename:
    filename: str
    binding: str
    supplementary_type: str
    sequence: int | None
    extension: str
    paper_uuid: str | None = None
    parent_stem: str | None = None

    @property
    def formatted_sequence(self) -> str:
        return format_supplementary_sequence(self.sequence)


def canonical_supplementary_type(value: object, *, default: str = "") -> str:
    """Return the display form for one supported supplementary type alias."""
    normalized = re.sub(r"\s+", "", str(value or "")).casefold()
    return _TYPE_ALIASES.get(normalized, default)


def format_supplementary_sequence(value: object) -> str:
    """Format a positive sequence with at least two digits; blank stays blank."""
    sequence = _parse_sequence(value)
    return f"{sequence:02d}" if sequence is not None else ""


def is_supported_document_extension(value: object) -> bool:
    extension = str(value or "").strip()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    return extension.casefold() in SUPPORTED_DOCUMENT_EXTENSIONS


def parse_uuid_supplementary_filename(
    filename: str,
) -> SupplementaryFilename | None:
    """Parse a UUID-bound supplementary filename using an exact full match."""
    name, stem, extension = _filename_parts(filename)
    if not name or not is_supported_document_extension(extension):
        return None
    match = _UUID_SUPPLEMENTARY.fullmatch(stem)
    if match is None:
        return None
    paper_uuid = _canonical_uuid(match.group("paper_uuid"))
    sequence = _parse_sequence(match.group("sequence"))
    if paper_uuid is None or (match.group("sequence") and sequence is None):
        return None
    return SupplementaryFilename(
        filename=name,
        binding="paper_uuid",
        paper_uuid=paper_uuid,
        parent_stem=None,
        supplementary_type=canonical_supplementary_type(match.group("type")),
        sequence=sequence,
        extension=extension,
    )


def parse_same_stem_supplementary_filename(
    filename: str,
) -> SupplementaryFilename | None:
    """Parse a same-batch supplementary suffix without guessing its parent."""
    name, stem, extension = _filename_parts(filename)
    if not name or not is_supported_document_extension(extension):
        return None
    match = _SAME_STEM_SUPPLEMENTARY.fullmatch(stem)
    if match is None:
        return None
    parent_stem = match.group("parent_stem")
    sequence = _parse_sequence(match.group("sequence"))
    if (
        not parent_stem
        or parent_stem.endswith(("_", "."))
        or (match.group("sequence") and sequence is None)
    ):
        return None
    return SupplementaryFilename(
        filename=name,
        binding="same_stem",
        paper_uuid=None,
        parent_stem=parent_stem,
        supplementary_type=canonical_supplementary_type(match.group("type")),
        sequence=sequence,
        extension=extension,
    )


def parse_supplementary_filename(filename: str) -> SupplementaryFilename | None:
    """Parse UUID-bound names before the broader same-stem convention."""
    return (
        parse_uuid_supplementary_filename(filename)
        or parse_same_stem_supplementary_filename(filename)
    )


def _filename_parts(filename: str) -> tuple[str, str, str]:
    text = str(filename or "")
    path = Path(text)
    if not text or path.name != text or text in {".", ".."}:
        return "", "", ""
    extension = path.suffix
    if not extension:
        return "", "", ""
    return path.name, path.stem, extension


def _canonical_uuid(value: str) -> str | None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if canonical == value.casefold() else None


def _parse_sequence(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text.isdecimal():
        return None
    sequence = int(text)
    return sequence if sequence > 0 else None
