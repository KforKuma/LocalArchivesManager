from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lam.cli import CliParserError, build_parser, main
from lam.config import Settings
from lam.http.client import HttpResult
from lam.models import CitationExportRecord, WorkflowStatus
from lam.services.citation_export_service import (
    CitationExportCache,
    ExportArtifactWriter,
    NbibSerializer,
    OfficialCitationResult,
    PubMedCitationClient,
    validate_export_artifact,
)
from lam.services.metadata_cache_service import MetadataCacheService
from lam.workflows.citation_export import CitationExportWorkflow
from lam.workflows.cleanup import CleanupWorkflow


NBIB_1 = b"""PMID- 12345678
OWN - NLM
TI  - Official title one
AU  - Smith A
DP  - 2025
JT  - Biomedical Journal
LID - 10.1000/example.1 [doi]

"""

NBIB_2 = b"""PMID- 87654321
OWN - NLM
TI  - Official title two
AU  - Jones B
DP  - 2024
JT  - Second Journal

"""

PUBMED_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345678</PMID>
<Article><ArticleTitle>Official XML title</ArticleTitle></Article></MedlineCitation>
</PubmedArticle></PubmedArticleSet>"""


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


class FakeCitationClient:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def fetch(self, pmid, **kwargs):
        self.calls.append((pmid, kwargs))
        value = self.mapping[pmid]
        return value(**kwargs) if callable(value) else value


def _http(content: bytes, status: int = 200):
    return HttpResult(status, content, {}, 1, 0, 0.0)


def _paper(index: int, **updates):
    paper_uuid = str(uuid.UUID(f"00000000-0000-4000-8000-{index:012x}"))
    values = {
        "paper_uuid": paper_uuid,
        "title": f"Paper {index}",
        "authors": "Alice Smith; Bob Jones",
        "year": "2025",
        "journal": "Biomedical Journal",
        "journal_abbrev": "Biomed J",
        "topic_folder": "Topic A",
    }
    values.update(updates)
    return values


def _document(row):
    return {
        "document_id": f"{row['paper_uuid']}:main",
        "paper_uuid": row["paper_uuid"],
        "document_type": "main",
        "filename": f"{row['paper_uuid']}.pdf",
        "relative_path": f"Topics/Topic A/{row['paper_uuid']}.pdf",
        "extension": ".pdf",
        "file_status": "filed",
    }


def _settings(root):
    base = Settings.from_root(root)
    return replace(base, pubmed=replace(base.pubmed, email="test@example.org"))


def test_pubmed_official_nbib_fetch_cache_offline_and_refresh(current_library_factory):
    root = current_library_factory()
    settings = _settings(root)
    fake = FakeHttp([_http(NBIB_1), _http(NBIB_1)])
    client = PubMedCitationClient(settings, http_client=fake)
    first = client.fetch(
        "12345678", format_name="nbib", offline=False, refresh=False, cache_write=True
    )
    cached = client.fetch(
        "12345678", format_name="nbib", offline=True, refresh=False, cache_write=True
    )
    refreshed = client.fetch(
        "12345678", format_name="nbib", offline=False, refresh=True, cache_write=True
    )
    assert first.status == "official_nbib_exported"
    assert first.doi == "10.1000/example.1"
    assert cached.status == "official_nbib_cache_hit"
    assert cached.cache_hit is True
    assert refreshed.status == "official_nbib_exported"
    assert len(fake.calls) == 2
    assert fake.calls[0][0].endswith("efetch.fcgi")
    assert fake.calls[0][1]["rettype"] == "medline"
    assert fake.calls[0][1]["retmode"] == "text"


def test_official_response_pmid_mismatch_is_rejected_and_not_cached(
    current_library_factory,
):
    root = current_library_factory()
    settings = _settings(root)
    fake = FakeHttp([_http(NBIB_2)])
    result = PubMedCitationClient(settings, http_client=fake).fetch(
        "12345678", format_name="nbib", offline=False, refresh=False, cache_write=True
    )
    assert result.status == "pubmed_record_mismatch"
    assert not settings.citation_export_cache_dir.exists()


def test_no_cache_write_and_offline_miss_do_not_persist(current_library_factory):
    root = current_library_factory()
    settings = _settings(root)
    fake = FakeHttp([_http(NBIB_1)])
    client = PubMedCitationClient(settings, http_client=fake)
    client.fetch(
        "12345678", format_name="nbib", offline=False, refresh=False, cache_write=False
    )
    miss = client.fetch(
        "12345678", format_name="nbib", offline=True, refresh=False, cache_write=False
    )
    assert miss.status == "offline_cache_miss"
    assert not settings.citation_export_cache_dir.exists()
    assert len(fake.calls) == 1


def test_all_export_merges_official_records_and_writes_individual_files(
    current_library_factory,
):
    rows = [
        _paper(1, pmid="12345678", doi="10.1000/example.1"),
        _paper(2, pmid="87654321"),
    ]
    root = current_library_factory(rows=rows, documents=[_document(row) for row in rows])
    before = hashlib.sha256((root / "catalogue.xlsx").read_bytes()).hexdigest()
    client = FakeCitationClient(
        {
            "12345678": OfficialCitationResult(
                "12345678", "nbib", "official_nbib_exported", NBIB_1, doi="10.1000/example.1"
            ),
            "87654321": OfficialCitationResult(
                "87654321", "nbib", "official_nbib_exported", NBIB_2
            ),
        }
    )
    result = CitationExportWorkflow(_settings(root), pubmed_client=client).run(
        dry_run=False, all_records=True
    )
    combined = root / "Exports" / "Zotero" / "library.nbib"
    assert result.status == WorkflowStatus.SUCCESS
    assert result.counts["official_pubmed_records"] == 2
    assert combined.read_bytes().count(b"PMID-") == 2
    assert all(
        (root / "Exports" / "Zotero" / "records" / f"{row['paper_uuid']}.nbib").is_file()
        for row in rows
    )
    assert hashlib.sha256((root / "catalogue.xlsx").read_bytes()).hexdigest() == before
    assert result.details["runs_final_check"] is False


def test_local_nbib_is_utf8_lam_marked_and_formats_multiple_values(
    current_library_factory,
):
    row = _paper(
        1,
        title="中文标题与 English title",
        authors="王小明; Alice Smith",
        abstract="第一行\n第二行",
        keywords="免疫; T cell",
        doi="https://doi.org/10.1000/LOCAL.1",
    )
    root = current_library_factory(rows=[row], documents=[_document(row)])
    result = CitationExportWorkflow(_settings(root)).run(
        dry_run=False, paper_uuid=row["paper_uuid"]
    )
    path = root / "Exports" / "Zotero" / "records" / f"{row['paper_uuid']}.nbib"
    text = path.read_text(encoding="utf-8")
    assert result.status == WorkflowStatus.SUCCESS
    assert "DB  - LAM" in text and "OWN - LAM" in text
    assert "TI  - 中文标题与 English title" in text
    assert text.count("AU  -") == 2
    assert text.count("OT  -") == 2
    assert "      第二行" in text
    assert "10.1000/local.1 [doi]" in text
    validate_export_artifact(path.read_bytes(), "nbib", 1)


def test_local_export_fills_only_blank_fields_from_exact_provider_cache(
    current_library_factory,
):
    row = _paper(
        1,
        authors="",
        journal="Catalogue Journal",
        doi="10.1000/cached",
        abstract="",
    )
    root = current_library_factory(rows=[row], documents=[_document(row)])
    settings = _settings(root)
    cache = MetadataCacheService(settings.metadata_cache_dir, settings.cache)
    cache.put(
        "unpaywall",
        "doi",
        "10.1000/cached",
        {
            "status": "found",
            "records": [
                {
                    "title": "Conflicting cached title",
                    "authors": ["Cached Author"],
                    "year": "2025",
                    "journal": "Conflicting Cached Journal",
                    "doi": "10.1000/cached",
                    "abstract": "Cached abstract",
                    "keywords": ["cached keyword"],
                }
            ],
            "errors": [],
        },
        ttl_seconds=3600,
    )
    result = CitationExportWorkflow(settings).run(
        dry_run=False, paper_uuid=row["paper_uuid"]
    )
    path = settings.zotero_exports_dir / "records" / f"{row['paper_uuid']}.nbib"
    text = path.read_text(encoding="utf-8")
    assert result.status == WorkflowStatus.SUCCESS
    assert "TI  - Paper 1" in text
    assert "JT  - Catalogue Journal" in text
    assert "AU  - Cached Author" in text
    assert "AB  - Cached abstract" in text
    assert "OT  - cached keyword" in text


def test_identical_lam_export_returns_no_changes(current_library_factory):
    row = _paper(1)
    root = current_library_factory(rows=[row], documents=[_document(row)])
    workflow = CitationExportWorkflow(_settings(root))
    first = workflow.run(dry_run=False, paper_uuid=row["paper_uuid"])
    second = workflow.run(dry_run=False, paper_uuid=row["paper_uuid"])
    assert first.status == WorkflowStatus.SUCCESS
    assert second.status == WorkflowStatus.NO_CHANGES
    assert second.counts["bytes_written"] == 0


def test_incomplete_local_record_needs_review_and_official_only_skips(
    current_library_factory,
):
    incomplete = _paper(1, authors="", journal="", journal_abbrev="")
    complete = _paper(2)
    root = current_library_factory(
        rows=[incomplete, complete],
        documents=[_document(incomplete), _document(complete)],
    )
    first = CitationExportWorkflow(_settings(root)).run(
        dry_run=False, paper_uuid=incomplete["paper_uuid"]
    )
    assert first.status == WorkflowStatus.NEEDS_REVIEW
    assert first.needs_review[0]["missing_fields"] == ["authors", "journal"]
    second = CitationExportWorkflow(_settings(root)).run(
        dry_run=False, paper_uuid=complete["paper_uuid"], official_only=True
    )
    assert second.status == WorkflowStatus.NO_CHANGES
    assert second.skipped[0]["reason"] == "official_only_without_pmid"


def test_selection_by_topic_and_uuid_and_dry_run_writes_no_exports(
    current_library_factory,
):
    first = _paper(1, topic_folder="Topic A")
    second = _paper(2, topic_folder="Topic B")
    root = current_library_factory(
        rows=[first, second], documents=[_document(first), _document(second)]
    )
    topic = CitationExportWorkflow(_settings(root)).run(
        dry_run=True, topic_folder="Topic B"
    )
    assert topic.counts["selected_records"] == 1
    assert topic.details["records"][0]["paper_uuid"] == second["paper_uuid"]
    assert not (root / "Exports").exists()
    single = CitationExportWorkflow(_settings(root)).run(
        dry_run=True, paper_uuid=first["paper_uuid"]
    )
    assert single.counts["selected_records"] == 1


def test_duplicate_pmid_blocks(
    current_library_factory,
):
    first = _paper(1, pmid="12345678")
    second = _paper(2, pmid="12345678")
    root = current_library_factory(
        rows=[first, second], documents=[_document(first), _document(second)]
    )
    blocked = CitationExportWorkflow(_settings(root), pubmed_client=FakeCitationClient({})).run(
        dry_run=True, all_records=True
    )
    assert blocked.status == WorkflowStatus.NEEDS_REVIEW
    assert blocked.needs_review[0]["issue"] == "duplicate_pmid_conflict"
    assert len(blocked.details["records"]) == 2
    assert {
        item["export_status"] for item in blocked.details["records"]
    } == {"needs_review"}


def test_equivalent_doi_is_deduplicated(current_library_factory):
    first = _paper(1, doi="10.1000/same", title="Same paper")
    second = _paper(2, doi="10.1000/same", title="Same paper")
    root = current_library_factory(
        rows=[first, second], documents=[_document(first), _document(second)]
    )
    deduped = CitationExportWorkflow(_settings(root)).run(
        dry_run=True, all_records=True
    )
    assert deduped.counts["exported_records"] == 1
    assert any(item.get("reason") == "duplicate_doi" for item in deduped.skipped)


def test_duplicate_doi_conflicting_metadata_blocks(current_library_factory):
    first = _paper(1, doi="10.1000/conflict", title="First identity")
    second = _paper(2, doi="10.1000/conflict", title="Different identity")
    root = current_library_factory(
        rows=[first, second], documents=[_document(first), _document(second)]
    )
    result = CitationExportWorkflow(_settings(root)).run(dry_run=True, all_records=True)
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert result.needs_review[0]["issue"] == "duplicate_doi_metadata_conflict"


def test_pubmed_xml_is_official_only_and_well_formed(current_library_factory):
    official = _paper(1, pmid="12345678")
    local = _paper(2)
    root = current_library_factory(
        rows=[official, local], documents=[_document(official), _document(local)]
    )
    client = FakeCitationClient(
        {
            "12345678": OfficialCitationResult(
                "12345678", "pubmed-xml", "official_xml_exported", PUBMED_XML
            )
        }
    )
    result = CitationExportWorkflow(_settings(root), pubmed_client=client).run(
        dry_run=False, all_records=True, format_name="pubmed-xml"
    )
    path = root / "Exports" / "Zotero" / "library.pubmed.xml"
    assert path.is_file()
    validate_export_artifact(path.read_bytes(), "pubmed-xml", 1)
    assert result.status == WorkflowStatus.SUCCESS
    assert any(item.get("reason") == "pubmed_xml_requires_pmid" for item in result.skipped)


def test_partial_provider_failure_has_no_false_output_path(
    current_library_factory,
):
    first = _paper(1, pmid="12345678")
    second = _paper(2, pmid="87654321")
    root = current_library_factory(
        rows=[first, second], documents=[_document(first), _document(second)]
    )
    partial_client = FakeCitationClient(
        {
            "12345678": OfficialCitationResult(
                "12345678", "nbib", "official_nbib_exported", NBIB_1
            ),
            "87654321": OfficialCitationResult(
                "87654321", "nbib", "provider_failed", error="network unavailable"
            ),
        }
    )
    partial = CitationExportWorkflow(
        _settings(root), pubmed_client=partial_client
    ).run(dry_run=False, all_records=True)
    failed_report = next(
        item for item in partial.details["records"] if item["pmid"] == "87654321"
    )
    assert partial.status == WorkflowStatus.NEEDS_REVIEW
    assert failed_report["output_path"] is None

def test_total_provider_failure_is_failed(current_library_factory):
    second = _paper(2, pmid="87654321")
    other_root = current_library_factory(rows=[second], documents=[_document(second)])
    total = CitationExportWorkflow(
        _settings(other_root),
        pubmed_client=FakeCitationClient(
            {
                "87654321": OfficialCitationResult(
                    "87654321", "nbib", "provider_failed", error="network unavailable"
                )
            }
        ),
    ).run(dry_run=False, all_records=True)
    assert total.status == WorkflowStatus.FAILED


def test_non_lam_target_is_never_overwritten(current_library_factory):
    row = _paper(1)
    root = current_library_factory(rows=[row], documents=[_document(row)])
    output = root / "custom.nbib"
    output.write_text("user content", encoding="utf-8")
    result = CitationExportWorkflow(_settings(root)).run(
        dry_run=False, paper_uuid=row["paper_uuid"], output=output
    )
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert output.read_text(encoding="utf-8") == "user content"
    assert result.needs_review[-1]["issue"] == "export_target_collision"


def test_atomic_commit_failure_leaves_no_partial_export(tmp_path, monkeypatch):
    record = CitationExportRecord(
        paper_uuid=str(uuid.uuid4()),
        title="Title",
        authors=["Author"],
        year="2025",
        journal="Journal",
    )
    content = NbibSerializer.serialize(record)
    target = tmp_path / "library.nbib"
    plan = ExportArtifactWriter.plan(
        target, content, format_name="nbib", record_count=1
    )
    real_replace = os.replace

    def fail_manifest(source, destination):
        if str(destination).endswith(".lam-export.json"):
            raise OSError("simulated manifest failure")
        return real_replace(source, destination)

    monkeypatch.setattr("lam.services.citation_export_service.os.replace", fail_manifest)
    with pytest.raises(Exception):
        ExportArtifactWriter.commit(plan)
    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_cleanup_only_removes_stale_export_temporary_files(
    current_library_factory,
):
    root = current_library_factory()
    settings = Settings.from_root(root)
    exports = settings.zotero_exports_dir
    exports.mkdir(parents=True)
    formal = exports / "library.nbib"
    manifest = exports / "library.nbib.lam-export.json"
    temporary = exports / ".library.nbib.failed.tmp"
    formal.write_text("keep", encoding="utf-8")
    manifest.write_text("keep", encoding="utf-8")
    temporary.write_text("remove", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(temporary, (old, old))
    CleanupWorkflow(settings).run(dry_run=False)
    assert formal.exists() and manifest.exists()
    assert not temporary.exists()


def test_cli_selector_modes_and_stable_json(
    current_library_factory, capsys, monkeypatch
):
    parser = build_parser()
    with pytest.raises(CliParserError):
        parser.parse_args(["export", "zotero", "--apply"])
    with pytest.raises(CliParserError):
        parser.parse_args(
            ["export", "zotero", "--all", "--paper-uuid", "x", "--apply"]
        )
    row = _paper(1)
    root = current_library_factory(rows=[row], documents=[_document(row)])
    code = main(
        [
            "--root",
            str(root),
            "--json",
            "export",
            "zotero",
            "--paper-uuid",
            row["paper_uuid"],
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out.strip()
    assert output.count("\n") == 0
    payload = json.loads(output)
    assert code == 0
    assert payload["command"] == "export zotero"
    assert payload["canonical_command"] == "export zotero"
    assert payload["details"]["details"]["runs_final_check"] is False
