from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable

from ..models import CatalogueRecord, DocumentRecord
from ..utils.filename import standard_supplementary_filename_result
from ..utils.hashing import full_hash
from ..utils.normalize import normalized_relative_path, normalized_text
from ..utils.supplementary import (
    SupplementaryFilename,
    is_supported_document_extension,
    parse_same_stem_supplementary_filename,
    parse_uuid_supplementary_filename,
)


DUPLICATE_FILE = "supplementary_duplicate_file"
DOCUMENT_ID_CONFLICT = "supplementary_document_id_conflict"
SEQUENCE_CONFLICT = "supplementary_sequence_conflict"
TARGET_COLLISION = "supplementary_target_collision"
UUID_NOT_FOUND = "supplementary_uuid_not_found"
BINDING_AMBIGUOUS = "supplementary_binding_ambiguous"
PARENT_MISSING = "supplementary_parent_missing"
MAIN_DOCUMENT_MISSING = "supplementary_main_document_missing"
NAMING_METADATA_MISSING = "supplementary_naming_metadata_missing"
NAME_UNRECOGNIZED = "supplementary_name_unrecognized"

_TYPE_SLUGS = {
    "supplementary": "generic",
    "table": "table",
    "figure": "figure",
    "methods": "methods",
    "data": "data",
    "appendix": "appendix",
    "other": "other",
}


@dataclass(frozen=True, slots=True)
class SupplementaryInboxItem:
    source: Path
    relative_path: str
    parsed: SupplementaryFilename


@dataclass(frozen=True, slots=True)
class SameStemSupplementaryGroup:
    parent_stem: str
    main_pdf: Path
    main_relative_path: str
    supplementary: tuple[SupplementaryInboxItem, ...]


@dataclass(frozen=True, slots=True)
class SupplementaryOrphan:
    source: Path
    relative_path: str
    reason: str
    parsed: SupplementaryFilename | None = None


@dataclass(frozen=True, slots=True)
class SupplementaryInboxScan:
    uuid_supplementary: tuple[SupplementaryInboxItem, ...]
    same_stem_groups: tuple[SameStemSupplementaryGroup, ...]
    independent_main_pdfs: tuple[Path, ...]
    orphan: tuple[SupplementaryOrphan, ...]
    skipped: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class SupplementaryRegistrationPlan:
    source: Path
    source_relative_path: str
    paper_uuid: str
    document_id: str
    supplementary_type: str
    sequence: int | None
    extension: str
    sha256: str
    target_filename: str | None
    target_path: Path | None
    target_relative_path: str | None
    document_values: dict[str, object]
    conflicts: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.conflicts and self.target_path is not None


class SupplementaryRegistrationService:
    """Classify Inbox documents and prepare side-effect-free registration plans."""

    def __init__(
        self,
        library_root: Path,
        catalogue,
        *,
        max_filename_length: int = 180,
    ):
        self.library_root = library_root.resolve()
        self.inbox_dir = (self.library_root / "Inbox").resolve()
        self.registered_dir = (self.library_root / "Registered").resolve()
        self.catalogue = catalogue
        self.max_filename_length = max_filename_length

    def scan_inbox(self) -> SupplementaryInboxScan:
        uuid_items: list[SupplementaryInboxItem] = []
        same_stem_items: list[SupplementaryInboxItem] = []
        potential_mains: list[Path] = []
        orphan: list[SupplementaryOrphan] = []
        skipped: list[dict[str, str]] = []
        if not self.inbox_dir.is_dir():
            return SupplementaryInboxScan((), (), (), (), ())

        for source in sorted(self.inbox_dir.iterdir(), key=lambda path: path.name.casefold()):
            relative = self._relative(source)
            if source.is_dir():
                skipped.append({"file": relative, "reason": "inbox_subdirectory"})
                continue
            if source.name.startswith((".", "~")):
                skipped.append({"file": relative, "reason": "hidden_or_temporary"})
                continue
            if source.is_symlink() or self._is_reparse_point(source):
                skipped.append({"file": relative, "reason": "symlink_or_reparse_point"})
                continue
            if not is_supported_document_extension(source.suffix):
                skipped.append({"file": relative, "reason": "unsupported_extension"})
                continue

            uuid_parsed = parse_uuid_supplementary_filename(source.name)
            if uuid_parsed is not None:
                item = SupplementaryInboxItem(source, relative, uuid_parsed)
                if self._catalogue_record(uuid_parsed.paper_uuid) is None:
                    orphan.append(
                        SupplementaryOrphan(source, relative, UUID_NOT_FOUND, uuid_parsed)
                    )
                else:
                    uuid_items.append(item)
                continue

            stem_parsed = parse_same_stem_supplementary_filename(source.name)
            if stem_parsed is not None:
                same_stem_items.append(
                    SupplementaryInboxItem(source, relative, stem_parsed)
                )
                continue
            if source.suffix.casefold() == ".pdf":
                potential_mains.append(source)
            else:
                orphan.append(
                    SupplementaryOrphan(source, relative, NAME_UNRECOGNIZED)
                )

        mains_by_stem: dict[str, list[Path]] = {}
        for main in potential_mains:
            mains_by_stem.setdefault(main.stem.casefold(), []).append(main)
        grouped: dict[str, list[SupplementaryInboxItem]] = {}
        group_main: dict[str, Path] = {}
        for item in same_stem_items:
            parent_stem = str(item.parsed.parent_stem or "")
            candidates = mains_by_stem.get(parent_stem.casefold(), [])
            if not candidates:
                orphan.append(
                    SupplementaryOrphan(
                        item.source, item.relative_path, PARENT_MISSING, item.parsed
                    )
                )
                continue
            if len(candidates) != 1:
                orphan.append(
                    SupplementaryOrphan(
                        item.source, item.relative_path, BINDING_AMBIGUOUS, item.parsed
                    )
                )
                continue
            key = parent_stem.casefold()
            grouped.setdefault(key, []).append(item)
            group_main[key] = candidates[0]

        groups: list[SameStemSupplementaryGroup] = []
        claimed_mains: set[Path] = set()
        for key, items in grouped.items():
            main = group_main[key]
            claimed_mains.add(main)
            ordered = tuple(sorted(items, key=self._item_sort_key))
            groups.append(
                SameStemSupplementaryGroup(
                    parent_stem=str(ordered[0].parsed.parent_stem or main.stem),
                    main_pdf=main,
                    main_relative_path=self._relative(main),
                    supplementary=ordered,
                )
            )

        return SupplementaryInboxScan(
            uuid_supplementary=tuple(sorted(uuid_items, key=self._item_sort_key)),
            same_stem_groups=tuple(
                sorted(groups, key=lambda group: group.main_relative_path.casefold())
            ),
            independent_main_pdfs=tuple(
                sorted(
                    (path for path in potential_mains if path not in claimed_mains),
                    key=lambda path: path.name.casefold(),
                )
            ),
            orphan=tuple(sorted(orphan, key=lambda item: item.relative_path.casefold())),
            skipped=tuple(skipped),
        )

    def plan_known_uuid_supplementaries(
        self, scan: SupplementaryInboxScan
    ) -> tuple[SupplementaryRegistrationPlan, ...]:
        bindings: list[tuple[SupplementaryInboxItem, CatalogueRecord, bool]] = []
        for item in scan.uuid_supplementary:
            record = self._catalogue_record(item.parsed.paper_uuid)
            if record is not None:
                bindings.append((item, record, False))
        return self._plan_bindings(bindings)

    def plan_known_uuid_supplementary(
        self, scan: SupplementaryInboxScan
    ) -> tuple[SupplementaryRegistrationPlan, ...]:
        """Backward-compatible singular spelling for the batch planner."""
        return self.plan_known_uuid_supplementaries(scan)

    def plan_same_stem_group(
        self,
        group: SameStemSupplementaryGroup,
        paper: CatalogueRecord,
        *,
        main_document_expected: bool = True,
    ) -> tuple[SupplementaryRegistrationPlan, ...]:
        return self._plan_bindings(
            (item, paper, main_document_expected) for item in group.supplementary
        )

    def plan_item(
        self,
        item: SupplementaryInboxItem,
        paper: CatalogueRecord,
        *,
        main_document_expected: bool = False,
    ) -> SupplementaryRegistrationPlan:
        return self._plan_bindings(((item, paper, main_document_expected),))[0]

    def _plan_bindings(
        self,
        bindings: Iterable[tuple[SupplementaryInboxItem, CatalogueRecord, bool]],
    ) -> tuple[SupplementaryRegistrationPlan, ...]:
        plans = [
            self._base_plan(item, paper, main_document_expected=main_expected)
            for item, paper, main_expected in bindings
        ]
        sha_counts = Counter(plan.sha256.casefold() for plan in plans if plan.sha256)
        id_counts = Counter(normalized_text(plan.document_id) for plan in plans)
        slot_counts = Counter(self._plan_slot(plan) for plan in plans)
        target_counts = Counter(
            normalized_relative_path(plan.target_relative_path)
            for plan in plans
            if plan.target_relative_path
        )
        resolved: list[SupplementaryRegistrationPlan] = []
        for plan in plans:
            conflicts = list(plan.conflicts)
            if sha_counts[plan.sha256.casefold()] > 1:
                conflicts.append(DUPLICATE_FILE)
            if id_counts[normalized_text(plan.document_id)] > 1:
                conflicts.append(DOCUMENT_ID_CONFLICT)
            if slot_counts[self._plan_slot(plan)] > 1:
                conflicts.append(SEQUENCE_CONFLICT)
            if (
                plan.target_relative_path
                and target_counts[normalized_relative_path(plan.target_relative_path)] > 1
            ):
                conflicts.append(TARGET_COLLISION)
            resolved.append(replace(plan, conflicts=tuple(dict.fromkeys(conflicts))))
        return tuple(resolved)

    def _base_plan(
        self,
        item: SupplementaryInboxItem,
        paper: CatalogueRecord,
        *,
        main_document_expected: bool,
    ) -> SupplementaryRegistrationPlan:
        paper_uuid = str(paper.get("paper_uuid") or "").strip()
        parsed_uuid = item.parsed.paper_uuid
        conflicts: list[str] = []
        if not paper_uuid or self._catalogue_record(paper_uuid) is None:
            conflicts.append(UUID_NOT_FOUND)
        if parsed_uuid and normalized_text(parsed_uuid) != normalized_text(paper_uuid):
            conflicts.append(BINDING_AMBIGUOUS)

        sequence = item.parsed.sequence
        supplementary_type = item.parsed.supplementary_type
        slug = _TYPE_SLUGS.get(supplementary_type.casefold(), "generic")
        document_sequence = sequence if sequence is not None else 1
        document_id = f"{paper_uuid}:supp:{slug}:{document_sequence:02d}"
        digest = full_hash(item.source)
        filename_result = standard_supplementary_filename_result(
            title=paper.get("title"),
            year=paper.get("year"),
            journal_abbrev=paper.get("journal_abbrev"),
            journal=paper.get("journal"),
            publication_type=paper.get("publication_type"),
            supplementary_type=supplementary_type,
            sequence=sequence,
            extension=item.parsed.extension,
            max_length=self.max_filename_length,
        )
        target_filename = filename_result.filename
        target_path = self.registered_dir / target_filename if target_filename else None
        target_relative = self._relative(target_path) if target_path else None
        if target_filename is None:
            conflicts.append(NAMING_METADATA_MISSING)

        documents: list[DocumentRecord] = list(self.catalogue.documents)
        if not main_document_expected and not any(
            normalized_text(document.get("paper_uuid")) == normalized_text(paper_uuid)
            and normalized_text(document.get("document_type")) == "main"
            for document in documents
        ):
            conflicts.append(MAIN_DOCUMENT_MISSING)
        if any(
            normalized_text(document.get("sha256")) == normalized_text(digest)
            for document in documents
        ):
            conflicts.append(DUPLICATE_FILE)
        if any(
            normalized_text(document.get("document_id")) == normalized_text(document_id)
            for document in documents
        ):
            conflicts.append(DOCUMENT_ID_CONFLICT)
        if any(
            normalized_text(document.get("document_type")) == "supplementary"
            and self._document_slot(document)
            == (
                normalized_text(paper_uuid),
                normalized_text(supplementary_type),
                normalized_text(sequence),
            )
            for document in documents
        ):
            conflicts.append(SEQUENCE_CONFLICT)
        if target_relative and any(
            normalized_relative_path(document.get("relative_path"))
            == normalized_relative_path(target_relative)
            for document in documents
        ):
            conflicts.append(TARGET_COLLISION)
        if target_path is not None and target_path.exists():
            conflicts.append(TARGET_COLLISION)

        today = date.today().isoformat()
        document_values: dict[str, object] = {
            "document_id": document_id,
            "paper_uuid": paper_uuid,
            "document_type": "supplementary",
            "supplementary_type": supplementary_type,
            "sequence": sequence if sequence is not None else "",
            "filename": target_filename or "",
            "relative_path": target_relative or "",
            "extension": item.parsed.extension,
            "sha256": digest,
            "file_status": "registered",
            "source": "local_file",
            "uncertainty": "",
            "date_added": today,
            "date_updated": today,
        }
        return SupplementaryRegistrationPlan(
            source=item.source,
            source_relative_path=item.relative_path,
            paper_uuid=paper_uuid,
            document_id=document_id,
            supplementary_type=supplementary_type,
            sequence=sequence,
            extension=item.parsed.extension,
            sha256=digest,
            target_filename=target_filename,
            target_path=target_path,
            target_relative_path=target_relative,
            document_values=document_values,
            conflicts=tuple(dict.fromkeys(conflicts)),
        )

    def _catalogue_record(self, paper_uuid: object) -> CatalogueRecord | None:
        matches = self.catalogue.find_by("paper_uuid", paper_uuid)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _document_slot(document: DocumentRecord) -> tuple[str, str, str]:
        return (
            normalized_text(document.get("paper_uuid")),
            normalized_text(document.get("supplementary_type") or "Supplementary"),
            normalized_text(document.get("sequence")),
        )

    @staticmethod
    def _plan_slot(plan: SupplementaryRegistrationPlan) -> tuple[str, str, str]:
        return (
            normalized_text(plan.paper_uuid),
            normalized_text(plan.supplementary_type),
            normalized_text(plan.sequence),
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.library_root).as_posix()

    @staticmethod
    def _item_sort_key(item: SupplementaryInboxItem) -> tuple[object, ...]:
        return (
            str(item.parsed.parent_stem or item.parsed.paper_uuid or "").casefold(),
            item.parsed.supplementary_type.casefold(),
            item.parsed.sequence is None,
            item.parsed.sequence or 0,
            item.source.name.casefold(),
        )

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)
