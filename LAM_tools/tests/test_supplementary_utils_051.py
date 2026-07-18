from __future__ import annotations

from pathlib import Path

import pytest

from lam.exceptions import FileOperationError
from lam.services.file_service import FileService
from lam.utils.filename import (
    standard_document_filename_result,
    standard_supplementary_filename_result,
)
from lam.utils.supplementary import (
    canonical_supplementary_type,
    format_supplementary_sequence,
    parse_same_stem_supplementary_filename,
    parse_supplementary_filename,
    parse_uuid_supplementary_filename,
)


PAPER_UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize(
    ("filename", "kind", "sequence", "extension"),
    [
        (f"{PAPER_UUID}__supp.pdf", "Supplementary", None, ".pdf"),
        (f"{PAPER_UUID}__supp01.PDF", "Supplementary", 1, ".PDF"),
        (f"{PAPER_UUID}__table2.xlsx", "Table", 2, ".xlsx"),
        (f"{PAPER_UUID}__figure02.csv", "Figure", 2, ".csv"),
        (f"{PAPER_UUID}__methods.xls", "Methods", None, ".xls"),
        (f"{PAPER_UUID}__data03.csv", "Data", 3, ".csv"),
    ],
)
def test_uuid_supplementary_parser_is_exact(filename, kind, sequence, extension):
    parsed = parse_uuid_supplementary_filename(filename)
    assert parsed is not None
    assert parsed.binding == "paper_uuid"
    assert parsed.paper_uuid == PAPER_UUID
    assert parsed.parent_stem is None
    assert parsed.supplementary_type == kind
    assert parsed.sequence == sequence
    assert parsed.extension == extension


@pytest.mark.parametrize(
    "filename",
    [
        f"prefix-{PAPER_UUID}__supp.pdf",
        f"{PAPER_UUID}__supp00.pdf",
        f"{PAPER_UUID}__sup01.pdf",
        f"{PAPER_UUID}__fig01.pdf",
        f"{PAPER_UUID}__appendix.pdf",
        f"{PAPER_UUID}__unknown01.pdf",
        f"{PAPER_UUID}__table01.txt",
        f"{PAPER_UUID}__table01.pdf.extra",
        f"folder/{PAPER_UUID}__supp.pdf",
        "not-a-uuid__supp.pdf",
    ],
)
def test_uuid_supplementary_parser_rejects_noncanonical_or_unsupported_names(filename):
    assert parse_uuid_supplementary_filename(filename) is None


@pytest.mark.parametrize(
    ("filename", "parent", "kind", "sequence"),
    [
        ("filenameA_supp1.pdf", "filenameA", "Supplementary", 1),
        ("filename_with_parts_sup02.PDF", "filename_with_parts", "Supplementary", 2),
        ("filenameA_supplement.pdf", "filenameA", "Supplementary", None),
        ("filenameA_table01.xlsx", "filenameA", "Table", 1),
        ("filenameA_fig2.csv", "filenameA", "Figure", 2),
        ("filenameA_figure02.pdf", "filenameA", "Figure", 2),
        ("filenameA_methods.xls", "filenameA", "Methods", None),
        ("filenameA_data03.csv", "filenameA", "Data", 3),
        ("filenameA_appendix.pdf", "filenameA", "Appendix", None),
    ],
)
def test_same_stem_parser_preserves_exact_parent_prefix(filename, parent, kind, sequence):
    parsed = parse_same_stem_supplementary_filename(filename)
    assert parsed is not None
    assert parsed.binding == "same_stem"
    assert parsed.parent_stem == parent
    assert parsed.paper_uuid is None
    assert parsed.supplementary_type == kind
    assert parsed.sequence == sequence


def test_same_stem_parser_rejects_suffix_outside_the_declared_convention():
    assert parse_same_stem_supplementary_filename("filenameA_supplementary.pdf") is None


def test_general_parser_prefers_uuid_binding_over_same_stem():
    parsed = parse_supplementary_filename(f"{PAPER_UUID}__table01.csv")
    assert parsed is not None
    assert parsed.binding == "paper_uuid"


def test_type_and_sequence_helpers_are_deterministic():
    assert canonical_supplementary_type("FIG") == "Figure"
    assert canonical_supplementary_type("not-known") == ""
    assert canonical_supplementary_type("not-known", default="Supplementary") == "Supplementary"
    assert format_supplementary_sequence(None) == ""
    assert format_supplementary_sequence("1") == "01"
    assert format_supplementary_sequence(100) == "100"
    assert format_supplementary_sequence("00") == ""


def test_supplementary_naming_preserves_extension_and_only_truncates_title():
    result = standard_supplementary_filename_result(
        title="A very long article title " * 10,
        year="2025",
        journal_abbrev="Cell",
        publication_type="Review",
        supplementary_type="table",
        sequence="2",
        extension=".CSV",
        max_length=100,
    )
    assert result.filename is not None
    assert len(result.filename) <= 100
    assert result.filename.startswith("Cell, 2025, Review - ")
    assert result.filename.endswith(" - Table02.CSV")
    assert result.title_truncated is True


def test_supplementary_naming_uses_generic_type_without_forcing_sequence():
    result = standard_document_filename_result(
        title="Main Paper",
        year="2019",
        journal="Cell",
        supplementary_type="",
        extension="xlsx",
    )
    assert result.filename == "Cell, 2019 - Main Paper - Supplementary.xlsx"
    assert result.title_truncated is False


@pytest.mark.parametrize(
    ("sequence", "extension"),
    [(0, ".pdf"), ("abc", ".pdf"), (1, ".txt"), (1, ".tar.gz")],
)
def test_supplementary_naming_rejects_invalid_sequence_or_extension(sequence, extension):
    result = standard_supplementary_filename_result(
        title="Main Paper",
        year="2019",
        journal="Cell",
        sequence=sequence,
        extension=extension,
    )
    assert result.filename is None


def test_document_registration_move_accepts_managed_table_without_opening_it(
    library_factory,
):
    root = library_factory([])
    source = root / "Inbox" / "incoming.CSV"
    source.write_bytes(b"not opened as a spreadsheet")
    service = FileService(root)
    operation = service.plan_document_registration_move(
        source,
        "Cell, 2019 - Main Paper - Table01.CSV",
        2,
        "supplementary registration",
    )
    service.apply_document_registration_move(operation)
    assert not source.exists()
    assert operation.target.read_bytes() == b"not opened as a spreadsheet"


def test_document_registration_move_rejects_unsupported_or_changed_extension(
    library_factory,
):
    root = library_factory([])
    text = root / "Inbox" / "notes.txt"
    text.write_text("notes", encoding="utf-8")
    table = root / "Inbox" / "table.csv"
    table.write_bytes(b"table")
    service = FileService(root)
    with pytest.raises(FileOperationError, match="Unsupported"):
        service.plan_document_registration_move(text, "notes.txt", 2, "test")
    with pytest.raises(FileOperationError, match="preserve"):
        service.plan_document_registration_move(table, "table.xlsx", 2, "test")
    with pytest.raises(FileOperationError, match="only moves direct Inbox"):
        service.plan_document_registration_move(
            root / "Topics" / "missing.csv", "missing.csv", 2, "test"
        )


def test_document_registration_move_never_overwrites_late_target(library_factory):
    root = library_factory([])
    source = root / "Inbox" / "table.csv"
    source.write_bytes(b"source")
    service = FileService(root)
    operation = service.plan_document_registration_move(
        source, "Registered Table.csv", 2, "test"
    )
    operation.target.write_bytes(b"target")
    with pytest.raises(FileOperationError, match="Refusing to overwrite"):
        service.apply_document_registration_move(operation)
    assert source.read_bytes() == b"source"
    assert operation.target.read_bytes() == b"target"


def test_legacy_registration_api_still_rejects_non_pdf(library_factory):
    root = library_factory([])
    source = root / "Inbox" / "table.csv"
    source.write_bytes(b"table")
    with pytest.raises(FileOperationError, match="only moves PDF"):
        FileService(root).plan_registration_move(source, "table.csv", 2, "test")
