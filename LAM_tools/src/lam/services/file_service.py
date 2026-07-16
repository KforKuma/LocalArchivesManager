from __future__ import annotations

import os
from difflib import SequenceMatcher
from pathlib import Path

from ..directory_policy import DirectoryPolicy
from ..exceptions import FileOperationError
from ..models import FileOperation, OperationType
from ..utils.hashing import full_hash
from ..utils.filename import sanitize_filename
from ..utils.supplementary import is_supported_document_extension


class FileService:
    def __init__(
        self,
        library_root: Path,
        max_filename_length: int = 180,
        extra_reserved: tuple[str, ...] = (),
    ):
        self.library_root = library_root.resolve()
        self.inbox_dir = (self.library_root / "Inbox").resolve()
        self.registered_dir = (self.library_root / "Registered").resolve()
        self.policy = DirectoryPolicy(self.library_root, extra_reserved)
        self.topics_dir = self.policy.topics_root.resolve()
        self.max_filename_length = max_filename_length

    def require_within_root(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.library_root)
        except ValueError as exc:
            raise FileOperationError(f"Path escapes library root: {path}") from exc
        return resolved

    def validate_topic_folder(self, folder_name: str) -> Path:
        name = self.policy.validate_topic_folder(folder_name)
        target = self.policy.topic_path(name)
        if not target.exists():
            similar = self._suspiciously_similar(target)
            if similar:
                raise FileOperationError(
                    f"New topic folder {name!r} is suspiciously similar to existing {similar!r}"
                )
        return target

    def _suspiciously_similar(self, proposed: Path) -> str | None:
        parent = proposed.parent
        if not parent.is_dir():
            return None
        proposed_key = proposed.name.casefold()
        for entry in parent.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            existing_key = entry.name.casefold()
            if existing_key == proposed_key:
                return None
            if SequenceMatcher(None, proposed_key, existing_key).ratio() >= 0.88:
                try:
                    return entry.relative_to(self.topics_dir).as_posix()
                except ValueError:
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

    def plan_document_registration_move(
        self,
        source: Path,
        target_filename: str,
        catalogue_row: int,
        reason: str,
    ) -> FileOperation:
        """Plan one managed PDF/XLSX/XLS/CSV move from Inbox to Registered."""
        source = self.require_within_root(source)
        if not source.is_file() or source.parent != self.inbox_dir:
            raise FileOperationError(
                f"Document registration only moves direct Inbox files: {source}"
            )
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(
                f"Document registration refuses symlinks or reparse points: {source}"
            )
        if not is_supported_document_extension(source.suffix):
            raise FileOperationError(
                f"Unsupported document registration extension: {source.suffix or '<none>'}"
            )
        safe_name = sanitize_filename(
            target_filename,
            self.max_filename_length,
            preserve_extension_case=True,
        )
        target_path = Path(target_filename)
        if safe_name != target_filename or target_path.name != target_filename:
            raise FileOperationError(
                f"Document registration target filename is not already safe: {target_filename!r}"
            )
        if (
            not is_supported_document_extension(target_path.suffix)
            or target_path.suffix.casefold() != source.suffix.casefold()
        ):
            raise FileOperationError(
                "Document registration must preserve a supported source extension"
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

    def apply_document_registration_move(self, operation: FileOperation) -> None:
        source = operation.source
        assert source is not None
        source = self.require_within_root(source)
        target = self.require_within_root(operation.target)
        if source.parent != self.inbox_dir or not is_supported_document_extension(
            source.suffix
        ):
            raise FileOperationError(
                f"Document registration source is no longer eligible: {source}"
            )
        if source.is_symlink() or self._is_reparse_point(source):
            raise FileOperationError(
                f"Document registration source became a symlink or reparse point: {source}"
            )
        if target.parent != self.registered_dir:
            raise FileOperationError(
                f"Document registration target is not directly in Registered: {target}"
            )
        if (
            not is_supported_document_extension(target.suffix)
            or target.suffix.casefold() != source.suffix.casefold()
        ):
            raise FileOperationError(
                "Document registration target does not preserve the source extension"
            )
        safe_name = sanitize_filename(
            target.name,
            self.max_filename_length,
            preserve_extension_case=True,
        )
        if safe_name != target.name:
            raise FileOperationError(
                f"Document registration target filename became unsafe: {target.name!r}"
            )
        self._apply_no_replace(operation, "document registration")

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
        relative_topic = self.policy.relative_topic_for_path(operation.target.parent)
        if not relative_topic:
            raise FileOperationError(f"Workflow 4 target is outside Topics/: {operation.target}")
        self.validate_topic_folder(relative_topic)
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
        try:
            relative = source.relative_to(self.topics_dir)
        except ValueError as exc:
            try:
                root_relative = source.relative_to(self.library_root)
            except ValueError:
                root_relative = Path()
            if len(root_relative.parts) >= 2:
                raise FileOperationError(
                    "legacy_topic_location: Workflow 4 will not create or move from "
                    "root-level topic directories; run migrate-topics."
                ) from exc
            raise FileOperationError(
                f"Workflow 4 only accepts Registered or Topics PDFs: {source}"
            ) from exc
        if len(relative.parts) < 2:
            raise FileOperationError(f"Workflow 4 requires a topic below Topics/: {source}")
        for parent in source.parents:
            if parent == self.topics_dir:
                break
            if (
                parent.name.startswith(".")
                or parent.is_symlink()
                or self._is_reparse_point(parent)
            ):
                raise FileOperationError(
                    f"Workflow 4 source directory is managed or unsafe: {parent}"
                )
        return "topic"

    def remove_empty_topic_directory(self, directory: Path) -> bool:
        directory = self.require_within_root(directory)
        try:
            relative = directory.relative_to(self.topics_dir)
        except ValueError:
            return False
        if not relative.parts:
            return False
        if (
            any(part.startswith(".") for part in relative.parts)
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

    def plan_topic_directory_move(
        self, source: Path, target: Path
    ) -> tuple[tuple[str, str, int, int], ...]:
        source = self.require_within_root(source)
        target = self.require_within_root(target)
        if source.parent != self.library_root:
            raise FileOperationError("Legacy topic migration source must be a root directory")
        if source.is_symlink() or self._is_reparse_point(source) or not source.is_dir():
            raise FileOperationError(f"Legacy topic source is unsafe: {source}")
        if target.parent != self.topics_dir:
            raise FileOperationError("Topic migration target must be directly below Topics/")
        self.policy.validate_topic_folder(target.name)
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise FileOperationError(f"Topic migration target is not empty: {target}")
        return self._directory_signature(source)

    def apply_topic_directory_move(
        self,
        source: Path,
        target: Path,
        expected_signature: tuple[tuple[str, str, int, int], ...],
    ) -> None:
        source = self.require_within_root(source)
        target = self.require_within_root(target)
        if self._directory_signature(source) != expected_signature:
            raise FileOperationError(f"Topic directory changed after planning: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        removed_empty_target = False
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise FileOperationError(f"Topic migration target became non-empty: {target}")
            target.rmdir()
            removed_empty_target = True
        try:
            if os.name != "nt":
                raise FileOperationError(
                    "No-overwrite directory moves are currently supported only on Windows"
                )
            os.rename(source, target)
        except Exception as exc:
            if removed_empty_target:
                target.mkdir(parents=True, exist_ok=True)
            if isinstance(exc, FileOperationError):
                raise
            raise FileOperationError(f"Cannot migrate topic directory {source} to {target}") from exc
        if source.exists() or not target.is_dir():
            raise FileOperationError(f"Topic directory move verification failed: {source} -> {target}")

    def rollback_topic_directory_move(self, source: Path, target: Path) -> None:
        source = self.require_within_root(source)
        target = self.require_within_root(target)
        if source.exists() or not target.is_dir():
            raise FileOperationError(f"Cannot safely roll back topic migration: {target}")
        try:
            if os.name != "nt":
                raise FileOperationError(
                    "No-overwrite directory rollbacks are currently supported only on Windows"
                )
            os.rename(target, source)
        except Exception as exc:
            if isinstance(exc, FileOperationError):
                raise
            raise FileOperationError(f"Topic migration rollback failed: {target} -> {source}") from exc

    def _directory_signature(
        self, directory: Path
    ) -> tuple[tuple[str, str, int, int], ...]:
        if not directory.is_dir():
            raise FileOperationError(f"Topic directory is missing: {directory}")
        entries: list[tuple[str, str, int, int]] = []
        for path in [directory, *directory.rglob("*")]:
            if path.is_symlink() or self._is_reparse_point(path):
                raise FileOperationError(
                    f"Topic migration refuses symlinks or reparse points: {path}"
                )
            stat = path.stat()
            relative = "." if path == directory else path.relative_to(directory).as_posix()
            entries.append(
                (
                    relative,
                    "directory" if path.is_dir() else "file",
                    stat.st_size if path.is_file() else 0,
                    stat.st_mtime_ns,
                )
            )
        return tuple(sorted(entries, key=lambda item: item[0].casefold()))

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
        operation.target.parent.mkdir(parents=True, exist_ok=True)
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
