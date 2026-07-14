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


class MatchStatus(StrEnum):
    EXACT = "exact"
    HIGH_CONFIDENCE = "high_confidence"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    BLOCKED = "blocked"


class MetadataLookupStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


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
class IdentifierCandidate:
    value: str
    page: int | None = None
    line_or_context: str = ""
    confidence: str = "medium"
    source_type: str = "text"


@dataclass(slots=True)
class TitleCandidate:
    value: str
    confidence: str
    source_type: str
    page: int | None = None


@dataclass(slots=True)
class PdfInspection:
    relative_path: str
    filename: str
    size: int
    mtime_ns: int
    is_readable: bool = False
    is_encrypted: bool = False
    page_count: int = 0
    metadata_title: str = ""
    metadata_author: str = ""
    metadata_subject: str = ""
    metadata_creator: str = ""
    first_page_text: str = ""
    second_page_text: str = ""
    sampled_text: str = ""
    doi_candidates: list[IdentifierCandidate] = field(default_factory=list)
    pmid_candidates: list[IdentifierCandidate] = field(default_factory=list)
    title_candidates: list[TitleCandidate] = field(default_factory=list)
    year_candidates: list[str] = field(default_factory=list)
    journal_candidates: list[str] = field(default_factory=list)
    is_probable_supplement: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cache_hit: bool = False

    def report_summary(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "filename": self.filename,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "is_readable": self.is_readable,
            "is_encrypted": self.is_encrypted,
            "page_count": self.page_count,
            "doi_candidates": [item.value for item in self.doi_candidates],
            "pmid_candidates": [item.value for item in self.pmid_candidates],
            "title_candidates": [item.value for item in self.title_candidates[:5]],
            "year_candidates": self.year_candidates,
            "is_probable_supplement": self.is_probable_supplement,
            "warnings": self.warnings,
            "errors": self.errors,
            "cache_hit": self.cache_hit,
        }


@dataclass(slots=True)
class MatchResult:
    status: MatchStatus
    matched_row_id: int | None = None
    matched_catalogue_id: str | None = None
    confidence: str = "insufficient"
    method: str = "none"
    candidate_rows: list[int] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    requires_metadata_lookup: bool = False
    issue_key: str | None = None


@dataclass(slots=True)
class MetadataLookupRequest:
    doi: str | None = None
    pmid: str | None = None
    title: str | None = None
    authors: str | None = None
    year: str | None = None
    journal: str | None = None
    source_pdf: str | None = None


@dataclass(slots=True)
class MetadataLookupResult:
    status: MetadataLookupStatus
    records: list[dict[str, Any]] = field(default_factory=list)
    best_record: dict[str, Any] | None = None
    confidence: str = "insufficient"
    providers_used: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DocumentRecord:
    document_type: str
    parent_catalogue_id: str | None = None


@dataclass(slots=True)
class FileOperation:
    operation_type: OperationType
    source: Path | None
    target: Path
    catalogue_row: int
    reason: str
    expected_size: int | None = None
    expected_mtime_ns: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_type": self.operation_type.value,
            "source": str(self.source) if self.source else None,
            "target": str(self.target),
            "catalogue_row": self.catalogue_row,
            "reason": self.reason,
            "expected_size": self.expected_size,
            "expected_mtime_ns": self.expected_mtime_ns,
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
