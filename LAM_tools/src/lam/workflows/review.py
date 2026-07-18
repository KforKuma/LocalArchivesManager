from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import MetadataLookupRequest, WorkflowResult
from ..services.catalogue_service import CatalogueService
from ..services.report_service import ReportService, append_change_log
from ..utils.normalize import normalized_relative_path, normalized_text
from .daily_check import DailyCheckWorkflow
from .metadata_query import MetadataQueryWorkflow


_ISSUE_KEY = re.compile(r"(?:^|;)\s*issue_key=([^;]+)", re.I)


class ReviewWorkflow:
    """Recheck machine blockers without manufacturing user approval."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self,
        *,
        dry_run: bool,
        all_records: bool = False,
        paper_uuid: str | None = None,
        document_id: str | None = None,
        provider: str | None = None,
        offline: bool = False,
        refresh: bool = False,
        cache_write: bool = True,
    ) -> WorkflowResult:
        result = WorkflowResult(
            "review",
            dry_run=dry_run,
            mode="dry_run" if dry_run else "apply",
        )
        catalogue = CatalogueService(self.settings.catalogue_path)
        records = catalogue.load()
        selected_papers = {
            normalized_text(record.get("paper_uuid")): record
            for record in records
            if all_records
            or (
                paper_uuid
                and normalized_text(record.get("paper_uuid"))
                == normalized_text(paper_uuid)
            )
        }
        selected_documents = [
            document
            for document in catalogue.documents
            if all_records
            or (
                document_id
                and normalized_text(document.get("document_id"))
                == normalized_text(document_id)
            )
            or normalized_text(document.get("paper_uuid")) in selected_papers
        ]
        if document_id and not selected_documents:
            result.needs_review.append(
                {"document_id": document_id, "issue": "document_not_found"}
            )
        if paper_uuid and not selected_papers:
            result.needs_review.append(
                {"paper_uuid": paper_uuid, "issue": "paper_not_found"}
            )

        active: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        today = date.today().isoformat()

        for record in selected_papers.values():
            lines = self._lines(record.get("uncertainty"))
            kept: list[str] = []
            for line in lines:
                if not line.lstrip().upper().startswith("NEEDS_REVIEW:"):
                    kept.append(line)
                    continue
                blocker = self._blocker(
                    "Catalogue", record.row_number, record.get("paper_uuid"), None, line
                )
                active.append(blocker)
                is_resolved, detail = self._catalogue_condition_resolved(
                    catalogue, record, blocker["issue_key"]
                )
                if is_resolved:
                    resolved.append({**blocker, "resolution": detail})
                else:
                    kept.append(line)
                    unresolved.append(
                        {
                            **blocker,
                            "recommendation": self._recommended_command(
                                blocker["issue_key"], record.get("paper_uuid")
                            ),
                        }
                    )
            if not dry_run and kept != lines:
                catalogue.update_fields(record, {"uncertainty": "\n".join(kept)})

        for document in selected_documents:
            lines = self._lines(document.get("uncertainty"))
            kept: list[str] = []
            for line in lines:
                if not self._is_document_machine_blocker(line):
                    kept.append(line)
                    continue
                blocker = self._blocker(
                    "Documents",
                    document.row_number,
                    document.get("paper_uuid"),
                    document.get("document_id"),
                    line,
                )
                active.append(blocker)
                is_resolved, updates, detail = self._document_condition_resolved(
                    catalogue, document, blocker["issue_key"]
                )
                if is_resolved:
                    resolved.append({**blocker, "resolution": detail})
                    if not dry_run and updates:
                        updates.setdefault("date_updated", today)
                        catalogue.update_document_fields(document, updates)
                else:
                    kept.append(line)
                    unresolved.append(
                        {
                            **blocker,
                            "recommendation": self._recommended_command(
                                blocker["issue_key"], document.get("paper_uuid")
                            ),
                        }
                    )
            if not dry_run and kept != lines:
                catalogue.update_document_fields(
                    document, {"uncertainty": "\n".join(kept)}
                )

        stale_inbox = self._review_inbox_blockers(
            selected_papers=set(selected_papers),
            all_records=all_records,
            dry_run=dry_run,
        )
        resolved.extend(stale_inbox)
        if not dry_run and stale_inbox:
            result.changed_files += 1

        result.details = {
            "active_blockers": active,
            "resolved_blockers": resolved,
            "unresolved_blockers": unresolved,
            "provider_policy": {
                "enabled": provider is not None,
                "provider": provider or "none",
                "offline": offline,
                "refresh": refresh,
                "cache_write": cache_write,
            },
            "writes_user_confirmed": False,
            "modifies_topic_folder": False,
        }
        result.counts = {
            "active_blockers": len(active),
            "resolved_blockers": len(resolved),
            "unresolved_blockers": len(unresolved),
        }

        if dry_run:
            result.completed.extend(
                {"action": "would_resolve_machine_blocker", **item}
                for item in resolved
            )
        else:
            backup = catalogue.save_atomic()
            if backup:
                result.catalogue_backup = str(backup)
            result.changed_rows = len(
                {change.row_number for change in catalogue.changes}
                | {change.row_number for change in catalogue.document_changes}
            )
            result.completed.extend(
                {"action": "resolved_machine_blocker", **item} for item in resolved
            )
            if result.changed_rows or stale_inbox:
                append_change_log(
                    self.settings.changes_log_path,
                    workflow="Review",
                    action="Recheck and clear objectively resolved machine blockers",
                    files_changed=result.changed_files,
                    catalogue_rows_changed=result.changed_rows,
                    reason="The recorded objective blocker condition no longer exists",
                    uncertainty=f"{len(unresolved)} blocker(s) remain",
                )

        provider_results: list[dict[str, Any]] = []
        if provider is not None:
            targets = sorted(
                {
                    str(item.get("paper_uuid") or "")
                    for item in unresolved
                    if item.get("sheet") == "Catalogue"
                    and item.get("paper_uuid")
                    and "identity" in normalized_text(item.get("issue_key"))
                }
            )
            for target_uuid in targets:
                nested = MetadataQueryWorkflow(self.settings).run(
                    MetadataLookupRequest(
                        paper_uuid=target_uuid,
                        provider=provider,
                        offline=offline,
                        refresh=refresh,
                        cache_write=cache_write,
                    ),
                    dry_run=dry_run,
                    paper_uuid=target_uuid,
                    normalize_existing=True,
                    max_records=1,
                )
                provider_results.append(
                    {
                        "paper_uuid": target_uuid,
                        "status": nested.status.value,
                        "report": nested.report_path,
                    }
                )
            result.details["provider_retries"] = provider_results

        if not dry_run:
            final = DailyCheckWorkflow(self.settings).run(final_check=True)
            result.details["final_check"] = {
                "status": final.status.value,
                "report": final.report_path,
            }
            result.state_committed = final.state_committed
            result.needs_review.extend(
                item for item in final.needs_review if item not in result.needs_review
            )

        result.needs_review.extend(unresolved)
        result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    @staticmethod
    def _lines(value: object) -> list[str]:
        return [line.rstrip() for line in str(value or "").splitlines() if line.strip()]

    @staticmethod
    def _blocker(
        sheet: str,
        row: int,
        paper_uuid: object,
        document_id: object,
        line: str,
    ) -> dict[str, Any]:
        match = _ISSUE_KEY.search(line)
        issue_key = match.group(1).strip() if match else line.strip()
        return {
            "sheet": sheet,
            "row": row,
            "paper_uuid": str(paper_uuid or "") or None,
            "document_id": str(document_id or "") or None,
            "issue_key": issue_key,
            "entry": line,
        }

    def _catalogue_condition_resolved(self, catalogue, record, issue_key: str):
        key = normalized_text(issue_key)
        if "topic_folder" in key:
            topic = str(record.get("topic_folder") or "").strip()
            documents = catalogue.documents_for_paper(record.get("paper_uuid"))
            if topic and documents and all(
                normalized_relative_path(document.get("relative_path")).startswith(
                    normalized_relative_path(f"Topics/{topic}/")
                )
                for document in documents
            ):
                return True, "all Documents now match topic_folder"
        return False, "condition still requires review"

    def _document_condition_resolved(self, catalogue, document, issue_key: str):
        key = normalized_text(issue_key)
        relative = str(document.get("relative_path") or "").strip().replace("\\", "/")
        source = self.settings.library_root / relative if relative else None
        if key in {"document_file_missing", "source_missing"}:
            if source and source.is_file():
                return True, {"file_status": self._status_for(relative)}, "file exists again"
            filename = str(document.get("filename") or "").strip()
            candidates = self._managed_filename_candidates(filename)
            if len(candidates) == 1:
                new_relative = candidates[0].relative_to(self.settings.library_root).as_posix()
                return (
                    True,
                    {
                        "filename": candidates[0].name,
                        "relative_path": new_relative,
                        "file_status": self._status_for(new_relative),
                    },
                    "file was found at one unique managed path",
                )
        if key == "topic_location_mismatch":
            paper = next(
                (
                    item
                    for item in catalogue.records
                    if normalized_text(item.get("paper_uuid"))
                    == normalized_text(document.get("paper_uuid"))
                ),
                None,
            )
            topic = str(paper.get("topic_folder") or "").strip() if paper else ""
            if topic and normalized_relative_path(relative).startswith(
                normalized_relative_path(f"Topics/{topic}/")
            ):
                return True, {"file_status": "filed"}, "path now matches topic_folder"
        if key in {"document_target_collision", "target_collision"}:
            paper = next(
                (
                    item
                    for item in catalogue.records
                    if normalized_text(item.get("paper_uuid"))
                    == normalized_text(document.get("paper_uuid"))
                ),
                None,
            )
            topic = str(paper.get("topic_folder") or "").strip() if paper else ""
            filename = str(document.get("filename") or "").strip()
            if topic and filename:
                target = self.settings.topics_dir / topic / filename
                if not target.exists():
                    return True, {}, "previous target collision no longer exists"
        return False, {}, "condition still exists or cannot be proven resolved"

    @staticmethod
    def _is_document_machine_blocker(line: str) -> bool:
        normalized = normalized_text(line)
        if normalized.startswith("needs_review:"):
            return True
        return normalized in {
            "document_file_missing",
            "source_missing",
            "topic_location_mismatch",
            "document_target_collision",
            "target_collision",
            "supplementary_target_collision",
        }

    def _managed_filename_candidates(self, filename: str) -> list[Path]:
        if not filename:
            return []
        candidates: list[Path] = []
        for directory in (self.settings.inbox_dir, self.settings.registered_dir):
            candidate = directory / filename
            if candidate.is_file():
                candidates.append(candidate)
        if self.settings.topics_dir.is_dir():
            candidates.extend(
                path
                for path in self.settings.topics_dir.rglob(filename)
                if path.is_file() and not any(part.startswith(".") for part in path.parts)
            )
        return candidates

    @staticmethod
    def _status_for(relative: str) -> str:
        top = relative.replace("\\", "/").split("/", 1)[0].casefold()
        return {"inbox": "inbox", "registered": "registered", "topics": "filed"}.get(
            top, "unclear"
        )

    def _review_inbox_blockers(
        self,
        *,
        selected_papers: set[str],
        all_records: bool,
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        path = self.settings.state_dir / "inbox_blockers.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        kept = []
        resolved = []
        for item in payload.get("files", []):
            item_uuid = normalized_text(item.get("paper_uuid"))
            if not all_records and item_uuid not in selected_papers:
                kept.append(item)
                continue
            source = self.settings.library_root / str(item.get("source_path") or "")
            if source.is_file():
                kept.append(item)
                continue
            resolved.append(
                {
                    "sheet": "Inbox blocker state",
                    "paper_uuid": item.get("paper_uuid"),
                    "source_path": item.get("source_path"),
                    "issue_key": ",".join(item.get("issue_keys") or []),
                    "resolution": "blocked Inbox file no longer exists",
                }
            )
        if not dry_run and kept != payload.get("files", []):
            if kept:
                temporary = path.with_name(f".{path.name}.tmp")
                temporary.write_text(
                    json.dumps({**payload, "files": kept}, ensure_ascii=False, indent=2)
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                os.replace(temporary, path)
            else:
                path.unlink()
        return resolved

    @staticmethod
    def _recommended_command(issue_key: str, paper_uuid: object) -> str:
        key = normalized_text(issue_key)
        if "identity" in key or "metadata" in key:
            return f"lam search --paper-uuid {paper_uuid} --normalize-existing"
        if "publication_type" in key:
            return "lam recover --scope publication-types --dry-run"
        if "topic" in key:
            return "lam file --dry-run"
        if "missing" in key or "collision" in key or "duplicate" in key:
            return "lam review --all --dry-run"
        return "lam check --dry-run"
