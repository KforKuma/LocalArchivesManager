from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


def normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalized_relative_path(value: object) -> str:
    text = str(value or "").replace("\\", "/").strip().strip("/")
    if not text:
        return ""
    return PurePosixPath(text).as_posix().casefold()

