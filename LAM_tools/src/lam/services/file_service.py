from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path

from ..exceptions import FileOperationError
from ..models import FileOperation, OperationType
from ..schema import RESERVED_DIRECTORIES
from ..utils.hashing import full_hash
from ..utils.filename import WINDOWS_UNSAFE, sanitize_filename


class FileService:
    def __init__(self, library_root: Path, max_filename_length: int = 180):
        self.library_root = library_root.resolve()
        self.max_filename_length = max_filename_length

    def require_within_root(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.library_root)
        except ValueError as exc:
            raise FileOperationError(f"Path escapes library root: {path}") from exc
        return resolved

    def validate_topic_folder(self, folder_name: str) -> Path:
        name = str(folder_name or "").strip()
        if not name or name in {".", ".."}:
            raise FileOperationError("Topic folder is empty or invalid")
        if "/" in name or "\\" in name or WINDOWS_UNSAFE.search(name):
            raise FileOperationError(f"Topic folder is not a direct safe child: {name}")
        if name.endswith((" ", ".")) or normalized_reserved(name):
            raise FileOperationError(f"Topic folder is reserved or unsafe: {name}")
        target = self.require_within_root(self.library_root / name)
        if target.parent != self.library_root:
            raise FileOperationError(f"Topic folder is not a direct child: {name}")
        if not target.exists():
            similar = self._suspiciously_similar(name)
            if similar:
                raise FileOperationError(
                    f"New topic folder {name!r} is suspiciously similar to existing {similar!r}"
                )
        return target

    def _suspiciously_similar(self, proposed: str) -> str | None:
        proposed_key = proposed.casefold()
        for entry in self.library_root.iterdir():
            if not entry.is_dir() or entry.name.casefold() in RESERVED_DIRECTORIES:
                continue
            existing_key = entry.name.casefold()
            if existing_key == proposed_key:
                return None
            if SequenceMatcher(None, proposed_key, existing_key).ratio() >= 0.88:
                return entry.name
        return None

    def plan_move(
        self,
        source: Path,
        target_folder: Path,
        catalogue_row: int,
        reason: str,
    ) -> FileOperation:
        source = self.require_within_root(source)
        target_folder = self.require_within_root(target_folder)
        if not source.is_file():
            raise FileOperationError(f"Source PDF does not exist: {source}")
        safe_name = sanitize_filename(source.name, self.max_filename_length)
        if safe_name != source.name:
            raise FileOperationError(
                f"Workflow 4 will not silently rename a non-standard filename: {source.name!r}"
            )
        target = self.require_within_root(target_folder / source.name)
        return FileOperation(OperationType.MOVE, source, target, catalogue_row, reason)

    def validate_plan(self, operations: list[FileOperation]) -> list[dict[str, str]]:
        problems: list[dict[str, str]] = []
        target_groups: dict[str, list[FileOperation]] = {}
        for operation in operations:
            target_groups.setdefault(str(operation.target).casefold(), []).append(operation)
        duplicate_rows = {
            operation.catalogue_row
            for group in target_groups.values()
            if len(group) > 1
            for operation in group
        }
        for operation in operations:
            source = operation.source
            assert source is not None
            if operation.catalogue_row in duplicate_rows:
                problems.append(
                    {
                        "row": str(operation.catalogue_row),
                        "issue": "multiple_rows_target_same_path",
                        "target": str(operation.target),
                    }
                )
                continue
            if operation.target.exists():
                identical = full_hash(source) == full_hash(operation.target)
                problems.append(
                    {
                        "row": str(operation.catalogue_row),
                        "issue": "identical_target_exists" if identical else "different_target_exists",
                        "target": str(operation.target),
                    }
                )
        return problems

    def apply_move(self, operation: FileOperation) -> None:
        source = operation.source
        assert source is not None
        self.require_within_root(source)
        self.require_within_root(operation.target)
        if operation.target.exists():
            raise FileOperationError(f"Refusing to overwrite existing file: {operation.target}")
        operation.target.parent.mkdir(parents=False, exist_ok=True)
        try:
            os.replace(source, operation.target)
        except Exception as exc:
            raise FileOperationError(
                f"Cannot move {source} to {operation.target}"
            ) from exc
        if not operation.target.is_file() or source.exists():
            raise FileOperationError(f"Move verification failed: {source} -> {operation.target}")

    def rollback_move(self, operation: FileOperation) -> None:
        source = operation.source
        assert source is not None
        self.require_within_root(source)
        self.require_within_root(operation.target)
        if source.exists() or not operation.target.is_file():
            raise FileOperationError(
                f"Cannot safely roll back move: {operation.target} -> {source}"
            )
        try:
            os.replace(operation.target, source)
        except Exception as exc:
            raise FileOperationError(
                f"Rollback failed: {operation.target} -> {source}"
            ) from exc


def normalized_reserved(name: str) -> bool:
    stem = re.sub(r"\..*$", "", name).casefold()
    return stem in RESERVED_DIRECTORIES or stem in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
