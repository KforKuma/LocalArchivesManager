from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject


@pytest.fixture(autouse=True)
def disable_real_ocr_by_default(monkeypatch):
    """Default tests never initialize or download real OCR models."""
    monkeypatch.setenv("OCR_ENABLED", "false")


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
    "year",
    "journal",
    "journal_abbrev",
    "publication_type",
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


def write_text_pdf(
    path: Path,
    pages: list[str],
    *,
    metadata: dict[str, str] | None = None,
    password: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_ref}
                )
            }
        )
        commands = [b"BT /F1 12 Tf 72 720 Td 16 TL"]
        for index, line in enumerate(text.splitlines() or [""]):
            escaped = (
                line.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .encode("latin-1", errors="replace")
            )
            if index:
                commands.append(b"T*")
            commands.append(b"(" + escaped + b") Tj")
        commands.append(b"ET")
        stream = DecodedStreamObject()
        stream.set_data(b"\n".join(commands))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if metadata:
        writer.add_metadata(metadata)
    if password:
        writer.encrypt(password)
    with path.open("wb") as handle:
        writer.write(handle)


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
