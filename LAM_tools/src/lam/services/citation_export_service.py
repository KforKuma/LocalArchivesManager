from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Settings
from ..exceptions import FileOperationError, NetworkError, ProviderError
from ..http.client import HttpClient
from ..http.rate_limiter import RateLimiter
from ..http.retry import RetryPolicy
from ..models import CatalogueRecord, CitationExportRecord, MetadataRecord
from ..utils.identifiers import normalize_arxiv_id, normalize_doi, normalize_pmid
from ..utils.normalize import normalized_text


CITATION_EXPORT_RESPONSE_VERSION = "1"
MEDLINE_PMID = re.compile(r"(?m)^PMID\s*-\s*(\d{5,9})\s*$")
MEDLINE_DOI = re.compile(r"(?mi)^(?:LID|AID)\s*-\s*(.+?)\s*$")


@dataclass(slots=True)
class OfficialCitationResult:
    pmid: str
    format: str
    status: str
    content: bytes = b""
    cache_hit: bool = False
    doi: str = ""
    error: str = ""


@dataclass(slots=True)
class ExportWritePlan:
    path: Path
    content: bytes
    format: str
    record_count: int
    action: str


class CitationExportCache:
    def __init__(self, root: Path, *, ttl_seconds: int):
        self.root = root
        self.ttl_seconds = ttl_seconds

    def get(self, pmid: str, format_name: str) -> bytes | None:
        metadata_path, raw_path = self._paths(pmid, format_name)
        if not metadata_path.is_file() or not raw_path.is_file():
            return None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if payload.get("response_version") != CITATION_EXPORT_RESPONSE_VERSION:
                return None
            if payload.get("pmid") != pmid or payload.get("format") != format_name:
                return None
            expires = datetime.fromisoformat(str(payload["expires_at"]))
            if expires <= datetime.now(timezone.utc):
                return None
            content = raw_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != payload.get("sha256"):
                return None
            if not validate_official_response(content, pmid, format_name):
                return None
            return content
        except Exception:
            return None

    def put(self, pmid: str, format_name: str, content: bytes) -> None:
        if not validate_official_response(content, pmid, format_name):
            raise ProviderError("Refusing to cache an invalid PubMed citation response")
        metadata_path, raw_path = self._paths(pmid, format_name)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        payload = {
            "provider": "pubmed",
            "pmid": pmid,
            "format": format_name,
            "response_version": CITATION_EXPORT_RESPONSE_VERSION,
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=max(1, self.ttl_seconds))).isoformat(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "raw_response": raw_path.name,
        }
        metadata = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        old_raw = raw_path.read_bytes() if raw_path.is_file() else None
        old_metadata = metadata_path.read_bytes() if metadata_path.is_file() else None
        try:
            self._atomic(raw_path, content)
            self._atomic(metadata_path, metadata)
        except Exception:
            self._restore(raw_path, old_raw)
            self._restore(metadata_path, old_metadata)
            raise

    def _paths(self, pmid: str, format_name: str) -> tuple[Path, Path]:
        material = f"pubmed|{format_name}|{pmid}|{CITATION_EXPORT_RESPONSE_VERSION}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        directory = self.root / "pubmed" / format_name
        return directory / f"{digest}.json", directory / f"{digest}.raw"

    @staticmethod
    def _atomic(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _restore(cls, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            cls._atomic(path, content)


class PubMedCitationClient:
    def __init__(
        self,
        settings: Settings,
        *,
        cache: CitationExportCache | None = None,
        http_client: HttpClient | None = None,
    ):
        self.settings = settings
        self.config = settings.pubmed
        self.cache = cache or CitationExportCache(
            settings.citation_export_cache_dir,
            ttl_seconds=settings.pubmed.exact_ttl_seconds,
        )
        self.http = http_client or HttpClient(
            "pubmed",
            settings.network,
            RateLimiter(settings.pubmed.min_interval_seconds),
            RetryPolicy(settings.network.max_retries),
        )

    def fetch(
        self,
        pmid: str,
        *,
        format_name: str,
        offline: bool,
        refresh: bool,
        cache_write: bool,
    ) -> OfficialCitationResult:
        normalized = normalize_pmid(pmid)
        if not normalized:
            return OfficialCitationResult(pmid, format_name, "pubmed_record_not_found")
        if not refresh:
            cached = self.cache.get(normalized, format_name)
            if cached is not None:
                return OfficialCitationResult(
                    normalized,
                    format_name,
                    "official_nbib_cache_hit" if format_name == "nbib" else "official_xml_cache_hit",
                    content=cached,
                    cache_hit=True,
                    doi=official_response_doi(cached, format_name),
                )
        if offline:
            return OfficialCitationResult(
                normalized, format_name, "offline_cache_miss", error="No valid citation-export cache entry"
            )
        if not self.config.enabled or not self.config.email:
            return OfficialCitationResult(
                normalized,
                format_name,
                "provider_failed",
                error="PubMed is disabled or NCBI_EMAIL is missing",
            )
        try:
            response = self.http.get(
                f"{self.config.base_url}/efetch.fcgi",
                params={
                    **self._common_params(),
                    "id": normalized,
                    **(
                        {"rettype": "medline", "retmode": "text"}
                        if format_name == "nbib"
                        else {"retmode": "xml"}
                    ),
                },
            )
            if response.status_code == 404 or not response.content.strip():
                return OfficialCitationResult(
                    normalized, format_name, "pubmed_record_not_found"
                )
            if response.status_code >= 400:
                raise ProviderError(f"PubMed EFetch returned HTTP {response.status_code}")
            if not validate_official_response(response.content, normalized, format_name):
                return OfficialCitationResult(
                    normalized,
                    format_name,
                    "pubmed_record_mismatch",
                    error="Official response PMID did not match the requested PMID",
                )
            if cache_write:
                self.cache.put(normalized, format_name, response.content)
            return OfficialCitationResult(
                normalized,
                format_name,
                "official_nbib_exported" if format_name == "nbib" else "official_xml_exported",
                content=response.content,
                doi=official_response_doi(response.content, format_name),
            )
        except (NetworkError, ProviderError, OSError, ET.ParseError) as exc:
            return OfficialCitationResult(
                normalized,
                format_name,
                "provider_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _common_params(self) -> dict[str, str]:
        params = {"db": "pubmed", "tool": self.config.tool, "email": self.config.email}
        if self.config.api_key:
            params["api_key"] = self.config.api_key
        return params


class NbibSerializer:
    @classmethod
    def serialize(cls, record: CitationExportRecord) -> bytes:
        entries: list[tuple[str, str]] = [("DB", "LAM"), ("OWN", "LAM")]
        if record.pmid:
            entries.append(("PMID", record.pmid))
        entries.append(("TI", record.title))
        entries.extend(("AU", author) for author in record.authors)
        entries.extend(
            (
                ("DP", record.year),
                ("JT", record.journal),
                ("TA", record.journal_abbrev),
                ("VI", record.volume),
                ("IP", record.issue),
                ("PG", record.pages),
            )
        )
        if record.doi:
            entries.extend((tag, f"{record.doi} [doi]") for tag in ("LID", "AID"))
        entries.append(("AB", record.abstract))
        entries.extend(("OT", keyword) for keyword in record.keywords)
        entries.extend(
            (
                ("PT", record.publication_type),
                ("LA", record.language),
                ("IS", record.issn),
            )
        )
        entries.extend(("AD", affiliation) for affiliation in record.affiliations)
        lines: list[str] = []
        for tag, value in entries:
            cleaned = str(value or "").strip()
            if not cleaned:
                continue
            parts = [part.strip() for part in cleaned.splitlines() if part.strip()]
            if not parts:
                continue
            lines.append(f"{tag:<4}- {parts[0]}")
            lines.extend(f"      {part}" for part in parts[1:])
        return ("\n".join(lines) + "\n\n").encode("utf-8")


class ExportArtifactWriter:
    PRODUCER = "LAM citation export"

    @classmethod
    def plan(
        cls, path: Path, content: bytes, *, format_name: str, record_count: int
    ) -> ExportWritePlan:
        validate_export_artifact(content, format_name, record_count)
        target = path.expanduser().resolve()
        if target.exists() and target.is_dir():
            raise FileOperationError(f"Export target is a directory: {target}")
        action = "create"
        if target.is_file():
            if target.read_bytes() == content and cls._owned(target):
                action = "unchanged"
            elif cls._owned(target):
                action = "update"
            else:
                action = "collision"
        return ExportWritePlan(target, content, format_name, record_count, action)

    @classmethod
    def commit(cls, plan: ExportWritePlan) -> int:
        if plan.action == "unchanged":
            return 0
        if plan.action == "collision":
            raise FileOperationError(f"Refusing to overwrite a non-LAM export: {plan.path}")
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = cls._manifest_path(plan.path)
        digest = hashlib.sha256(plan.content).hexdigest()
        manifest = (
            json.dumps(
                {
                    "producer": cls.PRODUCER,
                    "format": plan.format,
                    "record_count": plan.record_count,
                    "sha256": digest,
                    "output": plan.path.name,
                    "written_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        output_tmp = plan.path.with_name(f".{plan.path.name}.{uuid.uuid4().hex}.tmp")
        manifest_tmp = manifest_path.with_name(
            f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
        )
        previous_output = plan.path.read_bytes() if plan.path.is_file() else None
        previous_manifest = (
            manifest_path.read_bytes() if manifest_path.is_file() else None
        )
        try:
            output_tmp.write_bytes(plan.content)
            manifest_tmp.write_bytes(manifest)
            if output_tmp.read_bytes() != plan.content:
                raise FileOperationError("Temporary export verification failed")
            os.replace(output_tmp, plan.path)
            try:
                os.replace(manifest_tmp, manifest_path)
            except Exception:
                if previous_output is None:
                    plan.path.unlink(missing_ok=True)
                else:
                    rollback = plan.path.with_name(f".{plan.path.name}.rollback.tmp")
                    rollback.write_bytes(previous_output)
                    os.replace(rollback, plan.path)
                if previous_manifest is not None:
                    rollback = manifest_path.with_name(
                        f".{manifest_path.name}.rollback.tmp"
                    )
                    rollback.write_bytes(previous_manifest)
                    os.replace(rollback, manifest_path)
                raise
        except FileOperationError:
            raise
        except Exception as exc:
            raise FileOperationError(f"Could not commit export: {plan.path}") from exc
        finally:
            output_tmp.unlink(missing_ok=True)
            manifest_tmp.unlink(missing_ok=True)
        return len(plan.content)

    @classmethod
    def commit_many(cls, plans: list[ExportWritePlan]) -> list[int]:
        snapshots = {
            plan.path: (
                plan.path.read_bytes() if plan.path.is_file() else None,
                cls._manifest_path(plan.path).read_bytes()
                if cls._manifest_path(plan.path).is_file()
                else None,
            )
            for plan in plans
        }
        committed: list[ExportWritePlan] = []
        results: list[int] = []
        try:
            for plan in plans:
                results.append(cls.commit(plan))
                committed.append(plan)
            return results
        except Exception:
            for plan in reversed(committed):
                old_output, old_manifest = snapshots[plan.path]
                cls._restore(cls._manifest_path(plan.path), old_manifest)
                cls._restore(plan.path, old_output)
            raise

    @classmethod
    def _owned(cls, path: Path) -> bool:
        manifest_path = cls._manifest_path(path)
        if not manifest_path.is_file():
            return False
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            return (
                payload.get("producer") == cls.PRODUCER
                and payload.get("output") == path.name
                and payload.get("sha256")
                == hashlib.sha256(path.read_bytes()).hexdigest()
            )
        except Exception:
            return False

    @staticmethod
    def _manifest_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.lam-export.json")

    @staticmethod
    def _restore(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def citation_record_from_catalogue(record: CatalogueRecord) -> CitationExportRecord:
    fields = (
        "title",
        "authors",
        "year",
        "journal",
        "journal_abbrev",
        "doi",
        "pmid",
        "arxiv_id",
        "abstract",
        "keywords",
        "publication_type",
        "topic_folder",
    )
    return CitationExportRecord(
        paper_uuid=str(record.get("paper_uuid") or "").strip(),
        title=str(record.get("title") or "").strip(),
        authors=_split_multi(record.get("authors")),
        year=str(record.get("year") or "").strip(),
        journal=str(record.get("journal") or "").strip(),
        journal_abbrev=str(record.get("journal_abbrev") or "").strip(),
        doi=normalize_doi(record.get("doi")),
        pmid=normalize_pmid(record.get("pmid")),
        arxiv_id=str(record.get("arxiv_id") or "").strip(),
        abstract=str(record.get("abstract") or "").strip(),
        keywords=_split_multi(record.get("keywords")),
        publication_type=str(record.get("publication_type") or "").strip(),
        topic_folder=str(record.get("topic_folder") or "").strip().replace("\\", "/"),
        provenance={field: "catalogue" for field in fields if record.get(field)},
    )


def enrich_citation_record_from_provider_cache(
    record: CitationExportRecord,
    cache_root: Path | None,
    *,
    expected_versions: dict[str, str] | None = None,
) -> CitationExportRecord:
    """Fill blank export fields from valid, exact-match provider cache records.

    This projection is deliberately read-only and conservative. Identifier matches
    outrank title/year matches, conflicting non-empty Catalogue values are preserved,
    and malformed or expired cache entries are ignored.
    """
    if cache_root is None or not cache_root.is_dir():
        return record
    candidates: list[tuple[int, str, MetadataRecord]] = []
    for path in cache_root.rglob("*.json"):
        if path.name == "daily_counts.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if expected_versions is not None and payload.get("versions") != expected_versions:
                continue
            expires = datetime.fromisoformat(str(payload["expires_at"]))
            if expires <= datetime.now(timezone.utc):
                continue
            parsed = payload.get("parsed_result")
            if not isinstance(parsed, dict):
                continue
            provider = str(payload.get("provider") or path.parent.name).strip()
            for item in parsed.get("records", []):
                if not isinstance(item, dict):
                    continue
                candidate = MetadataRecord.from_dict(item)
                score = _cache_match_score(record, candidate)
                if score:
                    candidates.append((score, provider, candidate))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    if not candidates:
        return record
    _score, provider, candidate = max(candidates, key=lambda item: item[0])
    scalar_fields = (
        "title",
        "year",
        "journal",
        "journal_abbrev",
        "doi",
        "arxiv_id",
        "abstract",
        "publication_type",
        "language",
    )
    for field_name in scalar_fields:
        current = getattr(record, field_name)
        value = getattr(candidate, field_name)
        if not current and value:
            if field_name == "doi":
                value = normalize_doi(value)
            elif field_name == "arxiv_id":
                value = normalize_arxiv_id(value, keep_version=True)
            setattr(record, field_name, str(value).strip())
            record.provenance[field_name] = f"provider_cache:{provider}"
    for field_name in ("authors", "keywords"):
        if not getattr(record, field_name):
            values = [str(value).strip() for value in getattr(candidate, field_name) if value]
            if values:
                setattr(record, field_name, list(dict.fromkeys(values)))
                record.provenance[field_name] = f"provider_cache:{provider}"
    return record


def _cache_match_score(record: CitationExportRecord, candidate: MetadataRecord) -> int:
    record_doi = normalize_doi(record.doi)
    candidate_doi = normalize_doi(candidate.doi)
    if record_doi:
        return 100 if record_doi == candidate_doi else 0
    record_arxiv = normalize_arxiv_id(record.arxiv_id)
    candidate_arxiv = normalize_arxiv_id(candidate.arxiv_id)
    if record_arxiv:
        return 90 if record_arxiv.casefold() == candidate_arxiv.casefold() else 0
    if normalized_text(record.title) != normalized_text(candidate.title):
        return 0
    if record.year and candidate.year and str(record.year) != str(candidate.year):
        return 0
    return 50 if record.title and (record.year or candidate.year) else 0


def validate_official_response(content: bytes, pmid: str, format_name: str) -> bool:
    normalized = normalize_pmid(pmid)
    if format_name == "nbib":
        values = MEDLINE_PMID.findall(content.decode("utf-8", errors="replace"))
        return values == [normalized]
    if format_name == "pubmed-xml":
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return False
        values = [
            normalize_pmid("".join(node.itertext()))
            for node in root.findall(".//MedlineCitation/PMID")
        ]
        return values == [normalized]
    return False


def official_response_doi(content: bytes, format_name: str) -> str:
    if format_name == "nbib":
        for value in MEDLINE_DOI.findall(content.decode("utf-8", errors="replace")):
            doi = normalize_doi(re.sub(r"\s*\[doi\]\s*$", "", value, flags=re.I))
            if doi:
                return doi
        return ""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return ""
    for node in root.findall(".//ArticleId") + root.findall(".//ELocationID"):
        kind = node.get("IdType") or node.get("EIdType") or ""
        if kind.casefold() == "doi":
            doi = normalize_doi("".join(node.itertext()))
            if doi:
                return doi
    return ""


def merge_pubmed_xml(contents: list[bytes]) -> bytes:
    root = ET.Element("PubmedArticleSet")
    for content in contents:
        parsed = ET.fromstring(content)
        for child in list(parsed):
            if child.tag in {"PubmedArticle", "PubmedBookArticle"}:
                root.append(child)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def validate_local_record(record: CitationExportRecord) -> list[str]:
    missing = []
    if not record.title:
        missing.append("title")
    if not record.authors:
        missing.append("authors")
    if not re.fullmatch(r"(?:19|20)\d{2}", record.year):
        missing.append("year")
    if not (record.journal or record.journal_abbrev):
        missing.append("journal")
    return missing


def validate_export_artifact(
    content: bytes, format_name: str, expected_records: int
) -> None:
    if expected_records <= 0:
        raise FileOperationError("Citation export contains no records")
    if format_name == "nbib":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileOperationError("NBIB export is not valid UTF-8") from exc
        blocks = [
            block
            for block in re.split(r"(?:\r?\n){2,}", text.strip())
            if block.strip()
        ]
        valid = all(re.search(r"(?m)^TI\s*-\s*\S", block) for block in blocks)
        if len(blocks) != expected_records or not valid:
            raise FileOperationError(
                "NBIB export validation failed: record count or title tag mismatch"
            )
        return
    if format_name == "pubmed-xml":
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise FileOperationError("PubMed XML export is not well formed") from exc
        count = len(root.findall("./PubmedArticle")) + len(
            root.findall("./PubmedBookArticle")
        )
        if root.tag != "PubmedArticleSet" or count != expected_records:
            raise FileOperationError("PubMed XML export record count mismatch")
        return
    raise FileOperationError(f"Unsupported export validation format: {format_name}")


def _split_multi(value: object) -> list[str]:
    return list(
        dict.fromkeys(
            part.strip()
            for part in re.split(r"[;\n|]+", str(value or ""))
            if part.strip()
        )
    )
