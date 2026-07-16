from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..exceptions import FileOperationError
from ..run_context import current_run_context


class OperationJournal:
    def __init__(self, path: Path, payload: dict[str, Any]):
        self.path = path
        self.payload = payload

    @classmethod
    def create(
        cls,
        state_dir: Path,
        operations: list[dict[str, Any]],
        *,
        workflow: str = "inbox_register",
        suffix: str = "register",
    ) -> "OperationJournal":
        run_id = datetime.now().astimezone().strftime(
            f"%Y%m%d-%H%M%S-%f-{suffix}"
        )
        path = state_dir / "runs" / run_id / "operation_journal.json"
        for operation in operations:
            state = operation.get("execution_state", "planned")
            operation.setdefault("stages", [state])
        payload = {
            "run_id": run_id,
            "workflow": workflow,
            "status": "planned",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "operations": operations,
        }
        context = current_run_context()
        if context is not None:
            payload["invocation_id"] = context.run_id
            payload["top_level_command"] = context.top_level_command
        journal = cls(path, payload)
        journal.write()
        return journal

    def set_operation_state(
        self,
        catalogue_row: int | None,
        state: str,
        *,
        record_uid: str | None = None,
        operation_id: str | None = None,
        document_id: str | None = None,
        **details: Any,
    ) -> None:
        """Advance selected operations while retaining the legacy row/UID API.

        ``operation_id`` has highest precedence, followed by ``document_id``.
        When either precise selector is supplied, row and record UID fallback is
        intentionally disabled so that sibling documents for the same paper are
        not advanced accidentally.
        """
        operations = self.payload["operations"]
        if operation_id is not None:
            selected_operations = [
                operation
                for operation in operations
                if operation.get("operation_id") == operation_id
            ]
            selector = f"operation_id={operation_id!r}"
        elif document_id is not None:
            selected_operations = [
                operation
                for operation in operations
                if operation.get("document_id") == document_id
            ]
            selector = f"document_id={document_id!r}"
        else:
            selected_operations = []
            for operation in operations:
                uid_matches = bool(
                    record_uid
                    and operation.get("record_uid")
                    and operation.get("record_uid") == record_uid
                )
                if uid_matches or operation.get("catalogue_row") == catalogue_row:
                    selected_operations.append(operation)
            selector = ""
        if selector and len(selected_operations) != 1:
            raise FileOperationError(
                "Operation journal selector must match exactly one operation: "
                f"{selector}; matches={len(selected_operations)}"
            )
        for operation in selected_operations:
            operation["execution_state"] = state
            stages = operation.setdefault("stages", [])
            if not stages or stages[-1] != state:
                stages.append(state)
            operation.update(details)
        self.payload["status"] = state
        self.write()

    def finish(self, status: str = "final_check_committed") -> None:
        self.payload["status"] = status
        for operation in self.payload["operations"]:
            operation["execution_state"] = status
            stages = operation.setdefault("stages", [])
            if not stages or stages[-1] != status:
                stages.append(status)
        self.payload["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(self.payload, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, self.path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise FileOperationError(f"Cannot update operation journal: {self.path}") from exc


def incomplete_journals(state_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        return results
    for path in sorted(runs_dir.glob("*/operation_journal.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            results.append({"journal": str(path), "status": "unreadable"})
            continue
        if payload.get("status") != "final_check_committed":
            results.append(
                {
                    "journal": str(path),
                    "run_id": payload.get("run_id"),
                    "workflow": payload.get("workflow"),
                    "status": payload.get("status"),
                }
            )
    return results


def completed_file_movements(
    state_dir: Path,
    library_root: Path,
) -> list[dict[str, Any]]:
    """Return committed journal moves without trusting paths outside the library."""
    results: list[dict[str, Any]] = []
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        return results
    root = library_root.resolve()
    accepted_states = {"file_moved", "catalogue_committed", "final_check_committed"}
    for path in sorted(runs_dir.glob("*/operation_journal.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") not in accepted_states:
            continue
        for operation in payload.get("operations", []):
            if operation.get("operation_type") not in {"move", "rename"}:
                continue
            stages = set(operation.get("stages", []))
            state = operation.get("execution_state")
            if "file_moved" not in stages and state not in accepted_states:
                continue
            source = _journal_relative_path(operation.get("source"), root)
            target = _journal_relative_path(operation.get("target"), root)
            if not source or not target:
                continue
            results.append(
                {
                    "source": source,
                    "target": target,
                    "run_id": payload.get("run_id"),
                    "workflow": payload.get("workflow"),
                    "status": payload.get("status"),
                }
            )
    return results


def _journal_relative_path(value: Any, root: Path) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return relative.as_posix()
