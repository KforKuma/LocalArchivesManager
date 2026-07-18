from __future__ import annotations

from pathlib import Path

from lam.config import Settings
from lam.models import MetadataLookupResult, MetadataLookupStatus
from lam.run_context import RunContext, activate_run_context
from lam.workflows.daily_check import DailyCheckWorkflow
from lam.workflows.inbox_register import InboxRegisterWorkflow
from conftest import write_text_pdf


def test_historical_writer_entrypoints_are_absent():
    repository = Path(__file__).resolve().parents[2]
    assert not (repository / "scripts" / "search_literature.py").exists()
    assert not (repository / "main.py").exists()
    assert not (repository / "LAM_tools" / "tests" / "usertest_easyocr_function.py").exists()


def test_run_context_allows_only_one_final_check(current_library_factory):
    root = current_library_factory()
    settings = Settings.from_root(root)
    context = RunContext.create(
        caller="agent",
        library_root=root,
        dry_run=False,
        top_level_command="register",
    )
    with activate_run_context(context):
        first = DailyCheckWorkflow(settings).run(final_check=True)
        second = DailyCheckWorkflow(settings).run(final_check=True)
    assert first.details.get("skipped") != "final_check_already_claimed"
    assert second.details["skipped"] == "final_check_already_claimed"
    assert second.report_path is None


def test_register_propagates_provider_policy(current_library_factory):
    class RecordingService:
        def __init__(self):
            self.requests = []

        def lookup(self, request):
            self.requests.append(request)
            return MetadataLookupResult(MetadataLookupStatus.NOT_FOUND)

    root = current_library_factory()
    write_text_pdf(
        root / "Inbox" / "candidate.pdf",
        ["A sufficiently descriptive local paper title\nExample Author\n2025"],
    )
    service = RecordingService()
    result = InboxRegisterWorkflow(
        Settings.from_root(root), metadata_service=service
    ).run(
        dry_run=True,
        ocr_mode="never",
        offline=True,
        refresh=True,
        cache_write=False,
    )
    assert service.requests
    assert all(request.offline for request in service.requests)
    assert all(request.refresh for request in service.requests)
    assert all(not request.cache_write for request in service.requests)
    assert result.details["provider_policy"] == {
        "offline": True,
        "refresh": True,
        "cache_write": False,
    }
