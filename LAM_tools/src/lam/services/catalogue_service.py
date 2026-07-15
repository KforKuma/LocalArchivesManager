from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import uuid
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from ..exceptions import CatalogueError
from ..models import CatalogueChange, CatalogueRecord
from ..schema import (
    MACHINE_FILLABLE_FIELDS,
    MACHINE_MAINTAINED_FIELDS,
    PHASE1_REQUIRED_FIELDS,
    SNAPSHOT_FIELDS,
    SYSTEM_IDENTITY_FIELDS,
    USER_CONTROLLED_FIELDS,
)
from ..utils.identifiers import normalize_doi, normalize_pmid
from ..utils.normalize import normalized_text
from ..utils.publication_type import canonicalize_publication_type
from ..utils.uncertainty import (
    confirmed_value,
    has_user_confirmation,
    parse_user_confirmations,
)


UNCERTAINTY_PREFIXES = ("NEEDS_REVIEW:", "USER_CONFIRMED:", "MACHINE_NOTE:", "RESOLVED:")


class CatalogueService:
    def __init__(self, path: Path):
        self.path = path
        self.workbook = None
        self.worksheet = None
        self.headers: dict[str, int] = {}
        self.records: list[CatalogueRecord] = []
        self.changes: list[CatalogueChange] = []
        self.review_decisions: set[str] = set()

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
        for field_name in ("record_uid", "id", "doi", "pmid", "pdf_relative_path"):
            if field_name not in self.headers:
                continue
            seen: dict[str, int] = {}
            for record in self.records:
                key = self._field_key(field_name, record.get(field_name))
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
        key = self._field_key(field_name, value)
        if not key:
            return []
        return [
            record
            for record in self.records
            if self._field_key(field_name, record.get(field_name)) == key
        ]

    @staticmethod
    def _field_key(field_name: str, value: object) -> str:
        if field_name == "doi":
            return normalize_doi(value)
        if field_name == "pmid":
            return normalize_pmid(value)
        return normalized_text(value)

    def update_fields(self, record: CatalogueRecord, updates: dict[str, Any]) -> list[CatalogueChange]:
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before it can be updated")
        applied: list[CatalogueChange] = []
        for field_name, new_value in updates.items():
            if field_name in USER_CONTROLLED_FIELDS:
                raise CatalogueError(f"Refusing to overwrite user-controlled field: {field_name}")
            if field_name not in MACHINE_MAINTAINED_FIELDS | MACHINE_FILLABLE_FIELDS:
                raise CatalogueError(f"Workflow cannot update field: {field_name}")
            if field_name not in self.headers:
                raise CatalogueError(f"Catalogue field is missing: {field_name}")
            if field_name == "publication_type":
                new_value = canonicalize_publication_type(new_value).canonical_type
            old_value = record.get(field_name, None)
            if (
                field_name in MACHINE_FILLABLE_FIELDS
                and old_value not in (None, "")
                and not self._equivalent(old_value, new_value)
            ):
                raise CatalogueError(
                    f"Refusing to overwrite non-empty bibliographic field: {field_name}"
                )
            if self._equivalent(old_value, new_value):
                continue
            column = self.headers[field_name]
            self.worksheet.cell(row=record.row_number, column=column).value = new_value
            record.values[field_name] = new_value
            change = CatalogueChange(record.row_number, field_name, old_value, new_value)
            self.changes.append(change)
            applied.append(change)
        return applied

    def ensure_header(self, field_name: str) -> int:
        """Add one optional machine column in memory while preserving all sheets."""
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before its schema can be extended")
        if field_name in self.headers:
            return self.headers[field_name]
        column = self.worksheet.max_column + 1
        target = self.worksheet.cell(row=1, column=column)
        target.value = field_name
        if column > 1:
            source = self.worksheet.cell(row=1, column=column - 1)
            target.font = copy(source.font)
            target.fill = copy(source.fill)
            target.border = copy(source.border)
            target.alignment = copy(source.alignment)
            target.number_format = source.number_format
            target.protection = copy(source.protection)
        self.headers[field_name] = column
        for record in self.records:
            record.values.setdefault(field_name, None)
        return column

    def ensure_record_uid(self, record: CatalogueRecord) -> str:
        """Return the immutable row UUID, creating it only when absent."""
        current = str(record.get("record_uid") or "").strip()
        if current:
            try:
                return str(uuid.UUID(current))
            except ValueError as exc:
                raise CatalogueError(
                    f"Invalid record_uid at row {record.row_number}: {current!r}"
                ) from exc
        column = self.ensure_header("record_uid")
        value = str(uuid.uuid4())
        self.worksheet.cell(row=record.row_number, column=column).value = value
        record.values["record_uid"] = value
        self.changes.append(
            CatalogueChange(record.row_number, "record_uid", None, value)
        )
        return value

    def update_canonical_fields(
        self, record: CatalogueRecord, updates: dict[str, Any]
    ) -> list[CatalogueChange]:
        """Apply a prevalidated canonical record without overwriting user decisions."""
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before it can be updated")
        confirmed_fields = {
            item.field.strip().casefold()
            for item in parse_user_confirmations(record.get("uncertainty"))
        }
        applied: list[CatalogueChange] = []
        for field_name, new_value in updates.items():
            if field_name in USER_CONTROLLED_FIELDS:
                raise CatalogueError(f"Refusing to overwrite user-controlled field: {field_name}")
            if field_name not in (
                SYSTEM_IDENTITY_FIELDS | MACHINE_MAINTAINED_FIELDS | MACHINE_FILLABLE_FIELDS
            ):
                raise CatalogueError(f"Workflow cannot canonicalize field: {field_name}")
            if field_name == "record_uid":
                current_uid = str(record.get("record_uid") or "").strip()
                if current_uid and normalized_text(current_uid) != normalized_text(new_value):
                    raise CatalogueError("Refusing to change immutable record_uid")
                self.ensure_header("record_uid")
            elif field_name not in self.headers:
                raise CatalogueError(f"Catalogue field is missing: {field_name}")
            if field_name.casefold() in confirmed_fields and field_name not in {"id", "record_uid"}:
                continue
            if field_name == "publication_type":
                new_value = canonicalize_publication_type(new_value).canonical_type
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

    def update_provisional_fields(
        self, record: CatalogueRecord, updates: dict[str, Any]
    ) -> list[CatalogueChange]:
        """Upgrade machine-owned values on one LOCAL row after confirmed metadata."""
        if not str(record.get("id") or "").upper().startswith("LOCAL:"):
            return self.update_fields(record, updates)
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before it can be updated")
        current_uncertainty = str(record.get("uncertainty") or "")
        confirmed_fields = {
            item.field.strip().casefold()
            for item in parse_user_confirmations(current_uncertainty)
        }
        applied: list[CatalogueChange] = []
        for field_name, new_value in updates.items():
            if field_name in USER_CONTROLLED_FIELDS:
                raise CatalogueError(f"Refusing to overwrite user-controlled field: {field_name}")
            if field_name not in MACHINE_MAINTAINED_FIELDS | MACHINE_FILLABLE_FIELDS:
                raise CatalogueError(f"Workflow cannot update field: {field_name}")
            if field_name not in self.headers:
                raise CatalogueError(f"Catalogue field is missing: {field_name}")
            if field_name == "publication_type":
                new_value = canonicalize_publication_type(new_value).canonical_type
            if field_name.casefold() in confirmed_fields:
                continue
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

    def add_record(self, values: dict[str, Any]) -> CatalogueRecord:
        """Append one machine-created row without changing existing row order."""
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before a row can be added")
        self.ensure_header("record_uid")
        normalized_values = dict(values)
        normalized_values.setdefault("record_uid", str(uuid.uuid4()))
        if "publication_type" in normalized_values:
            normalized_values["publication_type"] = canonicalize_publication_type(
                normalized_values["publication_type"]
            ).canonical_type
        supplied = {
            key: value for key, value in normalized_values.items() if key in self.headers and value not in (None, "")
        }
        unsupported = set(supplied) - (
            SYSTEM_IDENTITY_FIELDS | MACHINE_FILLABLE_FIELDS | MACHINE_MAINTAINED_FIELDS
        )
        if unsupported:
            raise CatalogueError(
                f"Refusing unsupported fields in new catalogue row: {sorted(unsupported)}"
            )
        if any(supplied.get(field) for field in USER_CONTROLLED_FIELDS):
            raise CatalogueError("Machine-created rows cannot set user-controlled fields")
        for field_name in ("record_uid", "id", "doi", "pmid"):
            value = supplied.get(field_name)
            if value and self.find_by(field_name, value):
                raise CatalogueError(
                    f"Refusing duplicate catalogue record: {field_name}={value!r}"
                )
        row_number = self.worksheet.max_row + 1
        for field_name, value in supplied.items():
            self.worksheet.cell(row=row_number, column=self.headers[field_name]).value = value
        record_values = {
            name: self.worksheet.cell(row=row_number, column=column).value
            for name, column in self.headers.items()
        }
        record = CatalogueRecord(row_number=row_number, values=record_values)
        self.records.append(record)
        self.changes.append(CatalogueChange(row_number, "__row__", None, supplied))
        return record

    def repair_publication_type(
        self, record: CatalogueRecord, raw_value: Any
    ) -> list[CatalogueChange]:
        """Normalize the machine-fillable type during the explicit repair workflow."""
        if self.worksheet is None:
            raise CatalogueError("Catalogue must be loaded before it can be updated")
        if "publication_type" not in self.headers:
            raise CatalogueError("Catalogue field is missing: publication_type")
        new_value = canonicalize_publication_type(raw_value).canonical_type
        old_value = record.get("publication_type", None)
        if self._equivalent(old_value, new_value):
            return []
        column = self.headers["publication_type"]
        self.worksheet.cell(row=record.row_number, column=column).value = new_value
        record.values["publication_type"] = new_value
        change = CatalogueChange(record.row_number, "publication_type", old_value, new_value)
        self.changes.append(change)
        return [change]

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
        issue_key: str | None = None,
    ) -> bool:
        if prefix not in UNCERTAINTY_PREFIXES:
            raise CatalogueError(f"Unsupported uncertainty prefix: {prefix}")
        if prefix == "NEEDS_REVIEW:":
            outcome = self.ensure_review_blocker(
                record,
                field_name,
                issue,
                issue_key=issue_key or "review",
                conflict_with_confirmation=conflict_with_confirmation,
            )
            return outcome == "added"
        current = str(record.get("uncertainty") or "")
        lines = [line.rstrip() for line in current.splitlines() if line.strip()]
        issue_part = f"; issue_key={issue_key}" if issue_key else ""
        line = f"{prefix} field={field_name}{issue_part}; issue={issue}"
        normalized_line = normalized_text(line)
        if any(normalized_text(existing) == normalized_line for existing in lines):
            return False
        lines.append(line)
        self.update_fields(record, {"uncertainty": "\n".join(lines)})
        return True

    def ensure_review_blocker(
        self,
        record: CatalogueRecord,
        field_name: str,
        issue: str,
        *,
        issue_key: str,
        conflict_with_confirmation: bool = False,
    ) -> str:
        """Return added, existing, confirmed, or cleared for one row/field blocker."""
        current = str(record.get("uncertainty") or "")
        lines = [line.rstrip() for line in current.splitlines() if line.strip()]
        if not conflict_with_confirmation and self._has_user_confirmation(lines, field_name):
            retained = [
                line for line in lines if not self._is_review_for_field(line, field_name)
            ]
            if retained != lines:
                self.update_fields(record, {"uncertainty": "\n".join(retained)})
            return "confirmed"

        decision_key = self._review_decision_key(record, field_name, issue_key, issue)
        if decision_key in self.review_decisions:
            return "cleared"
        if any(self._is_review_for_field(line, field_name) for line in lines):
            return "existing"

        line = (
            f"NEEDS_REVIEW: field={field_name}; issue_key={issue_key}; issue={issue}"
        )
        lines.append(line)
        self.update_fields(record, {"uncertainty": "\n".join(lines)})
        return "added"

    def configure_review_state(self, previous_snapshot: dict[str, Any]) -> None:
        """Carry approvals and treat a user-cleared blocker as a one-time decision."""
        self.review_decisions.update(previous_snapshot.get("review_decisions", []))
        old_rows = {row.get("row_number"): row for row in previous_snapshot.get("rows", [])}
        old_by_uid = {
            normalized_text(row.get("record_uid") or row.get("fields", {}).get("record_uid")): row
            for row in previous_snapshot.get("rows", [])
            if normalized_text(row.get("record_uid") or row.get("fields", {}).get("record_uid"))
        }
        for record in self.records:
            old_row = old_by_uid.get(normalized_text(record.get("record_uid"))) or old_rows.get(
                record.row_number, {}
            )
            old_uncertainty = str(
                old_row.get("fields", {}).get("uncertainty") or ""
            )
            current_lines = {
                line.strip()
                for line in str(record.get("uncertainty") or "").splitlines()
                if line.strip()
            }
            for old_line in old_uncertainty.splitlines():
                old_line = old_line.strip()
                parsed = self._parse_review_line(old_line)
                if not parsed or old_line in current_lines:
                    continue
                field_name, issue_key, issue = parsed
                if any(self._is_review_for_field(line, field_name) for line in current_lines):
                    continue
                self.review_decisions.add(
                    self._review_decision_key(
                        record, field_name, issue_key, issue
                    )
                )

    def has_user_confirmation(self, record: CatalogueRecord, field_name: str) -> bool:
        return has_user_confirmation(record.get("uncertainty"), field_name)

    def confirmed_value(self, record: CatalogueRecord, field_name: str) -> str:
        return confirmed_value(record.get("uncertainty"), field_name)

    def resolve_confirmed_reviews(self, record: CatalogueRecord) -> list[str]:
        """Remove only machine blockers covered by a user confirmation."""
        current = str(record.get("uncertainty") or "")
        lines = [line.rstrip() for line in current.splitlines() if line.strip()]
        removed: list[str] = []
        retained: list[str] = []
        for line in lines:
            parsed = self._parse_review_line(line)
            if parsed and self.has_user_confirmation(record, parsed[0]):
                removed.append(parsed[1])
                continue
            retained.append(line)
        if retained != lines:
            self.update_fields(record, {"uncertainty": "\n".join(retained)})
        return removed

    def resolve_review_blockers(
        self,
        record: CatalogueRecord,
        field_name: str,
        issue_keys: set[str],
        *,
        resolution: str,
    ) -> list[str]:
        """Resolve only named machine blockers while preserving all user text."""
        current = str(record.get("uncertainty") or "")
        lines = [line.rstrip() for line in current.splitlines() if line.strip()]
        removed: list[str] = []
        retained: list[str] = []
        for line in lines:
            parsed = self._parse_review_line(line)
            if parsed and normalized_text(parsed[0]) == normalized_text(field_name) and parsed[1] in issue_keys:
                removed.append(parsed[1])
                continue
            retained.append(line)
        if not removed:
            return []
        resolved_line = (
            f"RESOLVED: field={field_name}; issue_key={','.join(sorted(set(removed)))}; "
            f"issue={resolution}"
        )
        if not any(normalized_text(line) == normalized_text(resolved_line) for line in retained):
            retained.append(resolved_line)
        self.update_fields(record, {"uncertainty": "\n".join(retained)})
        return removed

    def active_review_lines(self, record: CatalogueRecord) -> list[str]:
        active: list[str] = []
        for line in str(record.get("uncertainty") or "").splitlines():
            parsed = self._parse_review_line(line.strip())
            if not parsed:
                continue
            field_name, issue_key, issue = parsed
            if self.has_user_confirmation(record, field_name):
                continue
            if self._review_decision_key(record, field_name, issue_key, issue) in self.review_decisions:
                continue
            active.append(line.rstrip())
        return active

    @staticmethod
    def _parse_review_line(line: str) -> tuple[str, str, str] | None:
        if not line.lstrip().upper().startswith("NEEDS_REVIEW:"):
            return None
        field_match = re.search(r"(?:NEEDS_REVIEW:|;)\s*field=([^;]+)", line, re.I)
        issue_key_match = re.search(r"(?:^|;)\s*issue_key=([^;]+)", line, re.I)
        issue_match = re.search(r"(?:^|;)\s*issue=(.*)$", line, re.I)
        if not field_match or not issue_match:
            return None
        return (
            field_match.group(1).strip(),
            issue_key_match.group(1).strip() if issue_key_match else "review",
            issue_match.group(1).strip(),
        )

    @classmethod
    def _is_review_for_field(cls, line: str, field_name: str) -> bool:
        parsed = cls._parse_review_line(line)
        return bool(parsed and normalized_text(parsed[0]) == normalized_text(field_name))

    @staticmethod
    def _review_decision_key(
        record: CatalogueRecord,
        field_name: str,
        issue_key: str,
        issue: str,
    ) -> str:
        identity = (
            record.get("record_uid")
            or record.get("id")
            or f"row:{record.row_number}"
        )
        evidence = "|".join(
            normalized_text(record.get(field))
            for field in ("pmid", "doi", "title", "authors", "year", "journal")
        )
        payload = "|".join(
            normalized_text(value)
            for value in (identity, field_name, issue_key, issue, evidence)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _has_user_confirmation(lines: Iterable[str], field_name: str) -> bool:
        return has_user_confirmation("\n".join(lines), field_name)

    def snapshot_payload(self) -> dict[str, Any]:
        rows = []
        for record in self.records:
            rows.append(
                {
                    "row_number": record.row_number,
                    "record_uid": record.get("record_uid", None),
                    "fields": {field: record.get(field, None) for field in SNAPSHOT_FIELDS},
                }
            )
        return {
            "sheet": self.worksheet.title if self.worksheet else None,
            "rows": rows,
            "review_decisions": sorted(self.review_decisions),
        }

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
