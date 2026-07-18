from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..exceptions import CatalogueError, FileOperationError
from ..models import WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.report_service import ReportService, append_change_log
from .daily_check import DailyCheckWorkflow


class PaperDeleteWorkflow:
    """Move one complete paper entity into LAM's recoverable trash."""

    MANAGED_PARENTS = {"Inbox", "Registered", "Topics"}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.library_root.resolve()
        self.trash_root = settings.state_dir / "trash"

    def run(self, *, paper_uuid: str, dry_run: bool) -> WorkflowResult:
        result = WorkflowResult(
            "paper_delete",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        target_uuid = self._uuid4(paper_uuid)
        catalogue = CatalogueService(self.settings.catalogue_path)
        catalogue.load()
        matches = catalogue.find_by("paper_uuid", target_uuid)
        if len(matches) != 1:
            raise CatalogueError(
                f"paper_uuid must identify exactly one Catalogue row: {paper_uuid!r}"
            )
        record = matches[0]
        documents = list(catalogue.documents_for_paper(target_uuid))
        files = self._file_plan(documents)
        result.details["paper"] = {
            "paper_uuid": target_uuid,
            "row": record.row_number,
            "title": record.get("title"),
        }
        result.details["documents"] = [dict(item.values) for item in documents]
        result.details["files"] = files
        result.counts = {
            "catalogue_rows": 1,
            "document_rows": len(documents),
            "managed_files": sum(not item["missing_at_deletion"] for item in files),
            "missing_files": sum(item["missing_at_deletion"] for item in files),
        }
        if dry_run:
            result.completed.append(
                {
                    "action": "would_delete_paper_entity",
                    "paper_uuid": target_uuid,
                    "document_rows": len(documents),
                    "managed_files": result.counts["managed_files"],
                }
            )
            result.finalize_status()
            ReportService(self.settings.reports_dir).write(result)
            return result

        deletion_id = str(uuid.uuid4())
        deleted_at = datetime.now().astimezone().isoformat(timespec="seconds")
        trash_dir = self.trash_root / deletion_id
        if trash_dir.exists():
            raise FileOperationError(f"Trash deletion id already exists: {deletion_id}")
        trash_dir.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "deletion_id": deletion_id,
            "paper_uuid": target_uuid,
            "deleted_at": deleted_at,
            "status": "staging",
            "catalogue_row": record.row_number,
            "document_count": len(documents),
            "files": files,
        }
        self._write_json(trash_dir / "catalogue_record.json", dict(record.values))
        self._write_json(
            trash_dir / "document_records.json",
            [dict(item.values) for item in documents],
        )
        self._write_json(trash_dir / "manifest.json", manifest)

        moved: list[tuple[Path, Path]] = []
        try:
            for item in files:
                if item["missing_at_deletion"]:
                    continue
                source = self.root / item["original_relative_path"]
                target = trash_dir / item["trash_relative_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise FileOperationError(f"Trash target already exists: {target}")
                os.replace(source, target)
                moved.append((source, target))
            manifest["status"] = "files_staged"
            self._write_json(trash_dir / "manifest.json", manifest)
            catalogue.delete_paper_entity(target_uuid)
            backup = catalogue.save_atomic()
        except Exception:
            self._rollback_files(moved)
            shutil.rmtree(trash_dir, ignore_errors=True)
            raise

        manifest["status"] = "committed"
        manifest["catalogue_backup"] = str(backup) if backup else None
        self._write_json(trash_dir / "manifest.json", manifest)
        tombstones = self.trash_root / "tombstones"
        tombstones.mkdir(parents=True, exist_ok=True)
        self._write_json(
            tombstones / f"{deletion_id}.json",
            {
                "schema_version": 1,
                "deletion_id": deletion_id,
                "paper_uuid": target_uuid,
                "deleted_at": deleted_at,
                "status": "deleted",
            },
        )
        self._append_index(manifest)
        result.details["deletion_id"] = deletion_id
        result.details["trash_path"] = str(trash_dir)
        result.catalogue_backup = str(backup) if backup else None
        result.changed_files = len(moved)
        result.changed_rows = 1 + len(documents)
        result.completed.append(
            {
                "action": "deleted_paper_entity",
                "deletion_id": deletion_id,
                "paper_uuid": target_uuid,
                "files_moved_to_trash": len(moved),
            }
        )
        append_change_log(
            self.settings.changes_log_path,
            workflow="Paper delete",
            action=f"Move paper entity {target_uuid} to LAM trash {deletion_id}",
            files_changed=len(moved),
            catalogue_rows_changed=1 + len(documents),
            reason="Explicit recoverable paper-entity deletion",
            uncertainty=(
                f"{result.counts['missing_files']} file(s) were already missing"
                if result.counts["missing_files"]
                else "None"
            ),
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

    def _file_plan(self, documents: list[Any]) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for index, document in enumerate(documents, start=1):
            relative = str(document.get("relative_path") or "").strip().replace("\\", "/")
            if not relative:
                plan.append(
                    {
                        "document_id": document.get("document_id"),
                        "original_relative_path": "",
                        "trash_relative_path": "",
                        "missing_at_deletion": True,
                        "reason": "document_path_blank",
                    }
                )
                continue
            source = (self.root / relative).resolve()
            try:
                source.relative_to(self.root)
            except ValueError as exc:
                raise FileOperationError(
                    f"Document path escapes library root: {relative}"
                ) from exc
            parts = Path(relative).parts
            if not parts or parts[0] not in self.MANAGED_PARENTS:
                raise FileOperationError(
                    f"Refusing to delete a document outside managed namespaces: {relative}"
                )
            if source in seen:
                raise FileOperationError(f"Duplicate Document path in delete plan: {relative}")
            seen.add(source)
            missing = not source.is_file()
            plan.append(
                {
                    "document_id": document.get("document_id"),
                    "original_relative_path": Path(relative).as_posix(),
                    "trash_relative_path": f"files/{index:04d}/{source.name}",
                    "missing_at_deletion": missing,
                    "reason": "file_not_found" if missing else "",
                }
            )
        return plan

    @staticmethod
    def _uuid4(value: str) -> str:
        try:
            parsed = uuid.UUID(str(value).strip())
        except ValueError as exc:
            raise CatalogueError(f"Invalid paper_uuid: {value!r}") from exc
        if parsed.version != 4:
            raise CatalogueError(f"paper_uuid must be UUID4: {value!r}")
        return str(parsed)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise FileOperationError(f"Cannot write trash metadata: {path}") from exc

    @staticmethod
    def _rollback_files(moved: list[tuple[Path, Path]]) -> None:
        failures: list[str] = []
        for source, target in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    failures.append(f"rollback target exists: {source}")
                    continue
                os.replace(target, source)
            except OSError as exc:
                failures.append(f"{target} -> {source}: {exc}")
        if failures:
            raise FileOperationError("Delete rollback failed: " + "; ".join(failures))

    def _append_index(self, manifest: dict[str, Any]) -> None:
        self.trash_root.mkdir(parents=True, exist_ok=True)
        path = self.trash_root / "index.jsonl"
        payload = {
            key: manifest.get(key)
            for key in ("deletion_id", "paper_uuid", "deleted_at", "status")
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
