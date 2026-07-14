from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import WorkflowResult


class ReportService:
    def __init__(self, reports_dir: Path):
        self.reports_dir = reports_dir

    def write(self, result: WorkflowResult) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        stem = f"{stamp}-{result.workflow}"
        json_path = self.reports_dir / f"{stem}.json"
        md_path = self.reports_dir / f"{stem}.md"
        result.report_path = str(json_path)
        self._atomic_text(json_path, json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n")
        self._atomic_text(md_path, self._markdown(result))
        return json_path

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)

    @staticmethod
    def _markdown(result: WorkflowResult) -> str:
        title = "Daily library check" if result.workflow == "daily_check" else "Catalogue-based filing report"
        lines = [
            f"# {title}",
            "",
            f"Status: {result.status.value}",
            f"Dry run: {str(result.dry_run).lower()}",
        ]
        if result.mode:
            lines.append(f"Mode: {result.mode}")
        lines.extend(
            [
                f"Files changed: {result.changed_files}",
                f"Catalogue rows changed: {result.changed_rows}",
                "",
            ]
        )
        for heading, items in (
            ("Completed", result.completed),
            ("Skipped", result.skipped),
            ("Needs user review", result.needs_review),
            ("Failures", result.failures),
        ):
            lines.extend([f"## {heading}", ""])
            if not items:
                lines.append("None.")
            else:
                for item in items:
                    lines.append(f"- `{json.dumps(item, ensure_ascii=False, default=str)}`")
            lines.append("")
        return "\n".join(lines)


def append_change_log(
    path: Path,
    *,
    workflow: str,
    action: str,
    files_changed: int,
    catalogue_rows_changed: int,
    reason: str,
    uncertainty: str,
) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    block = (
        f"## {timestamp}\n\n"
        f"Workflow: {workflow}\n"
        f"Action: {action}\n"
        f"Files changed: {files_changed}\n"
        f"Catalogue rows changed: {catalogue_rows_changed}\n"
        f"Reason: {reason}\n"
        f"Uncertainty: {uncertainty or 'None'}\n\n"
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)
