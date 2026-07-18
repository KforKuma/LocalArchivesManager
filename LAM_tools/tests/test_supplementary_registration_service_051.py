from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lam.services.catalogue_service import CatalogueService
from lam.services.supplementary_registration_service import (
    BINDING_AMBIGUOUS,
    DOCUMENT_ID_CONFLICT,
    DUPLICATE_FILE,
    MAIN_DOCUMENT_MISSING,
    NAME_UNRECOGNIZED,
    PARENT_MISSING,
    SEQUENCE_CONFLICT,
    TARGET_COLLISION,
    UUID_NOT_FOUND,
    SupplementaryRegistrationService,
)


def _loaded_catalogue(root: Path, *, with_main: bool = True):
    catalogue = CatalogueService(root / "catalogue.xlsx")
    records = catalogue.load()
    paper = records[0]
    paper_uuid = catalogue.ensure_paper_uuid(paper)
    catalogue.ensure_documents_sheet()
    if with_main and not catalogue.documents:
        catalogue.add_document(
            {
                "document_id": f"{paper_uuid}:main",
                "paper_uuid": paper_uuid,
                "document_type": "main",
                "filename": "main.pdf",
                "relative_path": "Registered/main.pdf",
                "extension": ".pdf",
                "sha256": hashlib.sha256(b"main").hexdigest(),
                "file_status": "registered",
            }
        )
    return catalogue, paper, paper_uuid


def _root_with_paper(library_factory):
    return library_factory(
        [
            {
                "id": "PMID:1",
                "title": "Coordinated Paper",
                "year": "2025",
                "journal": "Test Journal",
                "journal_abbrev": "Test J",
                "topic_folder": "Topic",
            }
        ],
        {"Registered/main.pdf": b"main"},
    )


def test_scan_classifies_uuid_groups_independent_main_orphans_and_skips(library_factory):
    root = _root_with_paper(library_factory)
    catalogue, _, paper_uuid = _loaded_catalogue(root)
    inbox = root / "Inbox"
    (inbox / f"{paper_uuid}__table01.csv").write_bytes(b"uuid table")
    (inbox / "batch.pdf").write_bytes(b"main")
    (inbox / "batch_supp.pdf").write_bytes(b"supp")
    (inbox / "batch_figure02.pdf").write_bytes(b"figure")
    (inbox / "standalone.pdf").write_bytes(b"standalone")
    (inbox / "missing_table01.csv").write_bytes(b"orphan")
    (inbox / "unknown.xlsx").write_bytes(b"orphan sheet")
    unknown_uuid = "00000000-0000-4000-8000-000000000999"
    (inbox / f"{unknown_uuid}__supp.pdf").write_bytes(b"unknown uuid")
    (inbox / "notes.txt").write_text("notes", encoding="utf-8")
    (inbox / ".hidden.csv").write_bytes(b"hidden")

    scan = SupplementaryRegistrationService(root, catalogue).scan_inbox()

    assert [item.source.name for item in scan.uuid_supplementary] == [
        f"{paper_uuid}__table01.csv"
    ]
    assert len(scan.same_stem_groups) == 1
    group = scan.same_stem_groups[0]
    assert group.main_pdf.name == "batch.pdf"
    assert [item.source.name for item in group.supplementary] == [
        "batch_figure02.pdf",
        "batch_supp.pdf",
    ]
    assert [path.name for path in scan.independent_main_pdfs] == ["standalone.pdf"]
    reasons = {item.source.name: item.reason for item in scan.orphan}
    assert reasons == {
        "missing_table01.csv": PARENT_MISSING,
        "unknown.xlsx": NAME_UNRECOGNIZED,
        f"{unknown_uuid}__supp.pdf": UUID_NOT_FOUND,
    }
    assert {item["reason"] for item in scan.skipped} == {
        "hidden_or_temporary",
        "unsupported_extension",
    }


def test_uuid_plan_builds_document_values_without_mutating_catalogue(library_factory):
    root = _root_with_paper(library_factory)
    catalogue, _, paper_uuid = _loaded_catalogue(root)
    source = root / "Inbox" / f"{paper_uuid}__table1.CSV"
    source.write_bytes(b"table contents")
    documents_before = list(catalogue.documents)
    service = SupplementaryRegistrationService(root, catalogue)

    plans = service.plan_known_uuid_supplementaries(service.scan_inbox())

    assert len(plans) == 1
    plan = plans[0]
    assert plan.ready is True
    assert plan.document_id == f"{paper_uuid}:supp:table:01"
    assert plan.sha256 == hashlib.sha256(b"table contents").hexdigest()
    assert plan.target_filename == "Test J, 2025 - Coordinated Paper - Table01.CSV"
    assert plan.target_relative_path == (
        "Registered/Test J, 2025 - Coordinated Paper - Table01.CSV"
    )
    assert plan.document_values["document_type"] == "supplementary"
    assert plan.document_values["sequence"] == 1
    assert plan.document_values["extension"] == ".CSV"
    assert catalogue.documents == documents_before
    assert not plan.target_path.exists()


def test_planning_spreadsheet_uses_hash_only_and_no_content_parser(
    library_factory, monkeypatch
):
    root = _root_with_paper(library_factory)
    catalogue, _, paper_uuid = _loaded_catalogue(root)
    source = root / "Inbox" / f"{paper_uuid}__data03.xlsx"
    source.write_bytes(b"not a real workbook")
    calls: list[Path] = []

    def hash_only(path: Path) -> str:
        calls.append(path)
        return "a" * 64

    monkeypatch.setattr(
        "lam.services.supplementary_registration_service.full_hash", hash_only
    )
    service = SupplementaryRegistrationService(root, catalogue)
    plan = service.plan_known_uuid_supplementaries(service.scan_inbox())[0]

    assert calls == [source]
    assert plan.sha256 == "a" * 64
    assert plan.target_filename.endswith(" - Data03.xlsx")


def test_existing_document_conflicts_are_all_classified(library_factory):
    root = _root_with_paper(library_factory)
    catalogue, _, paper_uuid = _loaded_catalogue(root)
    content = b"duplicate"
    digest = hashlib.sha256(content).hexdigest()
    target_name = "Test J, 2025 - Coordinated Paper - Figure02.pdf"
    catalogue.add_document(
        {
            "document_id": f"{paper_uuid}:supp:figure:02",
            "paper_uuid": paper_uuid,
            "document_type": "supplementary",
            "supplementary_type": "Figure",
            "sequence": 2,
            "filename": "old.pdf",
            "relative_path": "Registered/old.pdf",
            "extension": ".pdf",
            "sha256": digest,
            "file_status": "registered",
        }
    )
    (root / "Registered" / target_name).write_bytes(b"target")
    source = root / "Inbox" / f"{paper_uuid}__figure02.pdf"
    source.write_bytes(content)

    plan = SupplementaryRegistrationService(
        root, catalogue
    ).plan_known_uuid_supplementary(
        SupplementaryRegistrationService(root, catalogue).scan_inbox()
    )[0]

    assert set(plan.conflicts) == {
        DUPLICATE_FILE,
        DOCUMENT_ID_CONFLICT,
        SEQUENCE_CONFLICT,
        TARGET_COLLISION,
    }
    assert plan.ready is False


def test_same_stem_batch_detects_in_run_document_slot_and_target_conflicts(
    library_factory,
):
    root = _root_with_paper(library_factory)
    catalogue, paper, _ = _loaded_catalogue(root, with_main=False)
    (root / "Inbox" / "batch.pdf").write_bytes(b"main")
    (root / "Inbox" / "batch_fig1.pdf").write_bytes(b"figure one")
    (root / "Inbox" / "batch_figure01.pdf").write_bytes(b"figure two")
    service = SupplementaryRegistrationService(root, catalogue)
    group = service.scan_inbox().same_stem_groups[0]

    plans = service.plan_same_stem_group(group, paper)

    assert len(plans) == 2
    for plan in plans:
        assert DOCUMENT_ID_CONFLICT in plan.conflicts
        assert SEQUENCE_CONFLICT in plan.conflicts
        assert TARGET_COLLISION in plan.conflicts
        assert MAIN_DOCUMENT_MISSING not in plan.conflicts


def test_direct_item_plan_requires_existing_or_expected_main(library_factory):
    root = _root_with_paper(library_factory)
    catalogue, paper, paper_uuid = _loaded_catalogue(root, with_main=False)
    source = root / "Inbox" / f"{paper_uuid}__supp.pdf"
    source.write_bytes(b"supp")
    service = SupplementaryRegistrationService(root, catalogue)
    item = service.scan_inbox().uuid_supplementary[0]

    blocked = service.plan_item(item, paper)
    allowed = service.plan_item(item, paper, main_document_expected=True)

    assert MAIN_DOCUMENT_MISSING in blocked.conflicts
    assert MAIN_DOCUMENT_MISSING not in allowed.conflicts


def test_unsequenced_generic_supplement_does_not_share_the_main_slot(library_factory):
    root = _root_with_paper(library_factory)
    catalogue, paper, paper_uuid = _loaded_catalogue(root)
    source = root / "Inbox" / f"{paper_uuid}__supp.pdf"
    source.write_bytes(b"generic supplement")
    service = SupplementaryRegistrationService(root, catalogue)

    plan = service.plan_item(service.scan_inbox().uuid_supplementary[0], paper)

    assert plan.document_id == f"{paper_uuid}:supp:generic:01"
    assert SEQUENCE_CONFLICT not in plan.conflicts
    assert plan.ready is True


def test_plan_rejects_binding_to_a_different_catalogue_paper(library_factory):
    root = _root_with_paper(library_factory)
    catalogue, paper, paper_uuid = _loaded_catalogue(root)
    source = root / "Inbox" / f"{paper_uuid}__methods.pdf"
    source.write_bytes(b"methods")
    item = SupplementaryRegistrationService(root, catalogue).scan_inbox().uuid_supplementary[0]
    other_uuid = "00000000-0000-4000-8000-000000000002"
    other = catalogue.add_record(
        {
            "id": "PMID:2",
            "paper_uuid": other_uuid,
            "title": "Other Paper",
            "year": "2025",
            "journal": "Other Journal",
        }
    )

    plan = SupplementaryRegistrationService(root, catalogue).plan_item(item, other)

    assert UUID_NOT_FOUND not in plan.conflicts
    assert BINDING_AMBIGUOUS in plan.conflicts
