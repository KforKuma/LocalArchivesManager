from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..exceptions import FileOperationError
from ..models import DiffType, FileDiff, FileSnapshot
from ..utils.hashing import quick_hash
from ..utils.normalize import normalized_relative_path


EXCLUDED_DIRECTORIES = {".git", ".library_state", "__pycache__", "lam_tools"}


class SnapshotService:
    def __init__(self, library_root: Path, state_dir: Path):
        self.library_root = library_root
        self.state_dir = state_dir
        self.catalogue_snapshot_path = state_dir / "catalogue_snapshot.json"
        self.file_manifest_path = state_dir / "file_manifest.json"
        self.last_diff_path = state_dir / "last_diff.json"

    @property
    def initialized(self) -> bool:
        return self.catalogue_snapshot_path.is_file() and self.file_manifest_path.is_file()

    def load_manifest(self) -> dict[str, FileSnapshot]:
        if not self.file_manifest_path.is_file():
            return {}
        payload = self._load_json(self.file_manifest_path)
        entries = payload.get("files", [])
        return {
            normalized_relative_path(item["relative_path"]): FileSnapshot(**item)
            for item in entries
        }

    def load_catalogue_snapshot(self) -> dict[str, Any]:
        if not self.catalogue_snapshot_path.is_file():
            return {}
        return self._load_json(self.catalogue_snapshot_path)

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
        for path in self.library_root.rglob("*.pdf"):
            if self._excluded(path):
                continue
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

    def _excluded(self, path: Path) -> bool:
        relative_parts = path.relative_to(self.library_root).parts[:-1]
        return any(part.casefold() in EXCLUDED_DIRECTORIES for part in relative_parts)

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
            if old.size == new.size and old.quick_hash == new.quick_hash:
                unchanged += 1
            else:
                diffs.append(
                    FileDiff(
                        DiffType.MODIFIED,
                        path=new.relative_path,
                        details={"old_size": old.size, "new_size": new.size},
                    )
                )

        removed = {key: previous[key] for key in previous.keys() - current.keys()}
        added = {key: current[key] for key in current.keys() - previous.keys()}
        removed_by_fingerprint: dict[tuple[int, str], list[str]] = defaultdict(list)
        added_by_fingerprint: dict[tuple[int, str], list[str]] = defaultdict(list)
        for key, item in removed.items():
            removed_by_fingerprint[(item.size, item.quick_hash)].append(key)
        for key, item in added.items():
            added_by_fingerprint[(item.size, item.quick_hash)].append(key)

        matched_removed: set[str] = set()
        matched_added: set[str] = set()
        for fingerprint in removed_by_fingerprint.keys() & added_by_fingerprint.keys():
            old_keys = removed_by_fingerprint[fingerprint]
            new_keys = added_by_fingerprint[fingerprint]
            if len(old_keys) == 1 and len(new_keys) == 1:
                old_key, new_key = old_keys[0], new_keys[0]
                matched_removed.add(old_key)
                matched_added.add(new_key)
                diffs.append(
                    FileDiff(
                        DiffType.MOVED_OR_RENAMED,
                        old_path=removed[old_key].relative_path,
                        new_path=added[new_key].relative_path,
                    )
                )
            else:
                matched_removed.update(old_keys)
                matched_added.update(new_keys)
                diffs.append(
                    FileDiff(
                        DiffType.POSSIBLE_COLLISION,
                        details={
                            "old_paths": [removed[key].relative_path for key in old_keys],
                            "new_paths": [added[key].relative_path for key in new_keys],
                        },
                    )
                )

        for key, item in sorted(removed.items()):
            if key not in matched_removed:
                diffs.append(FileDiff(DiffType.MISSING, path=item.relative_path))
        for key, item in sorted(added.items()):
            if key not in matched_added:
                diffs.append(FileDiff(DiffType.ADDED, path=item.relative_path))
        return diffs, unchanged

    @staticmethod
    def compare_catalogue(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
        old_rows = {row["row_number"]: row.get("fields", {}) for row in previous.get("rows", [])}
        new_rows = {row["row_number"]: row.get("fields", {}) for row in current.get("rows", [])}
        changes: list[dict[str, Any]] = []
        for row_number in sorted(old_rows.keys() | new_rows.keys()):
            old = old_rows.get(row_number)
            new = new_rows.get(row_number)
            if old == new:
                continue
            if old is None:
                changes.append({"row_number": row_number, "change": "row_added"})
            elif new is None:
                changes.append({"row_number": row_number, "change": "row_missing"})
            else:
                for field_name in sorted(old.keys() | new.keys()):
                    if old.get(field_name) != new.get(field_name):
                        changes.append(
                            {
                                "row_number": row_number,
                                "change": "field_changed",
                                "field": field_name,
                                "old": old.get(field_name),
                                "new": new.get(field_name),
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
        manifest_payload = {
            "version": 1,
            "files": [asdict(item) for _, item in sorted(manifest.items())],
        }
        self._atomic_json(self.catalogue_snapshot_path, catalogue_snapshot)
        self._atomic_json(self.file_manifest_path, manifest_payload)
        self._atomic_json(self.last_diff_path, diff_payload)

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
