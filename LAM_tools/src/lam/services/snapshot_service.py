from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..directory_policy import DirectoryPolicy, RootDirectoryKind
from ..exceptions import FileOperationError
from ..models import DiffType, FileDiff, FileSnapshot
from ..services.journal_service import completed_file_movements
from ..utils.hashing import full_hash, quick_hash
from ..utils.normalize import normalized_relative_path, normalized_text


MANIFEST_VERSION = 2
SUPPORTED_MANIFEST_VERSIONS = {None, 1, MANIFEST_VERSION}
MANAGED_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".xlsx", ".xls", ".csv"})


class SnapshotService:
    def __init__(
        self,
        library_root: Path,
        state_dir: Path,
        extra_reserved: tuple[str, ...] = (),
    ):
        self.library_root = library_root.resolve()
        self.state_dir = state_dir
        self.policy = DirectoryPolicy(self.library_root, extra_reserved)
        self.catalogue_snapshot_path = state_dir / "catalogue_snapshot.json"
        self.file_manifest_path = state_dir / "file_manifest.json"
        self.last_diff_path = state_dir / "last_diff.json"
        self.commit_marker_path = state_dir / "snapshot_commit.json"
        self.generations_dir = state_dir / "snapshot_generations"

    @property
    def initialized(self) -> bool:
        if self.commit_marker_path.is_file():
            generation_dir = self._active_generation_dir()
            return all(
                (generation_dir / name).is_file()
                for name in ("catalogue_snapshot.json", "file_manifest.json", "last_diff.json")
            )
        if self.generations_dir.is_dir():
            return False
        return self.catalogue_snapshot_path.is_file() and self.file_manifest_path.is_file()

    def load_manifest(self) -> dict[str, FileSnapshot]:
        path = self._active_path("file_manifest.json")
        if not path.is_file():
            return {}
        payload = self._load_json(path)
        version = payload.get("version")
        if not (
            version is None
            or (
                isinstance(version, int)
                and not isinstance(version, bool)
                and version in SUPPORTED_MANIFEST_VERSIONS
            )
        ):
            raise FileOperationError(
                f"Unsupported file manifest version {version!r}: {path}"
            )
        entries = payload.get("files", [])
        return {
            normalized_relative_path(item["relative_path"]): FileSnapshot(**item)
            for item in entries
        }

    def load_catalogue_snapshot(self) -> dict[str, Any]:
        path = self._active_path("catalogue_snapshot.json")
        if not path.is_file():
            return {}
        return self._load_json(path)

    def _active_path(self, filename: str) -> Path:
        if self.commit_marker_path.is_file():
            path = self._active_generation_dir() / filename
            payload = self._load_json(path)
            marker = self._load_json(self.commit_marker_path)
            if payload.get("_state", {}).get("generation_id") != marker.get("generation_id"):
                raise FileOperationError(f"State generation mismatch: {path}")
            return path
        if self.generations_dir.is_dir():
            return self.generations_dir / "__no_committed_generation__" / filename
        return self.state_dir / filename

    def _active_generation_dir(self) -> Path:
        marker = self._load_json(self.commit_marker_path)
        generation_id = str(marker.get("generation_id") or "")
        if not generation_id or Path(generation_id).name != generation_id:
            raise FileOperationError("Invalid snapshot commit marker")
        generation_dir = self.generations_dir / generation_id
        if not generation_dir.is_dir():
            raise FileOperationError(
                f"Committed snapshot generation is missing: {generation_id}"
            )
        return generation_dir

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except Exception as exc:
            raise FileOperationError(f"Cannot read state file: {path}") from exc
        if not isinstance(value, dict):
            raise FileOperationError(f"Invalid state file root object: {path}")
        return value

    def scan(self, previous: dict[str, FileSnapshot] | None = None) -> dict[str, FileSnapshot]:
        previous = previous or {}
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        current: dict[str, FileSnapshot] = {}
        for path in self._managed_document_paths():
            relative = path.relative_to(self.library_root).as_posix()
            key = normalized_relative_path(relative)
            stat = path.stat()
            old = previous.get(key)
            if old and old.size == stat.st_size and old.mtime_ns == stat.st_mtime_ns:
                fingerprint = old.quick_hash
                full = old.full_hash
                last_seen = old.last_seen
            else:
                fingerprint = quick_hash(path)
                full = None
                last_seen = now
            current[key] = FileSnapshot(
                relative_path=relative,
                filename=path.name,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                quick_hash=fingerprint,
                full_hash=full,
                last_seen=last_seen,
            )
        return current

    def _managed_document_paths(self) -> list[Path]:
        paths: list[Path] = []
        for directory in (self.library_root / "Inbox", self.library_root / "Registered"):
            if not directory.is_dir():
                continue
            paths.extend(
                path
                for path in directory.iterdir()
                if (
                    path.is_file()
                    and path.suffix.casefold() in MANAGED_DOCUMENT_EXTENSIONS
                    and not path.name.startswith(".")
                    and not path.is_symlink()
                    and not self._is_reparse_point(path)
                )
            )
        topics = self.policy.topics_root
        if topics.is_dir():
            for path in topics.rglob("*"):
                if (
                    not path.is_file()
                    or path.suffix.casefold() not in MANAGED_DOCUMENT_EXTENSIONS
                ):
                    continue
                try:
                    relative = path.relative_to(topics)
                except ValueError:
                    continue
                if (
                    len(relative.parts) < 2
                    or any(part.startswith(".") for part in relative.parts)
                    or path.is_symlink()
                    or self._is_reparse_point(path)
                    or any(
                        parent.is_symlink() or self._is_reparse_point(parent)
                        for parent in path.parents
                        if parent != topics and topics in parent.parents
                    )
                ):
                    continue
                paths.append(path)
        return sorted(set(paths), key=lambda path: path.as_posix().casefold())

    # Kept as a private compatibility alias for callers/tests from pre-0.5.1.
    def _managed_pdf_paths(self) -> list[Path]:
        return [
            path
            for path in self._managed_document_paths()
            if path.suffix.casefold() == ".pdf"
        ]

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            attributes = getattr(path.stat(), "st_file_attributes", 0)
        except OSError:
            return True
        return bool(attributes & 0x400)

    def root_items(
        self,
        referenced_legacy_roots: set[str],
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for path in sorted(self.library_root.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_file():
                if path.suffix.casefold() == ".pdf":
                    items.append({"path": path.name, "classification": "unmanaged_item"})
                continue
            if not path.is_dir():
                continue
            kind = self.policy.classify_root_directory(
                path,
                referenced_legacy_roots=referenced_legacy_roots,
            )
            if kind == RootDirectoryKind.LEGACY_TOPIC_CANDIDATE:
                items.append({"path": path.name, "classification": "legacy_topic_location"})
            elif kind == RootDirectoryKind.UNKNOWN:
                items.append({"path": path.name, "classification": "unmanaged_item"})
        return items

    def compare(
        self,
        previous: dict[str, FileSnapshot],
        current: dict[str, FileSnapshot],
    ) -> tuple[list[FileDiff], int]:
        diffs: list[FileDiff] = []
        unchanged = 0
        common = previous.keys() & current.keys()
        for key in sorted(common):
            old = previous[key]
            new = current[key]
            if (
                old.size == new.size
                and old.mtime_ns == new.mtime_ns
                and old.quick_hash == new.quick_hash
            ):
                unchanged += 1
            else:
                diffs.append(
                    FileDiff(
                        DiffType.MODIFIED,
                        path=new.relative_path,
                        details={
                            "old_size": old.size,
                            "new_size": new.size,
                            "old_mtime_ns": old.mtime_ns,
                            "new_mtime_ns": new.mtime_ns,
                        },
                    )
                )

        removed = {key: previous[key] for key in previous.keys() - current.keys()}
        added = {key: current[key] for key in current.keys() - previous.keys()}
        matched_removed: set[str] = set()
        matched_added: set[str] = set()

        for movement in completed_file_movements(self.state_dir, self.library_root):
            old_key = normalized_relative_path(movement["source"])
            new_key = normalized_relative_path(movement["target"])
            if old_key not in removed or new_key not in added:
                continue
            old_item = removed[old_key]
            new_item = added[new_key]
            if old_item.size != new_item.size or old_item.quick_hash != new_item.quick_hash:
                continue
            matched_removed.add(old_key)
            matched_added.add(new_key)
            diffs.append(
                FileDiff(
                    DiffType.EXPECTED_MOVE_OR_RENAME,
                    old_path=old_item.relative_path,
                    new_path=new_item.relative_path,
                    details={
                        "matched_by": "operation_journal",
                        "run_id": movement.get("run_id"),
                        "workflow": movement.get("workflow"),
                    },
                )
            )

        removed_by_filename: dict[str, list[str]] = defaultdict(list)
        added_by_filename: dict[str, list[str]] = defaultdict(list)
        for key, item in removed.items():
            if key in matched_removed:
                continue
            removed_by_filename[normalized_text(item.filename)].append(key)
        for key, item in added.items():
            if key in matched_added:
                continue
            added_by_filename[normalized_text(item.filename)].append(key)

        for filename in removed_by_filename.keys() & added_by_filename.keys():
            old_keys = removed_by_filename[filename]
            new_keys = added_by_filename[filename]
            if len(old_keys) == 1 and len(new_keys) == 1:
                old_key, new_key = old_keys[0], new_keys[0]
                matched_removed.add(old_key)
                matched_added.add(new_key)
                old_item = removed[old_key]
                new_item = added[new_key]
                diffs.append(
                    FileDiff(
                        DiffType.MOVED_OR_RENAMED,
                        old_path=old_item.relative_path,
                        new_path=new_item.relative_path,
                        details={
                            "matched_by": "filename",
                            "content_changed": (
                                old_item.size != new_item.size
                                or old_item.quick_hash != new_item.quick_hash
                            ),
                        },
                    )
                )
            else:
                matched_removed.update(old_keys)
                matched_added.update(new_keys)
                diffs.append(
                    FileDiff(
                        DiffType.POSSIBLE_COLLISION,
                        details={
                            "matched_by": "filename",
                            "old_paths": [removed[key].relative_path for key in old_keys],
                            "new_paths": [added[key].relative_path for key in new_keys],
                        },
                    )
                )

        remaining_removed = {
            key: item for key, item in removed.items() if key not in matched_removed
        }
        remaining_added = {
            key: item for key, item in added.items() if key not in matched_added
        }
        removed_by_fingerprint: dict[tuple[int, str], list[str]] = defaultdict(list)
        added_by_fingerprint: dict[tuple[int, str], list[str]] = defaultdict(list)
        for key, item in remaining_removed.items():
            removed_by_fingerprint[(item.size, item.quick_hash)].append(key)
        for key, item in remaining_added.items():
            added_by_fingerprint[(item.size, item.quick_hash)].append(key)
        for fingerprint in removed_by_fingerprint.keys() & added_by_fingerprint.keys():
            old_keys = removed_by_fingerprint[fingerprint]
            new_keys = added_by_fingerprint[fingerprint]
            matched_removed.update(old_keys)
            matched_added.update(new_keys)
            if len(old_keys) == 1 and len(new_keys) == 1:
                diffs.append(
                    FileDiff(
                        DiffType.QUICK_HASH_CANDIDATE,
                        old_path=removed[old_keys[0]].relative_path,
                        new_path=added[new_keys[0]].relative_path,
                        details={"matched_by": "quick_hash_candidate"},
                    )
                )
            else:
                diffs.append(
                    FileDiff(
                        DiffType.POSSIBLE_COLLISION,
                        details={
                            "matched_by": "ambiguous_quick_hash_candidates",
                            "old_paths": [removed[key].relative_path for key in old_keys],
                            "new_paths": [added[key].relative_path for key in new_keys],
                        },
                    )
                )

        current_by_fingerprint: dict[tuple[int, str], list[str]] = defaultdict(list)
        for key, item in current.items():
            current_by_fingerprint[(item.size, item.quick_hash)].append(key)
        original_added = set(added)
        for keys in current_by_fingerprint.values():
            if len(keys) < 2 or not (set(keys) & original_added):
                continue
            hashes: list[str] = []
            hash_failed = False
            for key in keys:
                item = current[key]
                try:
                    item.full_hash = item.full_hash or full_hash(
                        self.library_root / item.relative_path
                    )
                    hashes.append(item.full_hash)
                except OSError:
                    hash_failed = True
                    break
            if not hash_failed and len(set(hashes)) > 1:
                continue
            diffs.append(
                FileDiff(
                    DiffType.POSSIBLE_COLLISION,
                    details={
                        "matched_by": "coexisting_files",
                        "paths": [current[key].relative_path for key in keys],
                        "full_hash_status": "unavailable" if hash_failed else "identical",
                    },
                )
            )
            matched_added.update(set(keys) & original_added)

        for key, item in sorted(removed.items()):
            if key not in matched_removed:
                diffs.append(FileDiff(DiffType.MISSING, path=item.relative_path))
        for key, item in sorted(added.items()):
            if key not in matched_added:
                diffs.append(FileDiff(DiffType.ADDED, path=item.relative_path))
        return diffs, unchanged

    @staticmethod
    def compare_catalogue(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
        old_rows = list(previous.get("rows", []))
        new_rows = list(current.get("rows", []))
        old_by_uid = {
            normalized_text(row.get("paper_uuid") or row.get("fields", {}).get("paper_uuid")): index
            for index, row in enumerate(old_rows)
            if normalized_text(row.get("paper_uuid") or row.get("fields", {}).get("paper_uuid"))
        }
        new_by_uid = {
            normalized_text(row.get("paper_uuid") or row.get("fields", {}).get("paper_uuid")): index
            for index, row in enumerate(new_rows)
            if normalized_text(row.get("paper_uuid") or row.get("fields", {}).get("paper_uuid"))
        }
        paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
        matched_old: set[int] = set()
        matched_new: set[int] = set()
        for uid in old_by_uid.keys() & new_by_uid.keys():
            old_index, new_index = old_by_uid[uid], new_by_uid[uid]
            matched_old.add(old_index)
            matched_new.add(new_index)
            paired.append((old_rows[old_index], new_rows[new_index]))
        remaining_old_by_row = {
            row.get("row_number"): index
            for index, row in enumerate(old_rows)
            if index not in matched_old
        }
        remaining_new_by_row = {
            row.get("row_number"): index
            for index, row in enumerate(new_rows)
            if index not in matched_new
        }
        for row_number in remaining_old_by_row.keys() & remaining_new_by_row.keys():
            old_index = remaining_old_by_row[row_number]
            new_index = remaining_new_by_row[row_number]
            matched_old.add(old_index)
            matched_new.add(new_index)
            paired.append((old_rows[old_index], new_rows[new_index]))

        changes: list[dict[str, Any]] = []
        for old_row, new_row in sorted(
            paired, key=lambda pair: int(pair[1].get("row_number") or pair[0].get("row_number") or 0)
        ):
            old = old_row.get("fields", {})
            new = new_row.get("fields", {})
            if old == new:
                continue
            for field_name in sorted(old.keys() | new.keys()):
                if old.get(field_name) != new.get(field_name):
                    changes.append(
                        {
                            "row_number": new_row.get("row_number"),
                            "paper_uuid": new_row.get("paper_uuid")
                            or new.get("paper_uuid")
                            or old_row.get("paper_uuid")
                            or old.get("paper_uuid"),
                            "change": "field_changed",
                            "field": field_name,
                            "old": old.get(field_name),
                            "new": new.get(field_name),
                        }
                    )
        for index, row in enumerate(old_rows):
            if index not in matched_old:
                changes.append(
                    {
                        "row_number": row.get("row_number"),
                        "paper_uuid": row.get("paper_uuid") or row.get("fields", {}).get("paper_uuid"),
                        "change": "row_missing",
                    }
                )
        for index, row in enumerate(new_rows):
            if index not in matched_new:
                changes.append(
                    {
                        "row_number": row.get("row_number"),
                        "paper_uuid": row.get("paper_uuid") or row.get("fields", {}).get("paper_uuid"),
                        "change": "row_added",
                    }
                )
        old_documents = list(previous.get("documents", []))
        new_documents = list(current.get("documents", []))
        old_documents_by_id = {
            normalized_text(
                row.get("document_id") or row.get("fields", {}).get("document_id")
            ): index
            for index, row in enumerate(old_documents)
            if normalized_text(
                row.get("document_id") or row.get("fields", {}).get("document_id")
            )
        }
        new_documents_by_id = {
            normalized_text(
                row.get("document_id") or row.get("fields", {}).get("document_id")
            ): index
            for index, row in enumerate(new_documents)
            if normalized_text(
                row.get("document_id") or row.get("fields", {}).get("document_id")
            )
        }
        matched_old_documents: set[int] = set()
        matched_new_documents: set[int] = set()
        for document_id in old_documents_by_id.keys() & new_documents_by_id.keys():
            old_index = old_documents_by_id[document_id]
            new_index = new_documents_by_id[document_id]
            matched_old_documents.add(old_index)
            matched_new_documents.add(new_index)
            old_row = old_documents[old_index]
            new_row = new_documents[new_index]
            old_fields = old_row.get("fields", {})
            new_fields = new_row.get("fields", {})
            if old_fields == new_fields:
                continue
            for field_name in sorted(old_fields.keys() | new_fields.keys()):
                if old_fields.get(field_name) == new_fields.get(field_name):
                    continue
                changes.append(
                    {
                        "sheet": "Documents",
                        "row_number": new_row.get("row_number"),
                        "document_id": new_row.get("document_id")
                        or new_fields.get("document_id")
                        or old_row.get("document_id")
                        or old_fields.get("document_id"),
                        "paper_uuid": new_row.get("paper_uuid")
                        or new_fields.get("paper_uuid")
                        or old_row.get("paper_uuid")
                        or old_fields.get("paper_uuid"),
                        "change": "field_changed",
                        "field": field_name,
                        "old": old_fields.get(field_name),
                        "new": new_fields.get(field_name),
                    }
                )
        for index, row in enumerate(old_documents):
            if index in matched_old_documents:
                continue
            fields = row.get("fields", {})
            changes.append(
                {
                    "sheet": "Documents",
                    "row_number": row.get("row_number"),
                    "document_id": row.get("document_id")
                    or fields.get("document_id"),
                    "paper_uuid": row.get("paper_uuid")
                    or fields.get("paper_uuid"),
                    "change": "row_missing",
                }
            )
        for index, row in enumerate(new_documents):
            if index in matched_new_documents:
                continue
            fields = row.get("fields", {})
            changes.append(
                {
                    "sheet": "Documents",
                    "row_number": row.get("row_number"),
                    "document_id": row.get("document_id")
                    or fields.get("document_id"),
                    "paper_uuid": row.get("paper_uuid")
                    or fields.get("paper_uuid"),
                    "change": "row_added",
                }
            )
        return changes

    def commit(
        self,
        catalogue_snapshot: dict[str, Any],
        manifest: dict[str, FileSnapshot],
        diff_payload: dict[str, Any],
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        generation_id = (
            datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        state_meta = {
            "generation_id": generation_id,
            "generated_at": generated_at,
        }
        catalogue_payload = {**catalogue_snapshot, "_state": state_meta}
        manifest_payload = {
            "version": MANIFEST_VERSION,
            "files": [asdict(item) for _, item in sorted(manifest.items())],
            "_state": state_meta,
        }
        diff_payload = {**diff_payload, "_state": state_meta}

        generation_dir = self.generations_dir / generation_id
        generation_dir.mkdir(parents=True, exist_ok=False)
        payloads = {
            "catalogue_snapshot.json": catalogue_payload,
            "file_manifest.json": manifest_payload,
            "last_diff.json": diff_payload,
        }
        for filename, payload in payloads.items():
            self._atomic_json(generation_dir / filename, payload)
        for filename in payloads:
            validated = self._load_json(generation_dir / filename)
            if validated.get("_state", {}).get("generation_id") != generation_id:
                raise FileOperationError(
                    f"Snapshot generation validation failed: {filename}"
                )

        # Canonical paths remain compatibility mirrors. The commit marker is
        # switched last, so readers either see the complete old or new generation.
        self._atomic_json(self.catalogue_snapshot_path, catalogue_payload)
        self._atomic_json(self.file_manifest_path, manifest_payload)
        self._atomic_json(self.last_diff_path, diff_payload)
        self._atomic_json(
            self.commit_marker_path,
            {**state_meta, "files": sorted(payloads)},
        )

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as existing:
                    if json.load(existing) == payload:
                        return
            except (OSError, json.JSONDecodeError):
                pass
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                handle.write("\n")
            os.replace(temporary, path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise FileOperationError(f"Cannot commit state file: {path}") from exc
