from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..models import WorkflowResult
from ..run_context import RunContext


SENSITIVE_ARGUMENT_MARKERS = ("key", "token", "secret", "password")


class InvocationService:
    def __init__(self, directory: Path):
        self.directory = directory

    def write(
        self,
        context: RunContext,
        *,
        arguments: dict[str, Any],
        result: WorkflowResult,
        exit_code: int,
        duration_ms: int,
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone()
        path = self.directory / f"{timestamp:%Y-%m}.jsonl"
        payload = {
            "invocation_id": context.run_id,
            "timestamp": timestamp.isoformat(timespec="milliseconds"),
            "lam_version": __version__,
            "command": context.top_level_command,
            "arguments": self._sanitize(arguments),
            "library_root": str(context.library_root),
            "caller": context.caller,
            "dry_run": context.dry_run,
            "workflow": result.workflow,
            "status": result.status.value,
            "exit_code": exit_code,
            "report_path": result.report_path,
            "changed_files": result.changed_files,
            "changed_rows": result.changed_rows,
            "duration_ms": max(0, int(duration_ms)),
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return path

    @classmethod
    def _sanitize(cls, arguments: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in arguments.items():
            lowered = key.casefold()
            if any(marker in lowered for marker in SENSITIVE_ARGUMENT_MARKERS):
                sanitized[key] = "[REDACTED]"
            elif key in {"json_output", "verbose"}:
                sanitized[key] = bool(value)
            elif isinstance(value, Path):
                sanitized[key] = str(value)
            elif isinstance(value, tuple):
                sanitized[key] = list(value)
            else:
                sanitized[key] = value
        return sanitized
