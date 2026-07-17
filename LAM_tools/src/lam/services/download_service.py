from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..config import Settings
from ..models import DownloadCandidate, DownloadPlan, DownloadResult
from ..utils.filename import sanitize_filename
from .download_validation_service import DownloadValidationService
from .run_workspace import RunWorkspace


StageCallback = Callable[[str, dict[str, object]], None]


class UnsafeDownloadUrl(ValueError):
    pass


class DownloadService:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        resolver: Callable[..., object] | None = None,
        validation_service: DownloadValidationService | None = None,
    ):
        self.settings = settings
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self.resolver = resolver or socket.getaddrinfo
        self.validation = validation_service or DownloadValidationService()
        self._current_result: DownloadResult | None = None

    @staticmethod
    def safe_url(url: str) -> str:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))
        except ValueError:
            return "[invalid URL]"

    def select_candidate(
        self,
        candidates: Iterable[DownloadCandidate],
        *,
        source: str = "auto",
    ) -> DownloadCandidate | None:
        eligible = [
            item
            for item in candidates
            if item.is_direct_pdf
            and item.provider in {"arxiv", "unpaywall", "crossref"}
            and (source == "auto" or item.provider == source)
        ]
        provider_order = {"arxiv": 0, "unpaywall": 1, "crossref": 2}
        return min(
            eligible,
            key=lambda item: (provider_order[item.provider], item.priority),
            default=None,
        )

    def plan(
        self,
        candidate: DownloadCandidate,
        *,
        run_id: str,
        max_bytes: int | None = None,
        timeout_seconds: float | None = None,
        final_directory: Path | None = None,
        target_filename: str | None = None,
    ) -> DownloadPlan:
        self._validate_url(candidate.source_url, candidate, resolve=False)
        identifier = (
            candidate.expected_arxiv_id
            or candidate.expected_doi
            or hashlib.sha256(candidate.source_url.encode("utf-8")).hexdigest()[:16]
        )
        generated = target_filename or f"download_{candidate.provider}_{identifier}.pdf"
        filename = sanitize_filename(generated, self.settings.max_filename_length)
        if target_filename and filename != target_filename:
            raise ValueError("download_target_filename_is_not_safe")
        temp_root = self.settings.download_temp_dir or (
            self.settings.state_dir / "tmp"
        )
        temporary_path = temp_root / run_id / f"{filename}.part"
        destination = (final_directory or self.settings.inbox_dir).resolve()
        if destination not in {
            self.settings.inbox_dir.resolve(),
            self.settings.registered_dir.resolve(),
        }:
            raise ValueError("unsafe_download_destination")
        final_path = destination / filename
        self._require_direct_child(temporary_path, temp_root / run_id)
        self._require_direct_child(final_path, destination)
        return DownloadPlan(
            run_id=run_id,
            candidate=candidate,
            target_filename=filename,
            temporary_path=temporary_path,
            final_path=final_path,
            max_bytes=max_bytes or self.settings.download.max_bytes,
            timeout_seconds=timeout_seconds or self.settings.download.timeout_seconds,
            target_existed_at_plan=final_path.exists(),
        )

    def execute(
        self,
        plan: DownloadPlan,
        *,
        stage_callback: StageCallback | None = None,
    ) -> DownloadResult:
        self._current_result = None
        callback = stage_callback or (lambda _stage, _details: None)
        workspace = RunWorkspace.create(
            self.settings,
            run_id=plan.run_id,
            workflow="download",
            artifact_type="download_partial",
            cleanup_policy="retain_on_failure" if self.settings.keep_failed_temp else "immediate",
        )
        plan.temporary_path = workspace.path / f"{plan.target_filename}.part"
        part = plan.temporary_path
        bytes_downloaded = 0
        content_type = ""
        outcome_status = "failed"
        callback("download_started", {"url": self.safe_url(plan.candidate.source_url)})
        try:
            current_url = plan.candidate.source_url
            for redirect_count in range(self.settings.download.max_redirects + 1):
                self._validate_url(current_url, plan.candidate)
                with self.client.stream(
                    "GET",
                    current_url,
                    timeout=plan.timeout_seconds,
                    headers={"Accept": "application/pdf,*/*;q=0.1"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise ValueError("redirect_without_location")
                        if redirect_count >= self.settings.download.max_redirects:
                            raise ValueError("too_many_redirects")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ValueError(f"http_status_{response.status_code}")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    raw_length = response.headers.get("content-length", "").strip()
                    content_length = None
                    if raw_length:
                        try:
                            content_length = int(raw_length)
                        except ValueError as exc:
                            raise ValueError("invalid_content_length") from exc
                        if content_length > plan.max_bytes:
                            raise ValueError("download_too_large")
                    with part.open("xb") as handle:
                        for chunk in response.iter_bytes(self.settings.download.chunk_size):
                            if not chunk:
                                continue
                            bytes_downloaded += len(chunk)
                            if bytes_downloaded > plan.max_bytes:
                                raise ValueError("download_too_large")
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if content_length is not None and bytes_downloaded != content_length:
                        raise ValueError("content_length_mismatch")
                    break
            else:  # pragma: no cover - bounded loop always exits explicitly
                raise ValueError("too_many_redirects")

            callback(
                "temporary_file_written",
                {"bytes": bytes_downloaded, "content_type": content_type},
            )
            inspection = self.validation.inspect(
                part,
                plan.candidate,
                verify_identifiers=self.settings.download.verify_identifiers,
            )
            if not inspection.valid:
                return self._capture_result(self._failed_result(
                    "validation_failed",
                    plan,
                    part,
                    bytes_downloaded,
                    content_type,
                    inspection=inspection,
                    error=",".join(inspection.reasons),
                ))
            callback(
                "validation_passed",
                {"pages": inspection.page_count, "identity": inspection.identity_status},
            )
            fingerprint = self._fingerprint(part)
            if plan.final_path.exists():
                if self._fingerprint(plan.final_path) == fingerprint:
                    outcome_status = "already_present"
                    return self._capture_result(DownloadResult(
                        "already_present",
                        plan,
                        bytes_downloaded,
                        content_type,
                        fingerprint,
                        inspection,
                        str(plan.final_path),
                    ))
                return self._capture_result(self._failed_result(
                    "target_collision",
                    plan,
                    part,
                    bytes_downloaded,
                    content_type,
                    fingerprint=fingerprint,
                    inspection=inspection,
                    error="target_exists_with_different_content",
                ))
            try:
                self._commit_no_replace(part, plan.final_path)
            except FileExistsError:
                if self._fingerprint(plan.final_path) == fingerprint:
                    outcome_status = "already_present"
                    return self._capture_result(DownloadResult(
                        "already_present",
                        plan,
                        bytes_downloaded,
                        content_type,
                        fingerprint,
                        inspection,
                        str(plan.final_path),
                    ))
                return self._capture_result(self._failed_result(
                    "target_collision",
                    plan,
                    part,
                    bytes_downloaded,
                    content_type,
                    fingerprint=fingerprint,
                    inspection=inspection,
                    error="target_appeared_with_different_content",
                ))
            destination = plan.final_path.parent.name.casefold()
            callback(
                "committed_to_registered" if destination == "registered" else "committed_to_inbox",
                {
                    "path": plan.final_path.relative_to(
                        self.settings.library_root
                    ).as_posix()
                },
            )
            outcome_status = "downloaded"
            return self._capture_result(DownloadResult(
                "downloaded",
                plan,
                bytes_downloaded,
                content_type,
                fingerprint,
                inspection,
                str(plan.final_path),
            ))
        except (httpx.HTTPError, OSError, ValueError, UnsafeDownloadUrl) as exc:
            safe_error = (
                type(exc).__name__
                if isinstance(exc, httpx.HTTPError)
                else (str(exc) or type(exc).__name__)
            )
            return self._capture_result(self._failed_result(
                "download_failed",
                plan,
                part,
                bytes_downloaded,
                content_type,
                error=safe_error,
            ))
        finally:
            cleanup = workspace.cleanup(
                status=outcome_status,
                retain=(
                    outcome_status not in {"downloaded", "already_present"}
                    and (
                        self.settings.keep_failed_temp
                        or self.settings.download.keep_failed_parts
                    )
                ),
            )
            if cleanup.error and self._current_result is not None:
                self._current_result.cleanup_error = cleanup.error
                if not self._current_result.error:
                    self._current_result.error = "temporary_cleanup_failed"

    def _validate_url(
        self,
        url: str,
        candidate: DownloadCandidate,
        *,
        resolve: bool = True,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise UnsafeDownloadUrl("unsafe_download_url")
        host = parts.hostname.rstrip(".").casefold()
        if candidate.provider == "arxiv" and not (host == "arxiv.org" or host.endswith(".arxiv.org")):
            raise UnsafeDownloadUrl("arxiv_redirected_official_host")
        if not resolve:
            return
        try:
            addresses = self.resolver(host, parts.port or (443 if parts.scheme == "https" else 80))
        except OSError as exc:
            raise UnsafeDownloadUrl("download_host_resolution_failed") from exc
        found = False
        for item in addresses:
            address = item[4][0]
            ip = ipaddress.ip_address(address)
            found = True
            if not ip.is_global:
                raise UnsafeDownloadUrl("download_host_is_not_public")
        if not found:
            raise UnsafeDownloadUrl("download_host_resolution_failed")

    @staticmethod
    def _require_direct_child(path: Path, parent: Path) -> None:
        if path.parent.resolve() != parent.resolve():
            raise ValueError("unsafe_download_path")

    @staticmethod
    def _fingerprint(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _commit_no_replace(part: Path, final: Path) -> None:
        os.link(part, final)
        part.unlink()

    def _failed_result(
        self,
        status: str,
        plan: DownloadPlan,
        part: Path,
        bytes_downloaded: int,
        content_type: str,
        *,
        fingerprint: str = "",
        inspection=None,
        error: str = "",
    ) -> DownloadResult:
        return DownloadResult(
            status,
            plan,
            bytes_downloaded,
            content_type,
            fingerprint,
            inspection,
            None,
            error,
        )

    def _capture_result(self, result: DownloadResult) -> DownloadResult:
        self._current_result = result
        return result
