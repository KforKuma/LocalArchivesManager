from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..config import Settings
from ..directory_policy import DirectoryPolicy
from ..exceptions import FileOperationError
from ..models import WorkflowResult
from ..services.report_service import ReportService, append_change_log


BACKUP_PATTERN = re.compile(
    r"^catalogue\.backup\.\d{8}-\d{6}(?:-\d{2})?\.xlsx$",
    re.IGNORECASE,
)
PROTECTED_NAMES = {
    "catalogue.xlsx",
    "agents.md",
    "workflows.md",
    "summary.md",
}


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    path: Path
    kind: str
    reason: str
    size_bytes: int
    file_count: int

    def report(self, root: Path, action: str) -> dict[str, Any]:
        return {
            "action": action,
            "path": self.path.relative_to(root).as_posix(),
            "kind": self.kind,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
        }


class CleanupWorkflow:
    BACKUP_KEEP_COUNT = 10
    BACKUP_KEEP_DAYS = 30
    REPORT_KEEP_COUNT = 200
    REPORT_KEEP_DAYS = 90
    LOG_KEEP_COUNT = 5
    JOURNAL_KEEP_DAYS = 30
    TMP_MIN_AGE_HOURS = 24
    EXPORT_TMP_MIN_AGE_HOURS = 24

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.library_root.resolve()
        self.policy = DirectoryPolicy(
            self.root, settings.reserved_root_directories
        )

    def run(self, *, dry_run: bool) -> WorkflowResult:
        result = WorkflowResult(
            "cleanup",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        candidates = self.plan()
        result.counts = {
            "planned_entries": len(candidates),
            "planned_files": sum(item.file_count for item in candidates),
            "estimated_bytes": sum(item.size_bytes for item in candidates),
        }
        if dry_run:
            result.completed.extend(
                item.report(self.root, "would_delete") for item in candidates
            )
            result.details["estimated_release_bytes"] = result.counts["estimated_bytes"]
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        deleted: list[CleanupCandidate] = []
        released = 0
        deleted_files = 0
        for candidate in candidates:
            try:
                self._delete_candidate(candidate)
                deleted.append(candidate)
                released += candidate.size_bytes
                deleted_files += candidate.file_count
                result.completed.append(candidate.report(self.root, "deleted"))
            except (OSError, FileOperationError) as exc:
                result.failures.append(
                    {
                        "path": candidate.path.relative_to(self.root).as_posix(),
                        "issue": str(exc),
                    }
                )
        result.changed_files = len(deleted)
        result.counts.update(
            {
                "deleted_entries": len(deleted),
                "deleted_files": deleted_files,
                "released_bytes": released,
            }
        )
        result.details["released_bytes"] = released
        if deleted:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Cleanup",
                action="Remove expired allowlisted machine-generated files",
                files_changed=len(deleted),
                catalogue_rows_changed=0,
                reason="Applied the configured maintenance retention policy",
                uncertainty=f"{len(result.failures)} cleanup failure(s)",
            )
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def plan(self) -> list[CleanupCandidate]:
        now = datetime.now(timezone.utc)
        candidates: list[CleanupCandidate] = []
        candidates.extend(self._backup_candidates(now))
        candidates.extend(self._report_candidates(now))
        candidates.extend(self._log_candidates())
        candidates.extend(self._journal_candidates(now))
        candidates.extend(self._tmp_candidates(now))
        candidates.extend(self._citation_export_candidates(now))
        candidates.extend(self._metadata_cache_candidates(now))
        candidates.extend(self._citation_export_cache_candidates(now))
        candidates.extend(self._snapshot_generation_candidates())
        unique: dict[Path, CleanupCandidate] = {}
        for item in candidates:
            unique[item.path.resolve()] = item
        return sorted(unique.values(), key=lambda item: item.path.as_posix().casefold())

    def _backup_candidates(self, now: datetime) -> list[CleanupCandidate]:
        backups = sorted(
            (
                path
                for path in self.root.iterdir()
                if path.is_file() and BACKUP_PATTERN.fullmatch(path.name)
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        valid_backups = []
        for path in backups:
            try:
                workbook = load_workbook(path, read_only=True)
                workbook.close()
            except Exception:
                continue
            valid_backups.append(path)
        protected = self._protected_backup_paths()
        cutoff = now - timedelta(days=self.BACKUP_KEEP_DAYS)
        results = []
        for index, path in enumerate(valid_backups):
            if (
                index < self.BACKUP_KEEP_COUNT
                or self._mtime(path) >= cutoff
                or path.resolve() in protected
            ):
                continue
            results.append(
                self._candidate(
                    path,
                    "catalogue_backup",
                    "valid_backup_beyond_latest_10_and_older_than_30_days",
                )
            )
        return results

    def _protected_backup_paths(self) -> set[Path]:
        protected: set[Path] = set()
        runs = self.settings.state_dir / "runs"
        if not runs.is_dir():
            return protected
        for journal_path in runs.glob("*/operation_journal.json"):
            try:
                payload = json.loads(journal_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("status") == "final_check_committed":
                continue
            pending = [payload]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, str) and "catalogue.backup." in value:
                    candidate = Path(value)
                    if not candidate.is_absolute():
                        candidate = self.root / candidate.name
                    try:
                        candidate.resolve().relative_to(self.root)
                    except ValueError:
                        continue
                    protected.add(candidate.resolve())
        return protected

    def _report_candidates(self, now: datetime) -> list[CleanupCandidate]:
        directory = self.settings.reports_dir
        if not directory.is_dir():
            return []
        groups: dict[str, list[Path]] = {}
        for path in directory.iterdir():
            if path.is_file() and path.suffix.casefold() in {".json", ".md"}:
                groups.setdefault(path.stem, []).append(path)
        ordered = sorted(
            groups.values(),
            key=lambda group: max(path.stat().st_mtime_ns for path in group),
            reverse=True,
        )
        cutoff = now - timedelta(days=self.REPORT_KEEP_DAYS)
        results = []
        for index, group in enumerate(ordered):
            newest = max(self._mtime(path) for path in group)
            if index < self.REPORT_KEEP_COUNT or newest >= cutoff:
                continue
            for path in group:
                candidate = self._maybe_candidate(path, "report", "older_than_retention")
                if candidate:
                    results.append(candidate)
        return results

    def _log_candidates(self) -> list[CleanupCandidate]:
        directory = self.settings.logs_dir
        if not directory.is_dir():
            return []
        logs = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and path.name.casefold().startswith("lam.log")
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        active = {path for path in logs if path.name.casefold() == "lam.log"}
        rotated = [path for path in logs if path not in active]
        keep = active | set(rotated[: self.LOG_KEEP_COUNT])
        results = []
        for path in logs:
            if path in keep:
                continue
            candidate = self._maybe_candidate(
                path, "log", "beyond_latest_log_rotations"
            )
            if candidate:
                results.append(candidate)
        return results

    def _journal_candidates(self, now: datetime) -> list[CleanupCandidate]:
        runs = self.settings.state_dir / "runs"
        if not runs.is_dir():
            return []
        cutoff = now - timedelta(days=self.JOURNAL_KEEP_DAYS)
        results = []
        for run_dir in runs.iterdir():
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            journal = run_dir / "operation_journal.json"
            if not journal.is_file():
                continue
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("status") != "final_check_committed":
                continue
            finished = self._parse_datetime(payload.get("finished_at")) or self._mtime(journal)
            if finished >= cutoff:
                continue
            candidate = self._maybe_candidate(
                run_dir, "completed_operation_journal", "completed_over_30_days"
            )
            if candidate:
                results.append(candidate)
        return results

    def _tmp_candidates(self, now: datetime) -> list[CleanupCandidate]:
        directory = self.settings.state_dir / "tmp"
        if not directory.is_dir():
            return []
        cutoff = now - timedelta(hours=self.TMP_MIN_AGE_HOURS)
        results = []
        for path in directory.iterdir():
            if path.is_symlink() or self._mtime(path) >= cutoff:
                continue
            candidate = self._maybe_candidate(
                path, "temporary", "stale_temporary_artifact"
            )
            if candidate:
                results.append(candidate)
        return results

    def _metadata_cache_candidates(self, now: datetime) -> list[CleanupCandidate]:
        directory = self.settings.metadata_cache_dir
        if directory is None or not directory.is_dir():
            return []
        results: list[CleanupCandidate] = []
        for path in directory.rglob("*.json"):
            if path.name.casefold() == "daily_counts.json" or path.is_symlink():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                expires = self._parse_datetime(payload.get("expires_at"))
            except Exception:
                continue
            if expires is None or expires > now:
                continue
            candidate = self._maybe_candidate(
                path, "metadata_cache", "cache_entry_expired"
            )
            if candidate:
                results.append(candidate)
            raw_name = payload.get("raw_response_path")
            if raw_name and Path(str(raw_name)).name == str(raw_name):
                raw_path = path.with_name(str(raw_name))
                if raw_path.is_file() and not raw_path.is_symlink():
                    candidate = self._maybe_candidate(
                        raw_path, "metadata_cache", "cache_entry_expired"
                    )
                    if candidate:
                        results.append(candidate)
        return results

    def _citation_export_candidates(
        self, now: datetime
    ) -> list[CleanupCandidate]:
        directory = self.settings.zotero_exports_dir
        if not directory.is_dir():
            return []
        cutoff = now - timedelta(hours=self.EXPORT_TMP_MIN_AGE_HOURS)
        results: list[CleanupCandidate] = []
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink() or self._mtime(path) >= cutoff:
                continue
            name = path.name.casefold()
            if not (
                (name.startswith(".") and name.endswith(".tmp"))
                or name.endswith(".failed")
            ):
                continue
            candidate = self._maybe_candidate(
                path, "citation_export_temporary", "stale_citation_export_artifact"
            )
            if candidate:
                results.append(candidate)
        return results

    def _snapshot_generation_candidates(self) -> list[CleanupCandidate]:
        directory = self.settings.state_dir / "snapshot_generations"
        marker = self.settings.state_dir / "snapshot_commit.json"
        if not directory.is_dir() or not marker.is_file():
            return []
        try:
            active = str(json.loads(marker.read_text(encoding="utf-8")).get("generation_id") or "")
        except Exception:
            return []
        generations = sorted(
            (path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()),
            key=lambda path: path.name,
            reverse=True,
        )
        keep = {active}
        previous = next((path.name for path in generations if path.name != active), None)
        if previous:
            keep.add(previous)
        results = []
        for path in generations:
            if path.name in keep:
                continue
            candidate = self._maybe_candidate(
                path, "snapshot_generation", "older_snapshot_generation"
            )
            if candidate:
                results.append(candidate)
        return results

    def _citation_export_cache_candidates(
        self, now: datetime
    ) -> list[CleanupCandidate]:
        directory = self.settings.citation_export_cache_dir
        if not directory.is_dir():
            return []
        results: list[CleanupCandidate] = []
        for path in directory.rglob("*.json"):
            if path.is_symlink():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                expires = self._parse_datetime(payload.get("expires_at"))
            except Exception:
                continue
            if expires is None or expires > now:
                continue
            candidate = self._maybe_candidate(
                path, "citation_export_cache", "citation_export_cache_expired"
            )
            if candidate:
                results.append(candidate)
            raw_name = str(payload.get("raw_response") or "")
            if raw_name and Path(raw_name).name == raw_name:
                raw_path = path.with_name(raw_name)
                if raw_path.is_file() and not raw_path.is_symlink():
                    candidate = self._maybe_candidate(
                        raw_path,
                        "citation_export_cache",
                        "citation_export_cache_expired",
                    )
                    if candidate:
                        results.append(candidate)
        return results

    def _candidate(self, path: Path, kind: str, reason: str) -> CleanupCandidate:
        size, count = self._tree_stats(path)
        return CleanupCandidate(path.resolve(), kind, reason, size, count)

    def _maybe_candidate(
        self, path: Path, kind: str, reason: str
    ) -> CleanupCandidate | None:
        try:
            return self._candidate(path, kind, reason)
        except (OSError, FileOperationError):
            return None

    def _tree_stats(self, path: Path) -> tuple[int, int]:
        self._assert_safe_content(path)
        if path.is_file():
            return path.stat().st_size, 1
        size = 0
        count = 0
        for child in path.rglob("*"):
            if child.is_file():
                size += child.stat().st_size
                count += 1
        return size, count

    def _delete_candidate(self, candidate: CleanupCandidate) -> None:
        self._assert_allowed_candidate(candidate)
        path = candidate.path
        if not path.exists():
            return
        self._assert_safe_content(path)
        if path.is_file():
            path.unlink()
            return
        children = sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for child in children:
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        path.rmdir()

    def _assert_allowed_candidate(self, candidate: CleanupCandidate) -> None:
        path = candidate.path.resolve()
        try:
            path.relative_to(self.policy.topics_root.resolve())
        except ValueError:
            pass
        else:
            raise FileOperationError(f"Cleanup refuses all Topics/ content: {path}")
        allowed_roots = {
            "catalogue_backup": self.root,
            "report": self.settings.reports_dir.resolve(),
            "log": self.settings.logs_dir.resolve(),
            "completed_operation_journal": (self.settings.state_dir / "runs").resolve(),
            "temporary": (self.settings.state_dir / "tmp").resolve(),
            "metadata_cache": (self.settings.metadata_cache_dir or self.settings.state_dir / "metadata_cache").resolve(),
            "citation_export_cache": self.settings.citation_export_cache_dir.resolve(),
            "citation_export_temporary": self.settings.zotero_exports_dir.resolve(),
            "snapshot_generation": (self.settings.state_dir / "snapshot_generations").resolve(),
        }
        allowed = allowed_roots.get(candidate.kind)
        if allowed is None:
            raise FileOperationError(f"Cleanup kind is not allowlisted: {candidate.kind}")
        try:
            relative = path.relative_to(allowed)
        except ValueError as exc:
            raise FileOperationError(f"Cleanup path escapes its allowlist: {path}") from exc
        if not relative.parts:
            raise FileOperationError(f"Cleanup refuses to delete an allowlist root: {path}")
        if candidate.kind == "catalogue_backup" and (
            path.parent != self.root or not BACKUP_PATTERN.fullmatch(path.name)
        ):
            raise FileOperationError(f"Backup path is not strictly allowlisted: {path}")

    def _assert_safe_content(self, path: Path) -> None:
        items = [path] if path.is_file() else [path, *path.rglob("*")]
        for item in items:
            if item.is_symlink() or self._is_reparse_point(item):
                raise FileOperationError(f"Cleanup refuses symlinks or reparse points: {item}")
            if item.is_file():
                name = item.name.casefold()
                if item.suffix.casefold() == ".pdf" or name in PROTECTED_NAMES:
                    raise FileOperationError(f"Cleanup refuses protected content: {item}")

    @staticmethod
    def _mtime(path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

    @staticmethod
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

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            attributes = getattr(path.stat(), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & 0x400)
