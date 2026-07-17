from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..models import WorkflowResult
from ..run_context import RunContext


SENSITIVE_ARGUMENT_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "email",
    "credential",
    "authorization",
)
SENSITIVE_ARGUMENT_NAMES = {"title"}


class InvocationService:
    def __init__(self, directory: Path):
        self.directory = directory

    def write(
        self,
        context: RunContext,
        *,
        arguments: dict[str, Any],
        result: WorkflowResult | None,
        exit_code: int,
        duration_ms: int,
        canonical_command: str | None = None,
        status: str | None = None,
        error_type: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        completed = completed_at or datetime.now().astimezone()
        started = started_at or completed
        path = self.directory / f"{completed:%Y-%m}.jsonl"
        payload = {
            "invocation_id": context.run_id,
            "lam_version": __version__,
            "command": context.top_level_command,
            "canonical_command": canonical_command or context.top_level_command,
            "sanitized_arguments": self._sanitize(arguments),
            # Backward-compatible alias retained for one release.
            "arguments": self._sanitize(arguments),
            "library_root": str(context.library_root),
            "caller": context.caller,
            "dry_run": context.dry_run,
            "workflow": result.workflow if result is not None else None,
            "status": status or (result.status.value if result is not None else "failed"),
            "exit_code": int(exit_code),
            "error_type": error_type,
            "report_path": result.report_path if result is not None else None,
            "changed_files": result.changed_files if result is not None else 0,
            "changed_rows": result.changed_rows if result is not None else 0,
            "started_at": started.isoformat(timespec="milliseconds"),
            "completed_at": completed.isoformat(timespec="milliseconds"),
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
            if lowered == "argv" and isinstance(value, list):
                sanitized[key] = cls._sanitize_argv(value)
            elif lowered in SENSITIVE_ARGUMENT_NAMES or any(
                marker in lowered for marker in SENSITIVE_ARGUMENT_MARKERS
            ):
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

    @classmethod
    def _sanitize_argv(cls, values: list[Any]) -> list[Any]:
        result = list(values)
        redact_next = False
        for index, value in enumerate(result):
            text = str(value)
            if redact_next:
                result[index] = "[REDACTED]"
                redact_next = False
                continue
            if not text.startswith("--"):
                continue
            option = text[2:].split("=", 1)[0].replace("-", "_").casefold()
            sensitive = option in SENSITIVE_ARGUMENT_NAMES or any(
                marker in option for marker in SENSITIVE_ARGUMENT_MARKERS
            )
            if not sensitive:
                continue
            if "=" in text:
                result[index] = text.split("=", 1)[0] + "=[REDACTED]"
            else:
                redact_next = True
        return result
