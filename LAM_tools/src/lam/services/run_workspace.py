from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings


MANIFEST_NAME = ".lam-temp.json"


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupResult:
    cleaned: bool
    retained: bool = False
    error: str = ""


class RunWorkspace:
    """Manifested production workspace below ``.library_state/tmp``.

    The service owns creation, retention metadata and bounded Windows cleanup.
    Callers must close images, PDF readers and streams before ``cleanup``.
    """

    CLEANUP_RETRIES = 3
    CLEANUP_RETRY_SECONDS = 0.10

    def __init__(
        self,
        settings: Settings,
        *,
        run_id: str,
        workflow: str,
        artifact_type: str,
        cleanup_policy: str = "immediate",
    ):
        self.settings = settings
        self.run_id = self._safe(run_id, "run")
        self.workflow = self._safe(workflow, "workflow")
        self.artifact_type = artifact_type
        self.cleanup_policy = cleanup_policy
        self.created_at = datetime.now(timezone.utc)
        stamp = self.created_at.astimezone().strftime("%Y%m%d-%H%M%S-%f")
        base = settings.download_temp_dir or settings.state_dir / "tmp"
        self.root = Path(base).resolve()
        self.path = self._unique_path(
            self.root / f"{stamp}-{self.run_id}-{self.workflow}"
        )
        self.manifest_path = self.path / MANIFEST_NAME
        self.cleanup_error = ""

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        run_id: str,
        workflow: str,
        artifact_type: str,
        cleanup_policy: str = "immediate",
    ) -> "RunWorkspace":
        workspace = cls(
            settings,
            run_id=run_id,
            workflow=workflow,
            artifact_type=artifact_type,
            cleanup_policy=cleanup_policy,
        )
        workspace.path.mkdir(parents=True, exist_ok=False)
        workspace._write_manifest("active")
        return workspace

    def subdirectory(self, name: str) -> Path:
        safe = self._safe(name, "artifact")
        target = (self.path / safe).resolve()
        target.relative_to(self.path.resolve())
        target.mkdir(parents=True, exist_ok=True)
        return target

    def mark(self, status: str, **details: Any) -> None:
        if self.path.is_dir():
            self._write_manifest(status, **details)

    def cleanup(
        self,
        *,
        status: str,
        retain: bool = False,
        retention_hours: int | None = None,
    ) -> WorkspaceCleanupResult:
        if not self.path.exists():
            return WorkspaceCleanupResult(cleaned=True)
        if retain:
            hours = max(1, retention_hours or self.settings.temp_retention_hours)
            expires = datetime.now(timezone.utc) + timedelta(hours=hours)
            self._write_manifest(
                status,
                retained=True,
                expires_at=expires.isoformat(timespec="seconds"),
            )
            return WorkspaceCleanupResult(cleaned=False, retained=True)
        self._write_manifest(status, retained=False)
        last_error = ""
        for attempt in range(self.CLEANUP_RETRIES):
            try:
                shutil.rmtree(self.path)
                self._remove_empty_tmp_root_parent()
                return WorkspaceCleanupResult(cleaned=True)
            except FileNotFoundError:
                return WorkspaceCleanupResult(cleaned=True)
            except PermissionError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < self.CLEANUP_RETRIES:
                    time.sleep(self.CLEANUP_RETRY_SECONDS * (attempt + 1))
            except OSError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break
        self.cleanup_error = last_error or "workspace_cleanup_failed"
        try:
            self._write_manifest(
                "cleanup_failed",
                cleanup_error=self.cleanup_error,
                retained=True,
            )
        except OSError:
            pass
        return WorkspaceCleanupResult(cleaned=False, error=self.cleanup_error)

    def _write_manifest(self, status: str, **details: Any) -> None:
        payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "creator_pid": os.getpid(),
            "creator_user": getpass.getuser(),
            "artifact_type": self.artifact_type,
            "cleanup_policy": self.cleanup_policy,
            "status": status,
            **details,
        }
        temporary = self.manifest_path.with_name(f".{MANIFEST_NAME}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, self.manifest_path)

    def _remove_empty_tmp_root_parent(self) -> None:
        # The configured tmp root is a permanent management directory. Only
        # remove an empty intermediate parent when a future layout adds one.
        parent = self.path.parent
        if parent != self.root and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                pass

    @staticmethod
    def _safe(value: str, fallback: str) -> str:
        result = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-")
        return (result or fallback)[:80]

    @staticmethod
    def _unique_path(path: Path) -> Path:
        candidate = path
        counter = 1
        while candidate.exists():
            candidate = path.with_name(f"{path.name}-{counter:02d}")
            counter += 1
        return candidate


def read_temp_manifest(path: Path) -> dict[str, Any] | None:
    manifest = path / MANIFEST_NAME if path.is_dir() else path
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def temporary_inventory(settings: Settings) -> dict[str, Any]:
    root = (settings.download_temp_dir or settings.state_dir / "tmp").resolve()
    result: dict[str, Any] = {
        "temporary_directories": 0,
        "temporary_files": 0,
        "temporary_bytes": 0,
        "expired_temporary_artifacts": 0,
        "unreadable_temporary_artifacts": 0,
        "unknown_temporary_artifacts": 0,
        "test_temporary_artifacts": 0,
        "oldest_temporary_artifact": None,
        "artifacts": [],
    }
    if not root.is_dir():
        return result
    now = datetime.now(timezone.utc)
    oldest: datetime | None = None
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        result["unreadable_temporary_artifacts"] = 1
        return result
    for path in children:
        item: dict[str, Any] = {"path": path.name}
        try:
            if path.is_symlink() or _is_reparse_point(path):
                item.update(kind="unknown_temporary_artifact", status="reparse_refused")
                result["unknown_temporary_artifacts"] += 1
                result["artifacts"].append(item)
                continue
            stat = path.stat()
            created = datetime.fromtimestamp(stat.st_ctime, timezone.utc)
            oldest = created if oldest is None or created < oldest else oldest
            manifest = read_temp_manifest(path) if path.is_dir() else None
            if manifest:
                kind = str(manifest.get("artifact_type") or "production_temporary_artifact")
                expires = _parse_datetime(manifest.get("expires_at"))
                status = str(manifest.get("status") or "unknown")
                expired = bool(expires and expires <= now) or (
                    status in {"completed", "failed", "cleanup_failed"}
                    and created <= now - timedelta(hours=settings.temp_retention_hours)
                )
            elif path.is_dir() and is_strict_pytest_artifact_name(path.name):
                kind = "test_temporary_artifact"
                status = "historical"
                expired = created <= now - timedelta(hours=settings.temp_retention_hours)
                result["test_temporary_artifacts"] += 1
            elif path.is_file() and path.suffix.casefold() == ".part":
                kind = "download_partial"
                status = "historical"
                expired = created <= now - timedelta(hours=settings.temp_retention_hours)
            else:
                kind = "unknown_temporary_artifact"
                status = "unknown"
                expired = False
                result["unknown_temporary_artifacts"] += 1
            directories, files, size = _tree_stats_untrusted(path)
            result["temporary_directories"] += directories
            result["temporary_files"] += files
            result["temporary_bytes"] += size
            result["expired_temporary_artifacts"] += int(expired)
            item.update(
                kind=kind,
                status=status,
                expired=expired,
                directories=directories,
                files=files,
                bytes=size,
            )
        except OSError as exc:
            result["unreadable_temporary_artifacts"] += 1
            item.update(
                kind="unknown_temporary_artifact",
                status="unreadable",
                issue=type(exc).__name__,
            )
        result["artifacts"].append(item)
    result["oldest_temporary_artifact"] = (
        oldest.isoformat(timespec="seconds") if oldest else None
    )
    return result


def is_strict_pytest_artifact_name(name: str) -> bool:
    return bool(
        re.fullmatch(r"pytest-(?:\d+|of-[A-Za-z0-9_.-]+)(?:-[A-Za-z0-9_.-]+)?", name)
    )


def _tree_stats_untrusted(path: Path) -> tuple[int, int, int]:
    if path.is_file():
        return 0, 1, path.stat().st_size
    directories = 1
    files = 0
    size = 0
    for child in path.rglob("*"):
        if child.is_symlink() or _is_reparse_point(child):
            raise OSError("reparse_point_refused")
        if child.is_dir():
            directories += 1
        elif child.is_file():
            files += 1
            size += child.stat().st_size
    return directories, files, size


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attributes & 0x400)
