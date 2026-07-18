from __future__ import annotations

import shutil
import socket
from pathlib import Path

import httpx
from lam.config import Settings
from lam.models import (
    MetadataLookupResult,
    MetadataLookupStatus,
    MetadataRecord,
    DownloadCandidate,
    ProviderResult,
    ProviderStatus,
)
from lam.services.catalogue_service import CatalogueService
from lam.services.download_service import DownloadService
from lam.services.reference_text_service import ReferenceTextParser
from lam.workflows.reference_import import ReferenceTextImportWorkflow
from lam.workflows.inbox_register import InboxRegisterWorkflow
from conftest import write_text_pdf


FIXTURES = Path(__file__).parent / "fixtures" / "reference_text"


class FakeMetadataService:
    def __init__(self, *, fail_title: str = ""):
        self.calls = []
        self.fail_title = fail_title

    def lookup(self, request):
        self.calls.append(request)
        if self.fail_title and self.fail_title.casefold() in str(request.title or "").casefold():
            return MetadataLookupResult(
                MetadataLookupStatus.NOT_FOUND,
                provider_results=[
                    ProviderResult(
                        "crossref",
                        ProviderStatus.NOT_FOUND,
                        "bibliographic",
                        request.title or "",
                    )
                ],
            )
        doi = request.doi or (
            "10.1234/title.only" if request.title else ""
        )
        title = request.title or {
            "10.1234/ref.one": "A deterministic reference import study",
            "10.1234/ref.two": "Safe metadata registration without local documents",
        }.get(doi, "Resolved reference title")
        record = MetadataRecord(
            canonical_id=f"DOI:{doi}",
            title=title,
            authors=["Smith J", "Doe A"],
            year=request.year or "2024",
            journal=request.journal or "Journal of Test Systems",
            journal_abbrev="J Test Syst",
            doi=doi,
            source=["crossref"],
            is_published=True,
        )
        provider = ProviderResult(
            "crossref",
            ProviderStatus.FOUND,
            "doi" if request.doi else "bibliographic",
            doi or title,
            records=[record],
        )
        return MetadataLookupResult(
            MetadataLookupStatus.FOUND,
            records=[record.to_dict()],
            best_record=record.to_dict(),
            confidence="exact_identifier" if request.doi else "exact_title_supported",
            providers_used=["crossref"],
            provider_results=[provider],
        )


def test_refs1_and_refs2_segment_multiline_and_extract_candidates():
    parser = ReferenceTextParser()
    refs1 = parser.parse_file(FIXTURES / "refs1.txt")
    refs2 = parser.parse_file(FIXTURES / "refs2.txt")
    assert refs1.recognized is True
    assert len(refs1.candidates) == 2
    assert refs1.candidates[0].doi_candidates == ["10.1234/ref.one"]
    assert refs1.candidates[1].doi_candidates == ["10.1234/ref.two"]
    assert refs2.recognized is True
    assert len(refs2.candidates) == 2
    assert "dirty multi-line reference title" in refs2.candidates[0].title_candidates[0]
    assert refs2.candidates[0].line_end > refs2.candidates[0].line_start


def test_same_paper_lookup_variants_parse_title_author_year_and_strategy():
    batch = ReferenceTextParser().parse_file(FIXTURES / "lookup_variants.txt")
    assert batch.recognized is True
    assert len(batch.candidates) == 4
    assert all(item.title_candidates for item in batch.candidates)
    full_without_doi = batch.candidates[2]
    assert full_without_doi.title_candidates[0] == "A deterministic reference import study"
    assert full_without_doi.author_candidates == ["Smith J, Doe A"]
    assert full_without_doi.year_candidates == ["2024"]
    requests = [
        ReferenceTextImportWorkflow._request(item, False, False, True)
        for item in batch.candidates
    ]
    assert all(request is not None for request in requests)
    assert all(request.title for request in requests)
    assert all(request.title != request.authors for request in requests)
    assert all(request.title != item.normalized_text for request, item in zip(requests[2:], batch.candidates[2:]))
    assert requests[0].doi is None
    assert requests[1].doi is None
    assert requests[2].doi is None
    assert requests[3].doi == "10.1234/ref.one"


def test_plain_note_is_not_treated_as_reference_list(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Remember to discuss this draft with the team.", encoding="utf-8")
    batch = ReferenceTextParser().parse_file(path)
    assert batch.recognized is False
    assert batch.warnings == ["plain_text_not_recognized_as_reference_list"]


def test_reference_apply_registers_without_documents_and_moves_completed_batch(
    current_library_factory,
):
    root = current_library_factory()
    shutil.copy2(FIXTURES / "refs1.txt", root / "Inbox" / "refs1.txt")
    service = FakeMetadataService()
    result = ReferenceTextImportWorkflow(
        Settings.from_root(root), metadata_service=service
    ).run(dry_run=False, mode="only", offline=True)
    catalogue = CatalogueService(root / "catalogue.xlsx")
    rows = catalogue.load()
    assert len(rows) == 2
    assert catalogue.documents == []
    assert {row.get("doi") for row in rows} == {
        "10.1234/ref.one",
        "10.1234/ref.two",
    }
    assert not (root / "Inbox" / "refs1.txt").exists()
    assert (root / "Imports" / "ReferenceText" / "Processed" / "refs1.txt").is_file()
    assert result.counts["registered_new"] == 2
    assert result.details["documents_created_from_text"] == 0


def test_partial_batch_receipt_retries_only_unresolved(current_library_factory):
    root = current_library_factory()
    source = root / "Inbox" / "refs2.txt"
    shutil.copy2(FIXTURES / "refs2.txt", source)
    first_service = FakeMetadataService(fail_title="Conservative matching")
    first = ReferenceTextImportWorkflow(
        Settings.from_root(root), metadata_service=first_service
    ).run(dry_run=False, mode="only", offline=True)
    assert source.is_file()
    assert first.counts["unresolved"] == 1
    assert len(first_service.calls) == 2

    second_service = FakeMetadataService()
    second = ReferenceTextImportWorkflow(
        Settings.from_root(root), metadata_service=second_service
    ).run(dry_run=False, mode="only", offline=True)
    assert len(second_service.calls) == 1
    assert second_service.calls[0].title
    assert not source.exists()
    assert second.counts["registered_new"] == 1


def test_reference_dry_run_does_not_modify_workbook_or_move_file(
    current_library_factory,
):
    root = current_library_factory()
    source = root / "Inbox" / "refs1.txt"
    shutil.copy2(FIXTURES / "refs1.txt", source)
    before = (root / "catalogue.xlsx").read_bytes()
    result = ReferenceTextImportWorkflow(
        Settings.from_root(root), metadata_service=FakeMetadataService()
    ).run(dry_run=True, mode="only", offline=True)
    assert (root / "catalogue.xlsx").read_bytes() == before
    assert source.is_file()
    assert not (root / ".library_state" / "imports").exists()
    assert result.counts["registered_new"] == 2


def test_unresolved_reference_report_contains_parser_query_cache_and_scoring_details(
    current_library_factory,
):
    root = current_library_factory()
    shutil.copy2(
        FIXTURES / "lookup_variants.txt",
        root / "Inbox" / "lookup_variants.txt",
    )

    class AmbiguousMetadata:
        def lookup(self, request):
            records = [
                MetadataRecord(
                    canonical_id=f"DOI:10.1234/candidate.{index}",
                    doi=f"10.1234/candidate.{index}",
                    title=request.title,
                    authors=["Smith J"],
                    year=request.year or "2024",
                    journal=request.journal or "Journal of Test Systems",
                    source=["crossref"],
                )
                for index in (1, 2)
            ]
            return MetadataLookupResult(
                MetadataLookupStatus.AMBIGUOUS,
                records=[item.to_dict() for item in records],
                provider_results=[
                    ProviderResult(
                        "crossref",
                        ProviderStatus.FOUND,
                        "bibliographic",
                        request.title,
                        records=records,
                        http_status=200,
                        queries_attempted=[
                            {
                                "query_type": "bibliographic",
                                "query_bibliographic": request.title,
                            }
                        ],
                    )
                ],
                conflicts=["crossref_query_ambiguous"],
                selection_reason="Multiple provider identities remain plausible.",
                candidate_evaluations=[
                    {
                        "canonical_ids": [item.canonical_id],
                        "titles": [item.title],
                        "title_score": 1.0,
                        "support_requested": ["year"],
                        "support_matches": {"year": True},
                        "accepted": True,
                        "rejection_reasons": [],
                    }
                    for item in records
                ],
            )

    result = ReferenceTextImportWorkflow(
        Settings.from_root(root), metadata_service=AmbiguousMetadata()
    ).run(dry_run=True, mode="only", offline=True, max_references=3)
    resolution = result.details["batches"][0]["resolutions"][2]
    required = {
        "raw_text",
        "normalized_text",
        "parsed_title",
        "parsed_authors",
        "parsed_year",
        "parsed_journal",
        "extracted_identifiers",
        "strategy_selected",
        "queries_attempted",
        "cache_hit",
        "provider_http_status",
        "provider_results_count",
        "top_candidates",
        "candidate_scores",
        "candidate_rejection_reasons",
        "final_resolution_reason",
    }
    assert required <= set(resolution)
    assert resolution["parsed_title"] == "A deterministic reference import study"
    assert resolution["parsed_authors"] == ["Smith J, Doe A"]
    assert resolution["parsed_year"] == "2024"
    assert resolution["strategy_selected"] == "title_bibliographic"
    assert resolution["provider_http_status"][0]["http_status"] == 200
    assert resolution["provider_results_count"] == 2
    assert len(resolution["candidate_scores"]) == 2


def test_reference_oa_download_commits_directly_to_registered_and_documents(
    current_library_factory, tmp_path
):
    root = current_library_factory()
    shutil.copy2(FIXTURES / "refs1.txt", root / "Inbox" / "refs1.txt")
    payload_path = tmp_path / "download.pdf"
    write_text_pdf(
        payload_path,
        ["doi:10.1234/ref.one\nA deterministic reference import study"],
    )
    payload = payload_path.read_bytes()

    class DownloadingMetadata(FakeMetadataService):
        def lookup(self, request):
            lookup = super().lookup(request)
            record = MetadataRecord.from_dict(lookup.best_record)
            record.download_candidates = [
                DownloadCandidate(
                    provider="crossref",
                    source_url="https://publisher.example/article.pdf",
                    expected_doi=record.doi,
                    is_direct_pdf=True,
                )
            ]
            lookup.best_record = record.to_dict()
            lookup.records = [record.to_dict()]
            lookup.provider_results[0].records = [record]
            return lookup

    settings = Settings.from_root(root)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=payload,
                headers={"content-type": "application/pdf"},
            )
        ),
        follow_redirects=False,
    )
    resolver = lambda *_args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ]
    result = ReferenceTextImportWorkflow(
        settings,
        metadata_service=DownloadingMetadata(),
        download_service=DownloadService(
            settings,
            client=client,
            resolver=resolver,
        ),
    ).run(
        dry_run=False,
        mode="only",
        max_references=1,
        download_missing=True,
        offline=True,
    )
    catalogue = CatalogueService(root / "catalogue.xlsx")
    catalogue.load()
    assert len(catalogue.records) == 1
    assert len(catalogue.documents) == 1
    document = catalogue.documents[0]
    assert document.get("file_status") == "registered"
    assert str(document.get("relative_path")).startswith("Registered/")
    assert (root / str(document.get("relative_path"))).is_file()
    assert not list((root / "Inbox").glob("*.pdf"))
    assert result.changed_files == 2  # downloaded PDF plus processed .txt batch


def test_reference_auto_and_pdf_wrapper_runs_one_final_check(
    current_library_factory, monkeypatch
):
    root = current_library_factory()
    shutil.copy2(FIXTURES / "refs1.txt", root / "Inbox" / "refs1.txt")
    from lam.workflows import progressive_register

    calls = 0
    original = progressive_register.DailyCheckWorkflow.run

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(progressive_register.DailyCheckWorkflow, "run", counted)
    result = InboxRegisterWorkflow(
        Settings.from_root(root), metadata_service=FakeMetadataService()
    ).run(
        dry_run=False,
        reference_text="auto",
        offline=True,
    )
    assert calls == 1
    assert result.counts["reference_registered_new"] == 2
    assert result.details["reference_text"]["workflow"] == "reference_text_import"
