from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PdfStatus(StrEnum):
    NOT_DOWNLOADED = "not_downloaded"
    INBOX = "inbox"
    REGISTERED = "registered"
    FILED = "filed"
    MISSING = "missing"
    UNCLEAR = "unclear"


class DiffType(StrEnum):
    ADDED = "added"
    MISSING = "missing"
    MODIFIED = "modified"
    MOVED_OR_RENAMED = "moved_or_renamed"
    POSSIBLE_COLLISION = "possible_collision"


class WorkflowStatus(StrEnum):
    SUCCESS = "success"
    NO_CHANGES = "no_changes"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class OperationType(StrEnum):
    MOVE = "move"
    CREATE_DIRECTORY = "create_directory"


@dataclass(slots=True)
class CatalogueRecord:
    row_number: int
    values: dict[str, Any]

    def get(self, field_name: str, default: Any = "") -> Any:
        value = self.values.get(field_name, default)
        return default if value is None else value


@dataclass(slots=True)
class FileSnapshot:
    relative_path: str
    filename: str
    size: int
    mtime_ns: int
    quick_hash: str
    full_hash: str | None = None
    last_seen: str | None = None


@dataclass(slots=True)
class FileDiff:
    diff_type: DiffType
    path: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CatalogueChange:
    row_number: int
    field_name: str
    old_value: Any
    new_value: Any


@dataclass(slots=True)
class UncertaintyEntry:
    prefix: str
    field_name: str | None
    text: str


@dataclass(slots=True)
class FileOperation:
    operation_type: OperationType
    source: Path | None
    target: Path
    catalogue_row: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type.value,
            "source": str(self.source) if self.source else None,
            "target": str(self.target),
            "catalogue_row": self.catalogue_row,
            "reason": self.reason,
        }


@dataclass(slots=True)
class WorkflowResult:
    workflow: str
    status: WorkflowStatus = WorkflowStatus.SUCCESS
    dry_run: bool = False
    mode: str | None = None
    completed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    needs_review: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    changed_files: int = 0
    changed_rows: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    report_path: str | None = None
    catalogue_backup: str | None = None
    state_committed: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def finalize_status(self) -> None:
        if self.failures:
            self.status = WorkflowStatus.FAILED
        elif self.needs_review:
            self.status = WorkflowStatus.NEEDS_REVIEW
        elif self.dry_run and self.completed:
            self.status = WorkflowStatus.SUCCESS
        elif self.changed_files == 0 and self.changed_rows == 0 and self.mode != "initial":
            self.status = WorkflowStatus.NO_CHANGES
        else:
            self.status = WorkflowStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload
