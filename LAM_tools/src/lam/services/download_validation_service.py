from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from ..models import DownloadCandidate, DownloadedFileInspection
from ..utils.identifiers import extract_doi_candidates, normalize_arxiv_id, normalize_doi


ARXIV_TEXT_PATTERN = re.compile(
    r"(?:arxiv\s*:\s*)?(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
    re.I,
)


class DownloadValidationService:
    """Bounded structural and identity checks for an untrusted PDF download."""

    def inspect(
        self,
        path: Path,
        candidate: DownloadCandidate,
        *,
        verify_identifiers: bool = True,
    ) -> DownloadedFileInspection:
        result = DownloadedFileInspection(valid=False)
        try:
            size = path.stat().st_size
            if size <= 0:
                result.reasons.append("empty_file")
                return result
            with path.open("rb") as handle:
                prefix = handle.read(1024)
            stripped = prefix.lstrip().lower()
            if stripped.startswith((b"<!doctype html", b"<html", b"<?xml", b"{", b"[")):
                result.content_kind = "html_or_json"
                result.reasons.append("non_pdf_payload")
                return result
            result.has_pdf_signature = b"%PDF-" in prefix
            if not result.has_pdf_signature:
                result.reasons.append("missing_pdf_signature")
                return result

            with path.open("rb") as pdf_stream:
                reader = PdfReader(pdf_stream, strict=False)
                if reader.is_encrypted:
                    try:
                        if reader.decrypt("") == 0:
                            result.reasons.append("pdf_encrypted")
                            return result
                    except Exception:
                        result.reasons.append("pdf_encrypted")
                        return result
                result.page_count = len(reader.pages)
                if result.page_count < 1:
                    result.reasons.append("pdf_has_no_pages")
                    return result

                result.content_kind = "pdf"
                text_parts: list[str] = []
                metadata = reader.metadata or {}
                for key in ("/Title", "/Subject", "/Author", "/Keywords"):
                    value = str(metadata.get(key) or "").strip()
                    if value:
                        text_parts.append(value[:4000])
                total_chars = sum(len(item) for item in text_parts)
                for index in range(min(result.page_count, 3)):
                    if total_chars >= 30000:
                        break
                    try:
                        page_text = (reader.pages[index].extract_text() or "")[:10000]
                    except Exception:
                        continue
                    text_parts.append(page_text)
                    total_chars += len(page_text)
            text = "\n".join(text_parts)[:30000]
            dois = sorted({item.value for item in extract_doi_candidates(text)})
            arxiv_ids = sorted(
                {
                    normalized
                    for match in ARXIV_TEXT_PATTERN.finditer(text)
                    if (normalized := normalize_arxiv_id(match.group(0)))
                }
            )
            result.identifiers_found = {"doi": dois, "arxiv": arxiv_ids}

            if not verify_identifiers:
                result.identity_status = "not_checked"
            elif candidate.provider == "arxiv" and candidate.expected_arxiv_id:
                expected = normalize_arxiv_id(candidate.expected_arxiv_id)
                if expected in arxiv_ids:
                    result.identity_status = "verified"
                else:
                    result.identity_status = "mismatch" if arxiv_ids else "unverified"
                    result.reasons.append(f"identity_{result.identity_status}")
            elif candidate.expected_doi:
                expected = normalize_doi(candidate.expected_doi)
                if expected in dois:
                    result.identity_status = "verified"
                else:
                    result.identity_status = "mismatch" if dois else "unverified"
                    result.reasons.append(f"identity_{result.identity_status}")
            else:
                result.identity_status = "unverified"
                result.reasons.append("identity_unverified")

            result.valid = not result.reasons
            return result
        except Exception as exc:
            result.reasons.append(f"pdf_unreadable:{type(exc).__name__}")
            return result
