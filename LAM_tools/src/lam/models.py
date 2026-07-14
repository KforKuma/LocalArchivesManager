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
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ProviderStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    UNAVAILABLE_OFFLINE = "unavailable_offline"
    DISABLED = "disabled"
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
    arxiv_id: str | None = None
    title: str | None = None
    authors: str | None = None
    year: str | None = None
    journal: str | None = None
    source_pdf: str | None = None
    provider: str = "auto"
    max_results: int = 10
    refresh: bool = False
    offline: bool = False
    cache_write: bool = True


@dataclass(slots=True)
class MetadataProvenance:
    field_name: str
    value: Any
    provider: str
    source_identifier: str = ""
    retrieved_at: str = ""
    confidence: str = "high"


@dataclass(slots=True)
class MetadataConflict:
    field_name: str
    values: dict[str, Any]
    issue_key: str
    blocking: bool = True


@dataclass(slots=True)
class DownloadCandidate:
    provider: str
    source_url: str
    landing_page_url: str = ""
    expected_doi: str = ""
    expected_arxiv_id: str = ""
    host_type: str = ""
    license: str = ""
    version: str = ""
    is_direct_pdf: bool = False
    priority: int = 100
    selection_reason: str = ""


@dataclass(slots=True)
class DownloadPlan:
    run_id: str
    candidate: DownloadCandidate
    target_filename: str
    temporary_path: Path
    final_path: Path
    max_bytes: int
    timeout_seconds: float
    target_existed_at_plan: bool = False


@dataclass(slots=True)
class DownloadedFileInspection:
    valid: bool
    reasons: list[str] = field(default_factory=list)
    page_count: int = 0
    has_pdf_signature: bool = False
    content_kind: str = "unknown"
    identity_status: str = "not_checked"
    identifiers_found: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class DownloadResult:
    status: str
    plan: DownloadPlan | None = None
    bytes_downloaded: int = 0
    content_type: str = ""
    fingerprint: str = ""
    validation: DownloadedFileInspection | None = None
    final_path: str | None = None
    error: str = ""


@dataclass(slots=True)
class MetadataRecord:
    canonical_id: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    journal: str = ""
    journal_abbrev: str = ""
    doi: str = ""
    pmid: str = ""
    arxiv_id: str = ""
    publication_type: list[str] = field(default_factory=list)
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    mesh_terms: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    language: str = ""
    published_date: str = ""
    updated_date: str = ""
    source: list[str] = field(default_factory=list)
    source_ids: dict[str, str] = field(default_factory=dict)
    is_preprint: bool = False
    is_published: bool = False
    oa_status: str = ""
    best_oa_url: str = ""
    pdf_url: str = ""
    landing_page_url: str = ""
    provenance: list[MetadataProvenance] = field(default_factory=list)
    download_candidates: list[DownloadCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MetadataRecord":
        values = dict(payload)
        values["provenance"] = [
            item if isinstance(item, MetadataProvenance) else MetadataProvenance(**item)
            for item in values.get("provenance", [])
        ]
        values["download_candidates"] = [
            item if isinstance(item, DownloadCandidate) else DownloadCandidate(**item)
            for item in values.get("download_candidates", [])
        ]
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in values.items() if key in allowed})

    def catalogue_fields(self) -> dict[str, Any]:
        publication = "; ".join(self.publication_type)
        keywords = list(dict.fromkeys([*self.keywords, *self.mesh_terms]))
        return {
            "title": self.title,
            "authors": "; ".join(self.authors),
            "year": self.year,
            "journal": self.journal,
            "journal_abbrev": self.journal_abbrev,
            "doi": self.doi,
            "pmid": self.pmid,
            "publication_type": publication,
            "abstract": self.abstract,
            "keywords": "; ".join(keywords),
            "auto_tags": "; ".join(self.categories),
            "source": "; ".join(self.source),
        }


@dataclass(slots=True)
class ProviderStats:
    request_count: int = 0
    cache_hits: int = 0
    retries: int = 0
    rate_limit_wait_seconds: float = 0.0
    records_returned: int = 0
    parse_errors: int = 0


@dataclass(slots=True)
class ProviderResult:
    provider: str
    status: ProviderStatus
    query_type: str
    query_value: str
    records: list[MetadataRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: ProviderStats = field(default_factory=ProviderStats)
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "query_type": self.query_type,
            "query_value": self.query_value,
            "records": [record.to_dict() for record in self.records],
            "errors": self.errors,
            "stats": asdict(self.stats),
            "cache_hit": self.cache_hit,
        }


@dataclass(slots=True)
class MetadataLookupResult:
    status: MetadataLookupStatus
    records: list[dict[str, Any]] = field(default_factory=list)
    best_record: dict[str, Any] | None = None
    confidence: str = "insufficient"
    providers_used: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    provider_results: list[ProviderResult] = field(default_factory=list)
    selection_reason: str = ""
    conflicts_detail: list[MetadataConflict] = field(default_factory=list)


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
