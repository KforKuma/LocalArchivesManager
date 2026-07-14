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
