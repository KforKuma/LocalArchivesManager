from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.report_service import ReportService, append_change_log
from .daily_check import DailyCheckWorkflow
from .paper_delete import PaperDeleteWorkflow


class TrashRecoveryWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.library_root.resolve()
        self.trash_root = settings.state_dir / "trash"

    def list(self) -> WorkflowResult:
        result = WorkflowResult("trash_list", dry_run=True, mode="read_only")
        entries = []
        if self.trash_root.is_dir():
            for path in sorted(self.trash_root.iterdir()):
                if not path.is_dir() or path.name == "tombstones":
                    continue
                manifest = self._read_json(path / "manifest.json")
                if manifest:
                    entries.append(manifest)
        result.details["trash"] = entries
        result.counts = {"trash_entries": len(entries)}
        if not entries:
            result.skipped.append({"reason": "trash_empty"})
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    def run(self, *, deletion_id: str, dry_run: bool) -> WorkflowResult:
        result = WorkflowResult(
            "trash_recovery", dry_run=dry_run, mode="dry_run" if dry_run else "apply"
        )
        try:
            normalized_id = str(uuid.UUID(deletion_id))
        except ValueError as exc:
            raise CatalogueError(f"Invalid trash deletion id: {deletion_id!r}") from exc
        trash_dir = self.trash_root / normalized_id
        manifest = self._read_json(trash_dir / "manifest.json")
        catalogue_values = self._read_json(trash_dir / "catalogue_record.json")
        document_values = self._read_json(trash_dir / "document_records.json")
        if not manifest or manifest.get("status") != "committed":
            raise CatalogueError(f"Trash entry is not recoverable: {normalized_id}")
        if not isinstance(catalogue_values, dict) or not isinstance(document_values, list):
            raise CatalogueError(f"Trash entity metadata is incomplete: {normalized_id}")
        catalogue = CatalogueService(self.settings.catalogue_path)
        catalogue.load()
        paper_uuid = str(manifest.get("paper_uuid") or "")
        if catalogue.find_by("paper_uuid", paper_uuid):
            raise CatalogueError(f"paper_uuid already exists in Catalogue: {paper_uuid}")
        existing_paths = {
            str(item.get("relative_path") or "").replace("\\", "/").casefold()
            for item in catalogue.documents
        }
        moves: list[tuple[Path, Path]] = []
        for item in manifest.get("files", []):
            relative = str(item.get("original_relative_path") or "")
            if relative and relative.casefold() in existing_paths:
                raise CatalogueError(f"Document path already registered: {relative}")
            if item.get("missing_at_deletion"):
                continue
            source = trash_dir / str(item.get("trash_relative_path") or "")
            target = (self.root / relative).resolve()
            try:
                target.relative_to(self.root)
            except ValueError as exc:
                raise FileOperationError(f"Recovery path escapes library root: {relative}") from exc
            if not source.is_file():
                raise FileOperationError(f"Trashed file is missing: {source}")
            if target.exists():
                raise FileOperationError(f"Recovery target already exists: {target}")
            moves.append((source, target))
        result.details.update(
            {
                "deletion_id": normalized_id,
                "paper_uuid": paper_uuid,
                "files": len(moves),
                "documents": len(document_values),
            }
        )
        if dry_run:
            result.completed.append(
                {"action": "would_restore_trash_entity", **result.details}
            )
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result
        moved: list[tuple[Path, Path]] = []
        try:
            for source, target in moves:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                moved.append((source, target))
            catalogue.restore_paper_entity(catalogue_values, document_values)
            backup = catalogue.save_atomic()
        except Exception:
            PaperDeleteWorkflow._rollback_files(
                [(source, target) for source, target in moved]
            )
            raise
        manifest["status"] = "recovered"
        PaperDeleteWorkflow._write_json(trash_dir / "manifest.json", manifest)
        tombstone = self.trash_root / "tombstones" / f"{normalized_id}.json"
        if tombstone.is_file():
            payload = self._read_json(tombstone)
            payload["status"] = "recovered"
            PaperDeleteWorkflow._write_json(tombstone, payload)
        result.catalogue_backup = str(backup) if backup else None
        result.changed_files = len(moved)
        result.changed_rows = 1 + len(document_values)
        result.completed.append(
            {
                "action": "restored_trash_entity",
                "deletion_id": normalized_id,
                "paper_uuid": paper_uuid,
            }
        )
        append_change_log(
            self.settings.changes_log_path,
            workflow="Trash recovery",
            action=f"Restore paper entity from trash {normalized_id}",
            files_changed=len(moved),
            catalogue_rows_changed=1 + len(document_values),
            reason="Explicit recovery preserving paper_uuid and document_id values",
            uncertainty="None",
        )
        final = DailyCheckWorkflow(self.settings).run(final_check=True)
        result.details["final_check"] = {
            "status": final.status.value,
            "report": final.report_path,
        }
        result.state_committed = final.state_committed
        result.needs_review.extend(final.needs_review)
        result.failures.extend(final.failures)
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
