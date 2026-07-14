from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import ConfigurationError


def _load_optional_dotenv(project_root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(project_root / ".env", override=False)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    max_retries: int = 3
    max_response_bytes: int = 10 * 1024 * 1024
    user_agent: str = "LAM/0.3.2"


@dataclass(frozen=True, slots=True)
class PubMedConfig:
    enabled: bool = True
    base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    email: str = ""
    tool: str = "LAM"
    api_key: str = field(default="", repr=False)
    min_interval_seconds: float = 0.36
    batch_size: int = 100
    exact_ttl_seconds: int = 30 * 24 * 3600
    search_ttl_seconds: int = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class ArxivConfig:
    enabled: bool = True
    base_url: str = "https://export.arxiv.org/api/query"
    min_interval_seconds: float = 3.2
    exact_ttl_seconds: int = 30 * 24 * 3600
    search_ttl_seconds: int = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class UnpaywallConfig:
    enabled: bool = True
    base_url: str = "https://api.unpaywall.org/v2"
    email: str = ""
    min_interval_seconds: float = 0.25
    ttl_seconds: int = 7 * 24 * 3600
    daily_limit: int = 100_000


@dataclass(frozen=True, slots=True)
class CacheConfig:
    enabled: bool = True
    not_found_ttl_seconds: int = 24 * 3600
    cache_schema_version: str = "1"
    parser_version: str = "1"
    provider_schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    enabled: bool = True
    max_bytes: int = 150 * 1024 * 1024
    timeout_seconds: float = 120.0
    chunk_size: int = 64 * 1024
    keep_failed_parts: bool = False
    verify_identifiers: bool = True
    max_redirects: int = 5


@dataclass(frozen=True, slots=True)
class OcrConfig:
    enabled: bool = True
    languages: tuple[str, ...] = ("en",)
    dpi: int = 250
    gpu: str = "auto"
    model_storage_dir: Path | None = None
    download_enabled: bool = False
    poppler_path: Path | None = None
    max_image_pixels: int = 25_000_000
    timeout_seconds: float = 120.0
    min_text_chars: int = 80
    min_confidence: float = 0.30
    max_files_per_run: int = 25
    keep_debug_images: bool = False
    preprocessing_mode: str = "raw"
    cache_enabled: bool = True
    configuration_version: str = "1"


@dataclass(frozen=True, slots=True)
class Settings:
    library_root: Path
    project_root: Path
    catalogue_path: Path
    state_dir: Path
    reports_dir: Path
    logs_dir: Path
    inbox_dir: Path
    registered_dir: Path
    changes_log_path: Path
    lock_path: Path
    max_filename_length: int = 180
    pdf_max_pages: int = 3
    pdf_max_chars_per_page: int = 12_000
    pdf_max_total_chars: int = 30_000
    pdf_title_min_length: int = 8
    pdf_title_max_length: int = 300
    inbox_recursive: bool = False
    inspection_cache_enabled: bool = True
    metadata_lookup_enabled: bool = True
    metadata_cache_dir: Path | None = None
    network: NetworkConfig = field(default_factory=NetworkConfig)
    pubmed: PubMedConfig = field(default_factory=PubMedConfig)
    arxiv: ArxivConfig = field(default_factory=ArxivConfig)
    unpaywall: UnpaywallConfig = field(default_factory=UnpaywallConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    download_temp_dir: Path | None = None
    ocr: OcrConfig = field(default_factory=OcrConfig)
    ocr_cache_dir: Path | None = None

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        _load_optional_dotenv(project_root)
        selected = root or os.getenv("LIBRARY_ROOT") or project_root.parent
        library_root = Path(selected).expanduser().resolve()
        if not library_root.is_dir():
            raise ConfigurationError(f"Library root does not exist: {library_root}")
        catalogue = library_root / "catalogue.xlsx"
        if not catalogue.is_file():
            raise ConfigurationError(f"Required catalogue is missing: {catalogue}")
        api_key = os.getenv("NCBI_API_KEY", "").strip()
        pubmed_interval = 0.11 if api_key else 0.36
        network = NetworkConfig(
            timeout_seconds=_env_float("HTTP_TIMEOUT_SECONDS", 30.0),
            connect_timeout_seconds=_env_float("HTTP_CONNECT_TIMEOUT_SECONDS", 10.0),
            read_timeout_seconds=_env_float("HTTP_READ_TIMEOUT_SECONDS", 30.0),
            max_retries=_env_int("HTTP_MAX_RETRIES", 3),
            max_response_bytes=_env_int("HTTP_MAX_RESPONSE_BYTES", 10 * 1024 * 1024),
            user_agent=os.getenv("HTTP_USER_AGENT", "LAM/0.3.2").strip() or "LAM/0.3.2",
        )
        if network.max_retries < 0 or network.max_response_bytes <= 0:
            raise ConfigurationError("HTTP retry and response-size settings are invalid")
        pubmed = PubMedConfig(
            enabled=_env_bool("PUBMED_ENABLED", True),
            email=os.getenv("NCBI_EMAIL", "").strip(),
            tool=os.getenv("NCBI_TOOL", "LAM").strip() or "LAM",
            api_key=api_key,
            min_interval_seconds=max(
                pubmed_interval,
                _env_float("PUBMED_MIN_INTERVAL_SECONDS", pubmed_interval),
            ),
            batch_size=max(1, _env_int("PUBMED_BATCH_SIZE", 100)),
        )
        arxiv = ArxivConfig(
            enabled=_env_bool("ARXIV_ENABLED", True),
            min_interval_seconds=max(3.0, _env_float("ARXIV_DELAY_SECONDS", 3.2)),
        )
        unpaywall = UnpaywallConfig(
            enabled=_env_bool("UNPAYWALL_ENABLED", True),
            email=os.getenv("UNPAYWALL_EMAIL", "").strip(),
            min_interval_seconds=max(
                0.2, _env_float("UNPAYWALL_MIN_INTERVAL_SECONDS", 0.25)
            ),
            daily_limit=min(
                100_000, max(1, _env_int("UNPAYWALL_DAILY_LIMIT", 100_000))
            ),
        )
        download = DownloadConfig(
            enabled=_env_bool("DOWNLOAD_ENABLED", True),
            max_bytes=max(1, _env_int("DOWNLOAD_MAX_BYTES", 150 * 1024 * 1024)),
            timeout_seconds=max(1.0, _env_float("DOWNLOAD_TIMEOUT_SECONDS", 120.0)),
            chunk_size=max(4096, _env_int("DOWNLOAD_CHUNK_SIZE", 64 * 1024)),
            keep_failed_parts=_env_bool("DOWNLOAD_KEEP_FAILED_PARTS", False),
            verify_identifiers=_env_bool("DOWNLOAD_VERIFY_IDENTIFIERS", True),
            max_redirects=max(0, min(10, _env_int("DOWNLOAD_MAX_REDIRECTS", 5))),
        )
        languages = tuple(
            item.strip()
            for item in os.getenv("OCR_LANGUAGES", "en").split(",")
            if item.strip()
        ) or ("en",)
        gpu = os.getenv("OCR_GPU", "auto").strip().casefold() or "auto"
        if gpu not in {"auto", "true", "false"}:
            raise ConfigurationError("OCR_GPU must be auto, true, or false")
        model_dir = os.getenv("OCR_MODEL_STORAGE_DIR", "").strip()
        poppler_path = os.getenv("POPPLER_PATH", "").strip()
        preprocessing = os.getenv("OCR_PREPROCESSING_MODE", "raw").strip().casefold()
        if preprocessing not in {"raw", "grayscale_autocontrast"}:
            raise ConfigurationError(
                "OCR_PREPROCESSING_MODE must be raw or grayscale_autocontrast"
            )
        ocr = OcrConfig(
            enabled=_env_bool("OCR_ENABLED", True),
            languages=languages,
            dpi=max(72, min(600, _env_int("OCR_DPI", 250))),
            gpu=gpu,
            model_storage_dir=Path(model_dir).expanduser().resolve() if model_dir else None,
            download_enabled=_env_bool("OCR_DOWNLOAD_ENABLED", False),
            poppler_path=Path(poppler_path).expanduser().resolve() if poppler_path else None,
            max_image_pixels=max(1_000_000, _env_int("OCR_MAX_IMAGE_PIXELS", 25_000_000)),
            timeout_seconds=max(1.0, _env_float("OCR_TIMEOUT_SECONDS", 120.0)),
            min_text_chars=max(1, _env_int("OCR_MIN_TEXT_CHARS", 80)),
            min_confidence=min(1.0, max(0.0, _env_float("OCR_MIN_CONFIDENCE", 0.30))),
            max_files_per_run=max(1, _env_int("OCR_MAX_FILES_PER_RUN", 25)),
            keep_debug_images=_env_bool("OCR_KEEP_DEBUG_IMAGES", False),
            preprocessing_mode=preprocessing,
            cache_enabled=_env_bool("OCR_CACHE_ENABLED", True),
            configuration_version=os.getenv("OCR_CONFIGURATION_VERSION", "1").strip() or "1",
        )
        return cls(
            library_root=library_root,
            project_root=project_root,
            catalogue_path=catalogue,
            state_dir=library_root / ".library_state",
            reports_dir=library_root / ".library_state" / "reports",
            logs_dir=library_root / ".library_state" / "logs",
            inbox_dir=library_root / "Inbox",
            registered_dir=library_root / "Registered",
            changes_log_path=library_root / "library_changes.md",
            lock_path=library_root / ".library_state" / "lam.lock",
            metadata_cache_dir=library_root / ".library_state" / "metadata_cache",
            network=network,
            pubmed=pubmed,
            arxiv=arxiv,
            unpaywall=unpaywall,
            download=download,
            download_temp_dir=library_root / ".library_state" / "tmp",
            ocr=ocr,
            ocr_cache_dir=library_root / ".library_state" / "ocr_cache",
        )

    def ensure_runtime_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
