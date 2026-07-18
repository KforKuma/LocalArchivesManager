from __future__ import annotations

import pytest

from lam.config import Settings
from lam.models import DownloadCandidate
from lam.services.download_service import DownloadService


@pytest.mark.live_download
def test_one_fixed_public_arxiv_pdf_download(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    candidate = DownloadCandidate(
        provider="arxiv",
        source_url="https://arxiv.org/pdf/1706.03762",
        expected_arxiv_id="1706.03762",
        is_direct_pdf=True,
        priority=10,
    )
    service = DownloadService(settings)
    result = service.execute(service.plan(candidate, run_id="live-download"))
    assert result.status in {"downloaded", "already_present"}
    assert result.validation and result.validation.identity_status == "verified"
