from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import socket

import httpx
import pytest

from lam.config import Settings
from lam.models import DownloadCandidate
from lam.services.download_service import DownloadService

from conftest import write_text_pdf


def public_resolver(*_args):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def private_redirect_resolver(host, *_args):
    address = "127.0.0.1" if host == "private.example" else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def pdf_bytes(tmp_path: Path, text: str) -> bytes:
    path = tmp_path / "fixture.pdf"
    write_text_pdf(path, [text], metadata={"/Title": text})
    return path.read_bytes()


def service_for(root: Path, handler, *, resolver=public_resolver, max_bytes=None):
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    if max_bytes is not None:
        settings = replace(settings, download=replace(settings.download, max_bytes=max_bytes))
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return settings, DownloadService(settings, client=client, resolver=resolver)


def arxiv_candidate() -> DownloadCandidate:
    return DownloadCandidate(
        provider="arxiv",
        source_url="https://arxiv.org/pdf/2401.12345",
        expected_arxiv_id="2401.12345",
        is_direct_pdf=True,
        priority=10,
    )


def unpaywall_candidate() -> DownloadCandidate:
    return DownloadCandidate(
        provider="unpaywall",
        source_url="https://repository.example/paper.pdf?token=secret",
        expected_doi="10.1000/test.1",
        is_direct_pdf=True,
        priority=20,
    )


def test_arxiv_download_validates_and_commits_only_to_inbox(library_factory, tmp_path):
    root = library_factory([])
    payload = pdf_bytes(tmp_path, "arXiv:2401.12345")
    settings, service = service_for(
        root, lambda request: httpx.Response(200, content=payload, headers={"content-type": "text/plain"})
    )
    plan = service.plan(arxiv_candidate(), run_id="run-1")
    result = service.execute(plan)
    assert result.status == "downloaded"
    assert plan.final_path.parent == settings.inbox_dir
    assert plan.final_path.read_bytes() == payload
    assert not plan.temporary_path.exists()
    assert not list(settings.registered_dir.glob("*.pdf"))
    assert result.validation.identity_status == "verified"


def test_unpaywall_explicit_pdf_accepts_wrong_content_type_when_content_is_pdf(library_factory, tmp_path):
    root = library_factory([])
    payload = pdf_bytes(tmp_path, "doi:10.1000/test.1")
    _, service = service_for(
        root, lambda request: httpx.Response(200, content=payload, headers={"content-type": "application/octet-stream"})
    )
    result = service.execute(service.plan(unpaywall_candidate(), run_id="run-2"))
    assert result.status == "downloaded"
    assert result.validation.identity_status == "verified"
    assert service.safe_url(unpaywall_candidate().source_url).endswith("paper.pdf")
    assert "token" not in service.safe_url(unpaywall_candidate().source_url)


def test_html_payload_is_rejected_even_with_pdf_content_type(library_factory):
    root = library_factory([])
    _, service = service_for(
        root,
        lambda request: httpx.Response(
            200, content=b"<!doctype html><html>login</html>", headers={"content-type": "application/pdf"}
        ),
    )
    plan = service.plan(unpaywall_candidate(), run_id="html")
    result = service.execute(plan)
    assert result.status == "validation_failed"
    assert "non_pdf_payload" in result.validation.reasons
    assert not plan.final_path.exists()
    assert not plan.temporary_path.exists()


def test_size_limit_rejects_content_length_and_writes_no_part(library_factory):
    root = library_factory([])
    _, service = service_for(
        root,
        lambda request: httpx.Response(200, content=b"x" * 20, headers={"content-length": "20"}),
        max_bytes=10,
    )
    plan = service.plan(unpaywall_candidate(), run_id="large")
    result = service.execute(plan)
    assert result.status == "download_failed"
    assert result.error == "download_too_large"
    assert not plan.temporary_path.exists()


def test_streaming_limit_and_interrupted_transfer_clean_partial_file(library_factory):
    root = library_factory([])

    class TooLargeStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.4\n"
            yield b"x" * 100

    _, service = service_for(
        root, lambda request: httpx.Response(200, stream=TooLargeStream()), max_bytes=20
    )
    plan = service.plan(unpaywall_candidate(), run_id="stream-large")
    result = service.execute(plan)
    assert result.status == "download_failed"
    assert result.error == "download_too_large"
    assert not plan.temporary_path.exists()

    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.4\n"
            raise httpx.ReadError("connection interrupted")

    _, service = service_for(
        root, lambda request: httpx.Response(200, stream=InterruptedStream())
    )
    plan = service.plan(unpaywall_candidate(), run_id="interrupted")
    interrupted = service.execute(plan)
    assert interrupted.status == "download_failed"
    assert not plan.temporary_path.exists()


def test_private_redirect_is_blocked_before_following(library_factory):
    root = library_factory([])
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://private.example/file.pdf"})

    _, service = service_for(root, handler, resolver=private_redirect_resolver)
    plan = service.plan(unpaywall_candidate(), run_id="redirect")
    result = service.execute(plan)
    assert result.status == "download_failed"
    assert result.error == "download_host_is_not_public"
    assert len(calls) == 1


def test_one_public_redirect_is_followed_and_revalidated(library_factory, tmp_path):
    root = library_factory([])
    payload = pdf_bytes(tmp_path, "doi:10.1000/test.1")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.example/final"})
        return httpx.Response(200, content=payload)

    _, service = service_for(root, handler)
    result = service.execute(service.plan(unpaywall_candidate(), run_id="public-redirect"))
    assert result.status == "downloaded"
    assert calls == [
        "https://repository.example/paper.pdf?token=secret",
        "https://cdn.example/final",
    ]


def test_identity_mismatch_and_zero_page_pdf_are_rejected(library_factory, tmp_path):
    root = library_factory([])
    wrong = pdf_bytes(tmp_path, "arXiv:2401.99999")
    _, service = service_for(root, lambda request: httpx.Response(200, content=wrong))
    mismatch = service.execute(service.plan(arxiv_candidate(), run_id="wrong-id"))
    assert mismatch.status == "validation_failed"
    assert "identity_mismatch" in mismatch.validation.reasons

    from pypdf import PdfWriter

    empty_path = tmp_path / "zero.pdf"
    with empty_path.open("wb") as handle:
        PdfWriter().write(handle)
    empty = empty_path.read_bytes()
    _, service = service_for(root, lambda request: httpx.Response(200, content=empty))
    zero = service.execute(service.plan(arxiv_candidate(), run_id="zero"))
    assert zero.status == "validation_failed"
    assert "pdf_has_no_pages" in zero.validation.reasons

    _, service = service_for(root, lambda request: httpx.Response(200, content=b"%PDF-1.4\nbroken"))
    damaged = service.execute(service.plan(arxiv_candidate(), run_id="damaged"))
    assert damaged.status == "validation_failed"
    assert any(reason.startswith("pdf_unreadable:") for reason in damaged.validation.reasons)


def test_existing_target_same_content_is_idempotent_and_different_content_blocks(library_factory, tmp_path):
    root = library_factory([])
    payload = pdf_bytes(tmp_path, "arXiv:2401.12345")
    _, service = service_for(root, lambda request: httpx.Response(200, content=payload))
    plan = service.plan(arxiv_candidate(), run_id="same")
    plan.final_path.write_bytes(payload)
    same = service.execute(plan)
    assert same.status == "already_present"

    plan = service.plan(arxiv_candidate(), run_id="different")
    plan.final_path.write_bytes(b"different")
    different = service.execute(plan)
    assert different.status == "target_collision"
    assert plan.final_path.read_bytes() == b"different"
    assert not plan.temporary_path.exists()


def test_target_race_never_overwrites_new_file(library_factory, tmp_path, monkeypatch):
    root = library_factory([])
    payload = pdf_bytes(tmp_path, "arXiv:2401.12345")
    _, service = service_for(root, lambda request: httpx.Response(200, content=payload))
    plan = service.plan(arxiv_candidate(), run_id="race")

    def target_appears(part, final):
        final.write_bytes(b"new competing file")
        raise FileExistsError

    monkeypatch.setattr(service, "_commit_no_replace", target_appears)
    result = service.execute(plan)
    assert result.status == "target_collision"
    assert plan.final_path.read_bytes() == b"new competing file"
    assert not plan.temporary_path.exists()


def test_plan_does_not_make_pdf_request_or_create_temp_file(library_factory):
    root = library_factory([])
    calls = []
    _, service = service_for(
        root,
        lambda request: calls.append(request) or httpx.Response(500),
        resolver=lambda *_args: (_ for _ in ()).throw(AssertionError("DNS should not run")),
    )
    plan = service.plan(unpaywall_candidate(), run_id="dry")
    assert calls == []
    assert not plan.temporary_path.exists()
    assert not plan.temporary_path.parent.exists()


def test_candidate_selection_prefers_arxiv_then_explicit_unpaywall(library_factory):
    root = library_factory([])
    _, service = service_for(root, lambda request: pytest.fail("no request expected"))
    arxiv = arxiv_candidate()
    unpaywall = unpaywall_candidate()
    assert service.select_candidate([unpaywall, arxiv]).provider == "arxiv"
    assert service.select_candidate([arxiv, unpaywall], source="unpaywall").provider == "unpaywall"
    assert service.select_candidate([], source="auto") is None
