from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


HEADERS = [
    "id",
    "title",
    "authors",
    "doi",
    "pmid",
    "manual_tags",
    "topic_folder",
    "pdf_status",
    "pdf_filename",
    "pdf_relative_path",
    "date_updated",
    "notes",
    "uncertainty",
    "custom_column",
]


def create_catalogue(root: Path, rows: list[dict[str, object]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Catalogue"
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for row in rows:
        sheet.append([row.get(header) for header in HEADERS])
    notes = workbook.create_sheet("Other sheet")
    notes["A1"] = "preserve me"
    path = root / "catalogue.xlsx"
    workbook.save(path)
    return path


def write_pdf(path: Path, marker: bytes = b"paper") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + marker + b"\n%%EOF\n")


@pytest.fixture
def library_factory(tmp_path: Path):
    def factory(rows: list[dict[str, object]], files: dict[str, bytes] | None = None) -> Path:
        root = tmp_path / "library"
        root.mkdir()
        (root / "Inbox").mkdir()
        (root / "Registered").mkdir()
        create_catalogue(root, rows)
        for relative, marker in (files or {}).items():
            write_pdf(root / relative, marker)
        return root

    return factory

