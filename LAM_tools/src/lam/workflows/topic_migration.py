from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..directory_policy import DirectoryPolicy, RootDirectoryKind
from ..exceptions import CatalogueError, FileOperationError
from ..models import WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.file_service import FileService
from ..services.journal_service import OperationJournal
from ..services.report_service import ReportService, append_change_log
from ..services.snapshot_service import SnapshotService
from ..utils.hashing import quick_hash
from .daily_check import DailyCheckWorkflow


@dataclass(slots=True)
class TopicDirectoryMove:
    name: str
    source: Path
    target: Path
    signature: tuple[tuple[str, str, int, int], ...]
    row_numbers: tuple[int, ...]
    pdf_moves: tuple[dict[str, Any], ...]


class TopicMigrationWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.policy = DirectoryPolicy(
            settings.library_root, settings.reserved_root_directories
        )

    def run(
        self,
        *,
        dry_run: bool,
        include_topics: tuple[str, ...] = (),
    ) -> WorkflowResult:
        result = WorkflowResult(
            "topic_migration",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        files = FileService(
            self.settings.library_root,
            self.settings.max_filename_length,
            self.settings.reserved_root_directories,
        )
        references = self._legacy_references(catalogue, records)
        explicit = self._validate_explicit(include_topics)
        moves: list[TopicDirectoryMove] = []
        recover_roots: set[str] = set()

        root_directories = {
            path.name: path
            for path in self.settings.library_root.iterdir()
            if path.is_dir()
        }
        self._add_registered_pdf_references(
            catalogue, records, root_directories, references
        )
        referenced_roots = set(references) | explicit
        for name, path in sorted(root_directories.items(), key=lambda item: item[0].casefold()):
            kind = self.policy.classify_root_directory(
                path,
                referenced_legacy_roots=referenced_roots,
                explicit_legacy_roots=explicit,
            )
            if kind == RootDirectoryKind.UNKNOWN:
                result.skipped.append(
                    {"directory": name, "reason": "unmanaged_unknown_directory"}
                )
                continue
            if kind != RootDirectoryKind.LEGACY_TOPIC_CANDIDATE:
                continue
            target = self.policy.topics_root / name
            try:
                signature = files.plan_topic_directory_move(path, target)
            except FileOperationError as exc:
                result.needs_review.append(
                    {
                        "directory": name,
                        "issue": "topic_migration_collision",
                        "detail": str(exc),
                    }
                )
                continue
            row_numbers = tuple(
                sorted(record.row_number for record in references.get(name, []))
            )
            pdf_moves = tuple(
                self._pdf_movements(catalogue, path, target, references.get(name, []))
            )
            moves.append(
                TopicDirectoryMove(
                    name,
                    path,
                    target,
                    signature,
                    row_numbers,
                    pdf_moves,
                )
            )

        for name in sorted(referenced_roots, key=str.casefold):
            if name in root_directories:
                continue
            if (self.policy.topics_root / name).is_dir():
                recover_roots.add(name)

        ready_roots = {move.name for move in moves} | recover_roots
        planned_updates = self._plan_catalogue_updates(
            catalogue, records, ready_roots, moves
        )
        result.details = {
            "planned_topic_directories": [move.name for move in moves],
            "recovery_roots": sorted(recover_roots, key=str.casefold),
            "planned_catalogue_updates": planned_updates,
            "unmanaged_items": [
                item for item in result.skipped if item.get("reason") == "unmanaged_unknown_directory"
            ],
        }
        result.counts = {
            "topic_directories": len(moves),
            "catalogue_updates": len(planned_updates),
            "recovery_roots": len(recover_roots),
            "unmanaged_directories": len(result.details["unmanaged_items"]),
        }

        if dry_run:
            result.completed.extend(
                {
                    "action": "would_migrate_topic",
                    "directory": move.name,
                    "source": move.source.relative_to(self.settings.library_root).as_posix(),
                    "target": move.target.relative_to(self.settings.library_root).as_posix(),
                    "registered_pdfs": len(move.pdf_moves),
                }
                for move in moves
            )
            result.completed.extend(
                {"action": "would_recover_catalogue", "directory": name}
                for name in sorted(recover_roots, key=str.casefold)
            )
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        if not moves and not planned_updates:
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        journal = self._create_journal(moves, recover_roots, planned_updates)
        moved: list[TopicDirectoryMove] = []
        try:
            for move in moves:
                self._verify_pdf_fingerprints(move)
                files.apply_topic_directory_move(
                    move.source, move.target, move.signature
                )
                moved.append(move)
                for row_number in move.row_numbers or (-1,):
                    journal.set_operation_state(row_number, "file_moved")
                result.completed.append(
                    {
                        "action": "migrated_topic",
                        "directory": move.name,
                        "source": move.source.name,
                        "target": move.target.relative_to(
                            self.settings.library_root
                        ).as_posix(),
                    }
                )
            self._apply_catalogue_updates(catalogue, records, ready_roots, moves)
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            if catalogue.maintenance_actions:
                result.details["backup_maintenance"] = list(
                    catalogue.maintenance_actions
                )
        except (CatalogueError, FileOperationError):
            for move in reversed(moved):
                files.rollback_topic_directory_move(move.source, move.target)
            raise

        for move in moves:
            for row_number in move.row_numbers or (-1,):
                journal.set_operation_state(row_number, "catalogue_committed")
        if not moves and planned_updates:
            journal.set_operation_state(-1, "catalogue_committed")

        result.changed_files = sum(len(move.pdf_moves) for move in moved)
        result.changed_rows = len(
            {("Catalogue", change.row_number) for change in catalogue.changes}
            | {("Documents", change.row_number) for change in catalogue.document_changes}
        )
        if moved or catalogue.changes or catalogue.document_changes:
            append_change_log(
                self.settings.changes_log_path,
                workflow="Topic migration",
                action="Move legacy root topic directories into Topics/",
                files_changed=result.changed_files,
                catalogue_rows_changed=result.changed_rows,
                reason="Upgrade the library to the 0.5.0 Topics namespace",
                uncertainty=f"{len(result.needs_review)} item(s) need review",
            )

        final_check = DailyCheckWorkflow(self.settings).run(
            dry_run=False, final_check=True
        )
        result.details["final_check"] = {
            "status": final_check.status.value,
            "report": final_check.report_path,
        }
        result.state_committed = final_check.state_committed
        result.needs_review.extend(
            item for item in final_check.needs_review if item not in result.needs_review
        )
        result.failures.extend(
            item for item in final_check.failures if item not in result.failures
        )
        journal.finish("final_check_committed")
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def _verify_pdf_fingerprints(self, move: TopicDirectoryMove) -> None:
        for item in move.pdf_moves:
            path = Path(str(item["source"]))
            if not path.is_file():
                raise FileOperationError(f"Registered migration source disappeared: {path}")
            stat = path.stat()
            if (
                stat.st_size != item["expected_size"]
                or stat.st_mtime_ns != item["expected_mtime_ns"]
                or quick_hash(path) != item["expected_quick_hash"]
            ):
                raise FileOperationError(
                    f"Registered migration source changed after planning: {path}"
                )

    def _legacy_references(
        self, catalogue: CatalogueService, records: list[Any]
    ) -> dict[str, list[Any]]:
        references: dict[str, list[Any]] = {}
        for record in records:
            for document in catalogue.documents_for_paper(record.get("paper_uuid")):
                relative = str(document.get("relative_path") or "").strip().replace("\\", "/")
                if relative and not relative.casefold().startswith(
                    ("inbox/", "registered/", "topics/")
                ):
                    root = relative.split("/", 1)[0]
                    if self.policy.classify_root_directory(root) == RootDirectoryKind.UNKNOWN:
                        references.setdefault(root, []).append(record)
            topic = str(record.get("topic_folder") or "").strip().replace("\\", "/")
            if topic.casefold().startswith("topics/"):
                topic = topic.split("/", 1)[1]
            if topic:
                root = topic.split("/", 1)[0]
                if self.policy.classify_root_directory(root) == RootDirectoryKind.UNKNOWN:
                    references.setdefault(root, [])
                    if record not in references[root]:
                        references[root].append(record)
        return references

    def _add_registered_pdf_references(
        self,
        catalogue: CatalogueService,
        records: list[Any],
        root_directories: dict[str, Path],
        references: dict[str, list[Any]],
    ) -> None:
        filenames: dict[str, list[Any]] = {}
        for record in records:
            for document in catalogue.documents_for_paper(record.get("paper_uuid")):
                filename = str(document.get("filename") or "").strip()
                if filename:
                    filenames.setdefault(filename.casefold(), []).append(record)
        for name, directory in root_directories.items():
            if self.policy.classify_root_directory(name) != RootDirectoryKind.UNKNOWN:
                continue
            matched: list[Any] = []
            for path in directory.rglob("*.pdf"):
                if any(part.startswith(".") for part in path.relative_to(directory).parts):
                    continue
                matched.extend(filenames.get(path.name.casefold(), []))
            if matched:
                references.setdefault(name, [])
                for record in matched:
                    if record not in references[name]:
                        references[name].append(record)

    def _validate_explicit(self, values: tuple[str, ...]) -> set[str]:
        names: set[str] = set()
        for value in values:
            normalized = self.policy.validate_topic_folder(value)
            if "/" in normalized:
                raise FileOperationError(
                    "Explicit legacy migration candidates must be root directory names"
                )
            if self.policy.classify_root_directory(normalized) != RootDirectoryKind.UNKNOWN:
                raise FileOperationError(
                    f"Reserved directory cannot be a migration candidate: {normalized}"
                )
            names.add(normalized)
        return names

    def _pdf_movements(
        self,
        catalogue: CatalogueService,
        source: Path,
        target: Path,
        records: list[Any],
    ) -> list[dict[str, Any]]:
        movements: list[dict[str, Any]] = []
        for record in records:
            for document in catalogue.documents_for_paper(record.get("paper_uuid")):
                relative = str(document.get("relative_path") or "").strip().replace("\\", "/")
                old_path: Path | None = None
                if relative and relative.split("/", 1)[0].casefold() == source.name.casefold():
                    old_path = self.settings.library_root / Path(*relative.split("/"))
                else:
                    filename = str(document.get("filename") or "").strip()
                    candidates = (
                        [path for path in source.rglob(filename) if path.is_file()]
                        if filename
                        else []
                    )
                    if len(candidates) == 1:
                        old_path = candidates[0]
                if old_path is None:
                    continue
                if not old_path.is_file():
                    continue
                suffix = old_path.relative_to(source)
                stat = old_path.stat()
                movements.append(
                    {
                        "operation_type": "move",
                        "source": str(old_path),
                        "target": str(target / suffix),
                        "catalogue_row": record.row_number,
                        "document_row": document.row_number,
                        "document_id": document.get("document_id"),
                        "reason": "migrate legacy topic namespace",
                        "expected_size": stat.st_size,
                        "expected_mtime_ns": stat.st_mtime_ns,
                        "expected_quick_hash": quick_hash(old_path),
                        "execution_state": "planned",
                    }
                )
        return movements

    def _plan_catalogue_updates(
        self,
        catalogue: CatalogueService,
        records: list[Any],
        ready_roots: set[str],
        moves: list[TopicDirectoryMove],
    ) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        discovered_paths = {
            int(item["document_row"]): Path(str(item["target"]))
            .relative_to(self.settings.library_root)
            .as_posix()
            for move in moves
            for item in move.pdf_moves
        }
        for record in records:
            topic = str(record.get("topic_folder") or "").strip().replace("\\", "/")
            if topic.casefold().startswith("topics/"):
                normalized = self.policy.normalize_legacy_topic_folder(topic)
                updates.append(
                    {"row": record.row_number, "field": "topic_folder", "new": normalized}
                )
            for document in catalogue.documents_for_paper(record.get("paper_uuid")):
                relative = str(document.get("relative_path") or "").strip().replace("\\", "/")
                if document.row_number in discovered_paths and relative != discovered_paths[document.row_number]:
                    updates.append(
                        {
                            "sheet": "Documents",
                            "row": document.row_number,
                            "field": "relative_path",
                            "new": discovered_paths[document.row_number],
                        }
                    )
                elif relative and not relative.casefold().startswith("topics/"):
                    root = relative.split("/", 1)[0]
                    if root in ready_roots:
                        updates.append(
                            {
                                "sheet": "Documents",
                                "row": document.row_number,
                                "field": "relative_path",
                                "new": f"Topics/{relative}",
                            }
                        )
        return updates

    def _apply_catalogue_updates(
        self,
        catalogue: CatalogueService,
        records: list[Any],
        ready_roots: set[str],
        moves: list[TopicDirectoryMove],
    ) -> None:
        today = date.today().isoformat()
        discovered_paths = {
            int(item["document_row"]): Path(str(item["target"]))
            .relative_to(self.settings.library_root)
            .as_posix()
            for move in moves
            for item in move.pdf_moves
        }
        for record in records:
            topic = str(record.get("topic_folder") or "").strip().replace("\\", "/")
            if topic.casefold().startswith("topics/"):
                catalogue.normalize_topic_folder_for_migration(
                    record, self.policy.normalize_legacy_topic_folder(topic)
                )
            for document in catalogue.documents_for_paper(record.get("paper_uuid")):
                relative = str(document.get("relative_path") or "").strip().replace("\\", "/")
                if document.row_number in discovered_paths:
                    catalogue.update_document_fields(
                        document,
                        {
                            "relative_path": discovered_paths[document.row_number],
                            "date_updated": today,
                        },
                    )
                elif relative and not relative.casefold().startswith("topics/"):
                    root = relative.split("/", 1)[0]
                    if root in ready_roots:
                        catalogue.update_document_fields(
                            document,
                            {
                                "relative_path": f"Topics/{relative}",
                                "date_updated": today,
                            },
                        )

    def _create_journal(
        self,
        moves: list[TopicDirectoryMove],
        recover_roots: set[str],
        planned_updates: list[dict[str, Any]],
    ) -> OperationJournal:
        operations: list[dict[str, Any]] = []
        for index, move in enumerate(moves, start=1):
            operations.append(
                {
                    "operation_type": "move_directory",
                    "source": str(move.source),
                    "target": str(move.target),
                    "catalogue_row": move.row_numbers[0] if move.row_numbers else -index,
                    "reason": "migrate legacy topic namespace",
                    "tree_signature": list(move.signature),
                    "execution_state": "planned",
                }
            )
            operations.extend(move.pdf_moves)
        if not operations:
            operations.append(
                {
                    "operation_type": "catalogue_recovery",
                    "source": None,
                    "target": str(self.policy.topics_root),
                    "catalogue_row": -1,
                    "reason": "recover catalogue after completed directory move",
                    "execution_state": "planned",
                }
            )
        journal = OperationJournal.create(
            self.settings.state_dir,
            operations,
            workflow="topic_migration",
            suffix="migrate-topics",
        )
        journal.payload["recovery_roots"] = sorted(recover_roots, key=str.casefold)
        journal.payload["planned_catalogue_updates"] = planned_updates
        journal.write()
        return journal
