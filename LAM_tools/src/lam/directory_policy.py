from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .exceptions import FileOperationError
from .utils.filename import WINDOWS_UNSAFE


DEFAULT_RESERVED_ROOT_DIRECTORIES = frozenset(
    {
        "Inbox",
        "Registered",
        "Topics",
        "LAM_tools",
        "scripts",
        "build",
        "dist",
        "__pycache__",
        ".git",
        ".idea",
        ".agents",
        ".codex",
        ".library_state",
    }
)


class RootDirectoryKind(StrEnum):
    INBOX = "inbox"
    REGISTERED = "registered"
    TOPICS_ROOT = "topics_root"
    MANAGEMENT = "management"
    HIDDEN = "hidden"
    LEGACY_TOPIC_CANDIDATE = "legacy_topic_candidate"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DirectoryPolicy:
    library_root: Path
    extra_reserved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "library_root", self.library_root.resolve())

    @property
    def topics_root(self) -> Path:
        return self.library_root / "Topics"

    @property
    def reserved_names(self) -> frozenset[str]:
        return frozenset(
            name.casefold()
            for name in (*DEFAULT_RESERVED_ROOT_DIRECTORIES, *self.extra_reserved)
            if str(name).strip()
        )

    def classify_root_directory(
        self,
        path_or_name: str | Path,
        *,
        referenced_legacy_roots: set[str] | None = None,
        explicit_legacy_roots: set[str] | None = None,
    ) -> RootDirectoryKind:
        path = Path(path_or_name)
        name = path.name if path.name else str(path_or_name)
        key = name.casefold()
        if name.startswith("."):
            return RootDirectoryKind.HIDDEN
        if key == "inbox":
            return RootDirectoryKind.INBOX
        if key == "registered":
            return RootDirectoryKind.REGISTERED
        if key == "topics":
            return RootDirectoryKind.TOPICS_ROOT
        if key in self.reserved_names:
            return RootDirectoryKind.MANAGEMENT
        referenced = {item.casefold() for item in referenced_legacy_roots or set()}
        explicit = {item.casefold() for item in explicit_legacy_roots or set()}
        if key in referenced or key in explicit:
            return RootDirectoryKind.LEGACY_TOPIC_CANDIDATE
        return RootDirectoryKind.UNKNOWN

    def normalize_legacy_topic_folder(self, value: object) -> str:
        text = str(value or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if text.casefold() == "topics":
            return ""
        if text.casefold().startswith("topics/"):
            text = text.split("/", 1)[1]
        return self.validate_topic_folder(text)

    def validate_topic_folder(self, value: object) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if not text or text in {".", ".."}:
            raise FileOperationError("Topic folder is empty or invalid")
        if text.casefold() == "topics" or text.casefold().startswith("topics/"):
            raise FileOperationError("topic_folder must be relative to Topics/")
        if os.path.isabs(text) or re.match(r"^[A-Za-z]:", text):
            raise FileOperationError("Topic folder must not be an absolute path")
        path = PurePosixPath(text)
        parts = path.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise FileOperationError("Topic folder contains unsafe traversal components")
        for part in parts:
            if part.startswith("."):
                raise FileOperationError("Hidden directories cannot be topic folders")
            if part.endswith((" ", ".")) or WINDOWS_UNSAFE.search(part):
                raise FileOperationError(f"Unsafe topic path component: {part!r}")
            if _normalized_reserved(part, self.reserved_names):
                raise FileOperationError(f"Reserved topic path component: {part!r}")
        return "/".join(parts)

    def topic_path(self, value: object) -> Path:
        relative = self.validate_topic_folder(value)
        target = (self.topics_root / Path(*relative.split("/"))).resolve()
        try:
            target.relative_to(self.topics_root.resolve())
        except ValueError as exc:
            raise FileOperationError("Topic path escapes Topics/") from exc
        return target

    def relative_topic_for_path(self, path: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(self.topics_root.resolve())
        except ValueError:
            return None
        if not relative.parts:
            return None
        if any(part.startswith(".") for part in relative.parts):
            return None
        return relative.as_posix()


def _normalized_reserved(name: str, reserved: frozenset[str]) -> bool:
    stem = re.sub(r"\..*$", "", name).casefold()
    return stem in reserved or stem in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }


def parse_reserved_root_directories(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        item.strip()
        for item in re.split(r"[;,]", value)
        if item.strip()
    )


def classify_root_directory(
    library_root: Path,
    path_or_name: str | Path,
    *,
    referenced_legacy_roots: set[str] | None = None,
    explicit_legacy_roots: set[str] | None = None,
    extra_reserved: tuple[str, ...] = (),
) -> RootDirectoryKind:
    return DirectoryPolicy(library_root, extra_reserved).classify_root_directory(
        path_or_name,
        referenced_legacy_roots=referenced_legacy_roots,
        explicit_legacy_roots=explicit_legacy_roots,
    )
