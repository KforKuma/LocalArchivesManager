from __future__ import annotations

import hashlib
from pathlib import Path


QUICK_BLOCK_SIZE = 64 * 1024


def quick_hash(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(QUICK_BLOCK_SIZE))
        if size > QUICK_BLOCK_SIZE:
            handle.seek(max(0, size - QUICK_BLOCK_SIZE))
            digest.update(handle.read(QUICK_BLOCK_SIZE))
    return digest.hexdigest()


def full_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

