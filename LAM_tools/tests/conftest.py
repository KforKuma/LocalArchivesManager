from __future__ import annotations

from pathlib import Path
import uuid
import hashlib
import re

import pytest
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from lam.schema import CATALOGUE_FIELDS, DOCUMENT_FIELDS


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
def legacy_library_factory(tmp_path: Path):
    def factory(rows: list[dict[str, object]], files: dict[str, bytes] | None = None) -> Path:
        root = tmp_path / "library"
        root.mkdir()
        (root / "Inbox").mkdir()
        (root / "Registered").mkdir()
        (root / "Topics").mkdir()
        create_catalogue(root, rows)
        for relative, marker in (files or {}).items():
            write_pdf(root / relative, marker)
        return root

    return factory


@pytest.fixture
def current_library_factory(tmp_path: Path):
    """Create a strict current-schema library for ordinary workflow/CLI tests."""
    def factory(
        rows: list[dict[str, object]] | None = None,
        documents: list[dict[str, object]] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> Path:
        root = tmp_path / "current-library"
        root.mkdir()
        for name in ("Inbox", "Registered", "Topics"):
            (root / name).mkdir()
        workbook = Workbook()
        catalogue = workbook.active
        catalogue.title = "Catalogue"
        supplied_rows = rows or []
        legacy_only = {"id", "record_uid", "pdf_status", "pdf_filename", "pdf_relative_path"}
        extra_fields = sorted(
            {
                key
                for row in supplied_rows
                for key in row
                if key not in CATALOGUE_FIELDS and key not in legacy_only
            }
        )
        catalogue_headers = (*CATALOGUE_FIELDS, *extra_fields)
        catalogue.append(catalogue_headers)
        for cell in catalogue[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        normalized_rows: list[dict[str, object]] = []
        for index, row in enumerate(supplied_rows, start=1):
            values = dict(row)
            values.setdefault(
                "paper_uuid",
                str(uuid.UUID(f"00000000-0000-4000-8000-{index:012x}")),
            )
            normalized_rows.append(values)
            catalogue.append([values.get(field) for field in catalogue_headers])
        document_sheet = workbook.create_sheet("Documents")
        document_sheet.append(DOCUMENT_FIELDS)
        for document in documents or []:
            document_sheet.append([document.get(field) for field in DOCUMENT_FIELDS])
        notes = workbook.create_sheet("Other sheet")
        notes["A1"] = "preserve me"
        workbook.save(root / "catalogue.xlsx")
        for relative, marker in (files or {}).items():
            write_pdf(root / relative, marker)
        return root

    return factory


@pytest.fixture
def library_factory(current_library_factory):
    """Backward-friendly input adapter that always emits the current schema."""
    local_id = re.compile(r"^LOCAL:([0-9a-f-]{36})$", re.I)

    def factory(
        rows: list[dict[str, object]], files: dict[str, bytes] | None = None
    ) -> Path:
        normalized_rows: list[dict[str, object]] = []
        documents: list[dict[str, object]] = []
        for index, raw in enumerate(rows, start=1):
            values = dict(raw)
            candidate = str(values.get("paper_uuid") or values.get("record_uid") or "")
            legacy_id = str(values.get("id") or "")
            match = local_id.fullmatch(legacy_id)
            if not candidate and match:
                candidate = match.group(1)
            try:
                parsed = uuid.UUID(candidate)
                if parsed.version != 4:
                    raise ValueError
                paper_uuid = str(parsed)
            except ValueError:
                paper_uuid = str(uuid.UUID(f"00000000-0000-4000-8000-{index:012x}"))
            values["paper_uuid"] = paper_uuid
            if legacy_id.upper().startswith("PMID:") and not values.get("pmid"):
                values["pmid"] = legacy_id.split(":", 1)[1]
            if legacy_id.upper().startswith("DOI:") and not values.get("doi"):
                values["doi"] = legacy_id.split(":", 1)[1]
            normalized_rows.append(values)

            relative = str(values.get("pdf_relative_path") or "").replace("\\", "/")
            filename = str(values.get("pdf_filename") or "")
            # Inbox files are not registered documents yet. Workflow 3 creates
            # the Documents row only after identity is accepted and the file
            # is moved to Registered/.
            legacy_status = str(values.get("pdf_status") or "").casefold()
            has_managed_location = bool(relative) and not relative.casefold().startswith(
                "inbox/"
            )
            status_is_registered = legacy_status in {
                "registered",
                "filed",
                "missing",
                "unclear",
            }
            if (relative or filename) and (
                has_managed_location or status_is_registered
            ):
                relative = relative or f"Registered/{filename}"
                filename = filename or Path(relative).name
                marker = (files or {}).get(relative)
                digest = ""
                if marker is not None:
                    content = b"%PDF-1.4\n" + marker + b"\n%%EOF\n"
                    digest = hashlib.sha256(content).hexdigest()
                documents.append(
                    {
                        "document_id": f"{paper_uuid}:main",
                        "paper_uuid": paper_uuid,
                        "document_type": "main",
                        "filename": filename,
                        "relative_path": relative,
                        "extension": Path(filename).suffix,
                        "sha256": digest,
                        "file_status": values.get("pdf_status") or "unclear",
                        "source": values.get("source") or "",
                        "date_added": values.get("date_added") or "",
                        "date_updated": values.get("date_updated") or "",
                    }
                )
        return current_library_factory(normalized_rows, documents, files)

    return factory
