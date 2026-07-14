from __future__ import annotations

import csv
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from ..exceptions import CatalogueError
from ..models import CatalogueChange, CatalogueRecord
from ..schema import (
    MACHINE_MAINTAINED_FIELDS,
    PHASE1_REQUIRED_FIELDS,
    SNAPSHOT_FIELDS,
    USER_CONTROLLED_FIELDS,
)
from ..utils.normalize import normalized_text


UNCERTAINTY_PREFIXES = ("NEEDS_REVIEW:", "USER_CONFIRMED:", "MACHINE_NOTE:", "RESOLVED:")


class CatalogueService:
    def __init__(self, path: Path):
        self.path = path
        self.workbook = None
        self.worksheet = None
        self.headers: dict[str, int] = {}
        self.records: list[CatalogueRecord] = []
        self.changes: list[CatalogueChange] = []

    def load(self) -> list[CatalogueRecord]:
        try:
            self.workbook = load_workbook(self.path)
        except Exception as exc:
            raise CatalogueError(f"Cannot open catalogue: {self.path}") from exc

        candidates: list[tuple[Any, dict[str, int]]] = []
        for sheet in self.workbook.worksheets:
            headers = self._read_headers(sheet)
            if PHASE1_REQUIRED_FIELDS.issubset(headers):
                candidates.append((sheet, headers))
        if not candidates:
            available = {
                sheet.title: sorted(self._read_headers(sheet))
                for sheet in self.workbook.worksheets
            }
            raise CatalogueError(
                "No worksheet contains all phase-1 fields. "
                f"Required={sorted(PHASE1_REQUIRED_FIELDS)}; available={available}"
            )
        self.worksheet, self.headers = candidates[0]
        self._validate_duplicate_headers()
        self.records = []
        for row_number in range(2, self.worksheet.max_row + 1):
            values = {
                name: self.worksheet.cell(row=row_number, column=column).value
                for name, column in self.headers.items()
            }
            if any(value not in (None, "") for value in values.values()):
                self.records.append(CatalogueRecord(row_number=row_number, values=values))
        self._validate_duplicate_values()
        return self.records

    @staticmethod
    def _read_headers(sheet: Any) -> dict[str, int]:
        headers: dict[str, int] = {}
        for column in range(1, sheet.max_column + 1):
            raw = sheet.cell(row=1, column=column).value
            if raw is None or not str(raw).strip():
                continue
            name = str(raw).strip()
            if name not in headers:
                headers[name] = column
        return headers

    def _validate_duplicate_headers(self) -> None:
        assert self.worksheet is not None
        seen: dict[str, int] = {}
        duplicates: list[str] = []
        for column in range(1, self.worksheet.max_column + 1):
            raw = self.worksheet.cell(row=1, column=column).value
            if raw is None or not str(raw).strip():
                continue
            name = str(raw).strip()
            if name in seen:
                duplicates.append(name)
            seen[name] = column
        if duplicates:
            raise CatalogueError(f"Duplicate catalogue columns: {sorted(set(duplicates))}")

    def _validate_duplicate_values(self) -> None:
        problems: list[str] = []
        for field_name in ("id", "doi", "pmid", "pdf_relative_path"):
            if field_name not in self.headers:
                continue
            seen: dict[str, int] = {}
            for record in self.records:
                key = normalized_text(record.get(field_name))
                if not key:
                    continue
                if key in seen:
                    problems.append(
                        f"{field_name}={record.get(field_name)!r} at rows "
                        f"{seen[key]} and {record.row_number}"
                    )
                else:
                    seen[key] = record.row_number
        if problems:
            raise CatalogueError("Duplicate catalogue identifiers/paths: " + "; ".join(problems))

    def find_by(self, field_name: str, value: object) -> list[CatalogueRecord]:
        key = normalized_text(value)
        if not key:
            return []
        return [record for record in self.records if normalized_text(record.get(field_name)) == key]

    def update_fields(self, record: CatalogueRecord, updates: dict[str, Any]) -> list[CatalogueChange]:
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before it can be updated")
        applied: list[CatalogueChange] = []
        for field_name, new_value in updates.items():
            if field_name in USER_CONTROLLED_FIELDS:
                raise CatalogueError(f"Refusing to overwrite user-controlled field: {field_name}")
            if field_name not in MACHINE_MAINTAINED_FIELDS:
                raise CatalogueError(f"Phase 1 cannot update field: {field_name}")
            if field_name not in self.headers:
                raise CatalogueError(f"Catalogue field is missing: {field_name}")
            old_value = record.get(field_name, None)
            if self._equivalent(old_value, new_value):
                continue
            column = self.headers[field_name]
            self.worksheet.cell(row=record.row_number, column=column).value = new_value
            record.values[field_name] = new_value
            change = CatalogueChange(record.row_number, field_name, old_value, new_value)
            self.changes.append(change)
            applied.append(change)
        return applied

    @staticmethod
    def _equivalent(left: Any, right: Any) -> bool:
        if left in (None, "") and right in (None, ""):
            return True
        return left == right

    def add_uncertainty(
        self,
        record: CatalogueRecord,
        prefix: str,
        field_name: str,
        issue: str,
        *,
        conflict_with_confirmation: bool = False,
    ) -> bool:
        if prefix not in UNCERTAINTY_PREFIXES:
            raise CatalogueError(f"Unsupported uncertainty prefix: {prefix}")
        current = str(record.get("uncertainty") or "")
        lines = [line.rstrip() for line in current.splitlines() if line.strip()]
        if (
            prefix == "NEEDS_REVIEW:"
            and not conflict_with_confirmation
            and self._has_user_confirmation(lines, field_name)
        ):
            return False
        line = f"{prefix} field={field_name}; issue={issue}"
        normalized_line = normalized_text(line)
        if any(normalized_text(existing) == normalized_line for existing in lines):
            return False
        lines.append(line)
        self.update_fields(record, {"uncertainty": "\n".join(lines)})
        return True

    @staticmethod
    def _has_user_confirmation(lines: Iterable[str], field_name: str) -> bool:
        pattern = re.compile(rf"^USER_CONFIRMED:\s*field={re.escape(field_name)}(?:;|$)", re.I)
        return any(pattern.search(line.strip()) for line in lines)

    def snapshot_payload(self) -> dict[str, Any]:
        rows = []
        for record in self.records:
            rows.append(
                {
                    "row_number": record.row_number,
                    "fields": {field: record.get(field, None) for field in SNAPSHOT_FIELDS},
                }
            )
        return {"sheet": self.worksheet.title if self.worksheet else None, "rows": rows}

    def save_atomic(self) -> Path | None:
        if not self.changes:
            return None
        if self.workbook is None:
            raise CatalogueError("Catalogue must be loaded before it can be saved")
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = self._unique_backup_path(timestamp)
        temporary = self.path.with_name(f".{self.path.stem}.{timestamp}.tmp.xlsx")
        try:
            shutil.copy2(self.path, backup)
            self.workbook.save(temporary)
            check = load_workbook(temporary, read_only=True)
            check.close()
            os.replace(temporary, self.path)
            return backup
        except Exception as exc:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
            pending = self.path.with_name("catalogue_pending_updates.csv")
            self._write_pending_updates(pending)
            raise CatalogueError(
                f"Catalogue write failed; proposed changes exported to {pending}"
            ) from exc

    def _unique_backup_path(self, timestamp: str) -> Path:
        candidate = self.path.with_name(f"catalogue.backup.{timestamp}.xlsx")
        counter = 1
        while candidate.exists():
            candidate = self.path.with_name(
                f"catalogue.backup.{timestamp}-{counter:02d}.xlsx"
            )
            counter += 1
        return candidate

    def _write_pending_updates(self, path: Path) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["row_number", "field_name", "old_value", "new_value"],
            )
            writer.writeheader()
            for change in self.changes:
                writer.writerow(
                    {
                        "row_number": change.row_number,
                        "field_name": change.field_name,
                        "old_value": change.old_value,
                        "new_value": change.new_value,
                    }
                )
