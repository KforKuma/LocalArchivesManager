from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..exceptions import FileOperationError


class OperationJournal:
    def __init__(self, path: Path, payload: dict[str, Any]):
        self.path = path
        self.payload = payload

    @classmethod
    def create(cls, state_dir: Path, operations: list[dict[str, Any]]) -> "OperationJournal":
        run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f-register")
        path = state_dir / "runs" / run_id / "operation_journal.json"
        payload = {
            "run_id": run_id,
            "workflow": "inbox_register",
            "status": "planned",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "operations": operations,
        }
        journal = cls(path, payload)
        journal.write()
        return journal

    def set_operation_state(self, catalogue_row: int, state: str, **details: Any) -> None:
        for operation in self.payload["operations"]:
            if operation.get("catalogue_row") == catalogue_row:
                operation["execution_state"] = state
                operation.update(details)
        self.payload["status"] = state
        self.write()

    def finish(self, status: str = "final_check_committed") -> None:
        self.payload["status"] = status
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
                    "status": payload.get("status"),
                }
            )
    return results
