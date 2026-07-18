from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from lam.services.reference_text_service import ReferenceTextParser


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples" / "test_corpus"
REQUIRED = {
    "case_id",
    "filename",
    "source_type",
    "purpose",
    "doi",
    "pmid",
    "arxiv_id",
    "download_url",
    "sha256",
    "redistribution",
    "expected_behavior",
    "notes",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch_module():
    path = ROOT / "scripts" / "fetch_test_corpus.py"
    spec = importlib.util.spec_from_file_location("lam_fetch_test_corpus", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_has_complete_unique_cases_and_verified_included_hashes():
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert json.loads((CORPUS / "manifest.schema.json").read_text(encoding="utf-8"))[
        "$schema"
    ].endswith("2020-12/schema")
    ids = [item["case_id"] for item in manifest["cases"]]
    assert len(ids) == len(set(ids))
    for item in manifest["cases"]:
        assert set(item) == REQUIRED
        assert item["redistribution"] in {"included", "download_only"}
        if item["redistribution"] != "included":
            continue
        parent = "included" if item["source_type"] == "synthetic_pdf" else "reference_text"
        path = CORPUS / parent / item["filename"]
        assert path.is_file()
        assert _sha256(path) == item["sha256"]


def test_pdf_fixtures_have_expected_text_layers_and_contamination():
    included = CORPUS / "included"
    native = PdfReader(included / "native_text.pdf")
    image_only = PdfReader(included / "image_only.pdf")
    screenshot = PdfReader(included / "screenshot_wrapped.pdf")
    contaminated = PdfReader(included / "contaminated_metadata.pdf")
    assert "Synthetic Signals" in (native.pages[0].extract_text() or "")
    assert (image_only.pages[0].extract_text() or "") == ""
    assert (screenshot.pages[0].extract_text() or "") == ""
    assert "Synthetic Signals" in (contaminated.pages[0].extract_text() or "")
    assert "Download PDF" in str(contaminated.metadata.title)


def test_reference_corpus_records_current_and_target_regressions():
    parser = ReferenceTextParser()
    refs1 = parser.parse_file(CORPUS / "reference_text" / "refs1.txt")
    refs2 = parser.parse_file(CORPUS / "reference_text" / "refs2.txt")
    assert refs1.recognized is True
    assert len(refs1.candidates) == 6
    assert sum(len(item.doi_candidates) for item in refs1.candidates) == 6
    assert refs2.recognized is False
    assert len(refs2.candidates) == 1


def test_fetcher_selects_only_download_only_and_verifies_hash(tmp_path, monkeypatch):
    module = _fetch_module()
    payload = b"lawful direct fixture"
    expected = hashlib.sha256(payload).hexdigest()
    cases = [
        {"case_id": "included", "redistribution": "included"},
        {
            "case_id": "remote",
            "filename": "remote.pdf",
            "redistribution": "download_only",
            "download_url": "https://example.invalid/remote.pdf",
            "sha256": expected,
        },
    ]

    class Response:
        def __init__(self):
            self.remaining = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.invalid/remote.pdf"

        def read(self, _size):
            chunk, self.remaining = self.remaining, b""
            return chunk

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    selected = module.selected_cases({"cases": cases})
    assert [item["case_id"] for item in selected] == ["remote"]
    assert module.fetch_case(selected[0], tmp_path, timeout=1) == "downloaded_verified"
    assert (tmp_path / "remote.pdf").read_bytes() == payload
    assert module.fetch_case(selected[0], tmp_path, timeout=1) == "already_present_verified"


def test_fetcher_never_overwrites_hash_mismatch(tmp_path):
    module = _fetch_module()
    target = tmp_path / "remote.pdf"
    target.write_bytes(b"existing user bytes")
    case = {
        "case_id": "remote",
        "filename": target.name,
        "download_url": "https://example.invalid/remote.pdf",
        "sha256": hashlib.sha256(b"different bytes").hexdigest(),
    }
    with pytest.raises(ValueError, match="refusing to overwrite"):
        module.fetch_case(case, tmp_path, timeout=1)
    assert target.read_bytes() == b"existing user bytes"
