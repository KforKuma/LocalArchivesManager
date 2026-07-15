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
        self.inbox_dir = (self.library_root / "Inbox").resolve()
        self.registered_dir = (self.library_root / "Registered").resolve()
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
        if name.startswith("."):
            raise FileOperationError(f"Hidden directories cannot be topic folders: {name}")
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
        self.workflow4_source_kind(source)
        target = self.require_within_root(target_folder / source.name)
        stat = source.stat()
        return FileOperation(
            OperationType.MOVE,
            source,
            target,
            catalogue_row,
            reason,
            expected_size=stat.st_size,
            expected_mtime_ns=stat.st_mtime_ns,
        )

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
            if not source.is_file():
                problems.append(
                    {
                        "row": str(operation.catalogue_row),
                        "issue": "source_missing_or_unreadable",
                        "target": str(operation.target),
                    }
                )
                continue
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
                try:
                    identical = full_hash(source) == full_hash(operation.target)
                except OSError:
                    problems.append(
                        {
                            "row": str(operation.catalogue_row),
                            "issue": "collision_validation_failed",
                            "target": str(operation.target),
                        }
                    )
                    continue
                problems.append(
                    {
                        "row": str(operation.catalogue_row),
                        "issue": "identical_target_exists" if identical else "different_target_exists",
                        "target": str(operation.target),
                    }
                )
        return problems

    def plan_registration_move(
        self,
        source: Path,
        target_filename: str,
        catalogue_row: int,
        reason: str,
    ) -> FileOperation:
        source = self.require_within_root(source)
        if not source.is_file() or source.parent != self.inbox_dir:
            raise FileOperationError(
                f"Workflow 3 only moves direct Inbox files: {source}"
            )
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(f"Workflow 3 refuses symlinks or reparse points: {source}")
        if source.suffix.casefold() != ".pdf":
            raise FileOperationError(f"Workflow 3 only moves PDF files: {source}")
        safe_name = sanitize_filename(target_filename, self.max_filename_length)
        if safe_name != target_filename or Path(target_filename).name != target_filename:
            raise FileOperationError(
                f"Registration target filename is not already safe: {target_filename!r}"
            )
        target = self.require_within_root(self.registered_dir / target_filename)
        stat = source.stat()
        return FileOperation(
            OperationType.MOVE,
            source,
            target,
            catalogue_row,
            reason,
            expected_size=stat.st_size,
            expected_mtime_ns=stat.st_mtime_ns,
        )

    def apply_registration_move(self, operation: FileOperation) -> None:
        source = operation.source
        assert source is not None
        self.require_within_root(source)
        self.require_within_root(operation.target)
        if source.parent != self.inbox_dir or source.suffix.casefold() != ".pdf":
            raise FileOperationError(
                f"Registration source is no longer an eligible Inbox PDF: {source}"
            )
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(
                f"Registration source became a symlink or reparse point: {source}"
            )
        if operation.target.parent != self.registered_dir:
            raise FileOperationError(
                f"Registration target is not directly in Registered: {operation.target}"
            )
        self._apply_no_replace(operation, "registration")

    def plan_registered_rename(
        self,
        source: Path,
        target_filename: str,
        catalogue_row: int,
        reason: str,
    ) -> FileOperation:
        source = self.require_within_root(source)
        if not source.is_file() or source.parent != self.registered_dir:
            raise FileOperationError(
                f"Publication type repair only renames direct Registered PDFs: {source}"
            )
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(f"Repair refuses symlinks or reparse points: {source}")
        if source.suffix.casefold() != ".pdf":
            raise FileOperationError(f"Repair only renames PDF files: {source}")
        safe_name = sanitize_filename(target_filename, self.max_filename_length)
        if safe_name != target_filename or Path(target_filename).name != target_filename:
            raise FileOperationError(
                f"Repair target filename is not already safe: {target_filename!r}"
            )
        target = self.require_within_root(self.registered_dir / target_filename)
        stat = source.stat()
        return FileOperation(
            OperationType.RENAME,
            source,
            target,
            catalogue_row,
            reason,
            expected_size=stat.st_size,
            expected_mtime_ns=stat.st_mtime_ns,
        )

    def apply_registered_rename(self, operation: FileOperation) -> None:
        source = operation.source
        assert source is not None
        self.require_within_root(source)
        self.require_within_root(operation.target)
        if source.parent != self.registered_dir or operation.target.parent != self.registered_dir:
            raise FileOperationError("Repair rename must stay directly within Registered")
        if source.suffix.casefold() != ".pdf" or operation.target.suffix.casefold() != ".pdf":
            raise FileOperationError("Repair rename requires PDF source and target")
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(f"Repair source became a symlink or reparse point: {source}")
        self._apply_no_replace(operation, "publication type repair")

    def apply_move(self, operation: FileOperation) -> None:
        source = operation.source
        assert source is not None
        self.require_within_root(source)
        self.require_within_root(operation.target)
        self.workflow4_source_kind(source)
        if operation.target.parent.parent != self.library_root:
            raise FileOperationError(
                f"Workflow 4 target is not a direct topic folder: {operation.target}"
            )
        self.validate_topic_folder(operation.target.parent.name)
        self._apply_no_replace(operation, "filing")

    def workflow4_source_kind(self, source: Path) -> str:
        source = self.require_within_root(source)
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise FileOperationError(f"Workflow 4 source is not a PDF file: {source}")
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(
                f"Workflow 4 refuses symlinks or reparse points: {source}"
            )
        if source.parent == self.registered_dir:
            return "registered"
        if source.parent == self.inbox_dir:
            raise FileOperationError(f"Workflow 4 refuses Inbox sources: {source}")
        parent = source.parent
        if parent.parent != self.library_root:
            raise FileOperationError(
                f"Workflow 4 only accepts Registered or top-level topic PDFs: {source}"
            )
        name = parent.name
        if (
            name.startswith(".")
            or normalized_reserved(name)
            or parent.is_symlink()
            or self._is_reparse_point(parent)
        ):
            raise FileOperationError(f"Workflow 4 source directory is managed or unsafe: {parent}")
        return "topic"

    def remove_empty_topic_directory(self, directory: Path) -> bool:
        directory = self.require_within_root(directory)
        if directory.parent != self.library_root:
            return False
        if (
            directory.name.startswith(".")
            or normalized_reserved(directory.name)
            or directory.is_symlink()
            or self._is_reparse_point(directory)
        ):
            return False
        if not directory.is_dir():
            return False
        try:
            next(directory.iterdir())
            return False
        except StopIteration:
            directory.rmdir()
            return True

    def _apply_no_replace(self, operation: FileOperation, label: str) -> None:
        source = operation.source
        assert source is not None
        if not source.is_file():
            raise FileOperationError(f"{label.title()} source no longer exists: {source}")
        stat = source.stat()
        if (
            operation.expected_size is not None
            and operation.expected_mtime_ns is not None
            and (
                stat.st_size != operation.expected_size
                or stat.st_mtime_ns != operation.expected_mtime_ns
            )
        ):
            raise FileOperationError(f"{label.title()} source changed after planning: {source}")
        if operation.target.exists():
            raise FileOperationError(f"Refusing to overwrite existing file: {operation.target}")
        operation.target.parent.mkdir(parents=False, exist_ok=True)
        try:
            if os.name != "nt":
                raise FileOperationError(
                    "No-overwrite moves are currently supported only on Windows"
                )
            # On Windows os.rename maps to a no-replace move. If another process
            # creates the target after validation, the kernel rejects the move.
            os.rename(source, operation.target)
        except Exception as exc:
            if isinstance(exc, FileOperationError):
                raise
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
            if os.name != "nt":
                raise FileOperationError(
                    "No-overwrite rollbacks are currently supported only on Windows"
                )
            os.rename(operation.target, source)
        except Exception as exc:
            if isinstance(exc, FileOperationError):
                raise
            raise FileOperationError(
                f"Rollback failed: {operation.target} -> {source}"
            ) from exc

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)


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
