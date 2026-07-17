from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from ..config import OcrConfig, Settings
from ..models import (
    IdentifierCandidate,
    OcrAvailability,
    OcrInspection,
    OcrTextBlock,
    TitleCandidate,
)
from ..utils.identifiers import extract_doi_candidates, extract_pmid_candidates
from ..utils.text import normalize_title, title_candidates_from_page


OCR_NOISE = re.compile(
    r"^(?:original article|research article|review|open access|received|accepted|"
    r"published|copyright|vol(?:ume)?\.?\s*\d+.*|no\.?\s*\d+.*|"
    r"第\s*\d+\s*卷.*|issue\b.*|page\b.*|p(?:p)?\.?\s*\d+.*|"
    r"issn\b.*|https?://.*|www\..*|doi\b.*|abstract|摘\s*要|"
    r"keywords?|关\s*键\s*词)$",
    re.I,
)


class OcrService:
    _reader_cache: dict[tuple[Any, ...], Any] = {}
    _reader_lock = threading.RLock()
    _gpu_failed_keys: set[tuple[Any, ...]] = set()
    _easyocr_import_available: bool | None = None

    def __init__(
        self,
        settings: Settings,
        *,
        renderer: Callable[..., Any] | None = None,
        reader_factory: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.config = settings.ocr
        self.renderer = renderer
        self.reader_factory = reader_factory

    def check_availability(
        self,
        *,
        deep: bool = False,
        initialize_models: bool = False,
    ) -> OcrAvailability:
        pdf2image_available = importlib.util.find_spec("pdf2image") is not None
        easyocr_installed = importlib.util.find_spec("easyocr") is not None
        torch_available = importlib.util.find_spec("torch") is not None
        poppler = self._poppler_executable()
        easyocr_available = easyocr_installed
        if pdf2image_available and poppler is not None and easyocr_installed:
            easyocr_available = self._probe_easyocr_import()
        cuda_available = False
        if torch_available:
            try:
                import torch

                cuda_available = bool(torch.cuda.is_available())
            except Exception:
                pass
        temporary_writable = self._temporary_directory_writable()
        status = "available"
        if not pdf2image_available:
            status = "ocr_unavailable_pdf2image"
        elif poppler is None:
            status = "ocr_unavailable_poppler"
        elif not easyocr_available:
            status = "ocr_unavailable_easyocr"
        elif not temporary_writable:
            status = "ocr_temporary_directory_unwritable"
        model_available: bool | None = None
        details: dict[str, Any] = {
            "poppler_executable": str(poppler) if poppler else None,
            "model_storage_dir": (
                str(self.config.model_storage_dir) if self.config.model_storage_dir else None
            ),
            "download_enabled": bool(initialize_models),
            "uses_network": bool(initialize_models),
            "may_download_models": bool(initialize_models),
            "easyocr_installed": easyocr_installed,
            "easyocr_import_probe": easyocr_available,
        }
        if deep and status == "available" and not initialize_models:
            # A default doctor run is a dependency probe only. Constructing an
            # EasyOCR Reader can create or alter its model directory even when
            # downloads are disabled, so model initialization is explicit.
            model_available = None
            details["reader_initialization"] = "skipped_no_model_side_effects"
        elif deep and status == "available":
            try:
                safe_config = replace(
                    self.config,
                    download_enabled=True,
                )
                _, mode = self._get_reader(safe_config)
                model_available = True
                details["reader_mode"] = mode
            except FileNotFoundError:
                model_available = False
                status = "ocr_unavailable_model_missing"
            except Exception as exc:
                model_available = False
                status = self._classify_initialization_error(exc)
                details["initialization_error"] = type(exc).__name__
        return OcrAvailability(
            available=status == "available",
            pdf2image_available=pdf2image_available,
            poppler_available=poppler is not None,
            easyocr_available=easyocr_available,
            model_available=model_available,
            torch_available=torch_available,
            cuda_available=cuda_available,
            temporary_directory_writable=temporary_writable,
            status=status,
            details=details,
        )

    def inspect_first_page(
        self,
        path: Path,
        *,
        run_id: str,
        trigger_reason: str,
        config: OcrConfig | None = None,
        cache_write: bool = True,
    ) -> OcrInspection:
        started = time.monotonic()
        cfg = config or self.config
        result = OcrInspection(
            status="not_run",
            dpi=cfg.dpi,
            languages=list(cfg.languages),
            trigger_reason=trigger_reason,
        )
        cache_path = self._cache_path(path, cfg)
        if cfg.cache_enabled and cache_path.is_file():
            cached = self._load_cache(cache_path)
            if cached is not None:
                cached.cache_hit = True
                cached.trigger_reason = trigger_reason
                cached.duration_ms = int((time.monotonic() - started) * 1000)
                return cached

        availability = self.check_availability(deep=False)
        if not availability.available:
            result.status = availability.status
            result.errors.append(availability.status)
            return self._finish(result, started)

        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id).strip(". ")[:120]
        safe_run_id = safe_run_id or "ocr-run"
        temp_root = (
            self.settings.download_temp_dir or self.settings.state_dir / "tmp"
        ) / safe_run_id / "ocr"
        temp_root.mkdir(parents=True, exist_ok=True)
        try:
            try:
                images = self._render_first_page(path, temp_root, cfg)
            except TimeoutError:
                raise
            except Exception as exc:
                result.status = "ocr_render_failed"
                result.errors.append("ocr_render_failed")
                result.warnings.append(type(exc).__name__)
                return self._finish(result, started)
            if not images:
                result.status = "ocr_render_failed"
                result.errors.append("ocr_render_failed")
                return self._finish(result, started)
            image = images[0]
            result.image_width, result.image_height = map(int, image.size)
            if result.image_width * result.image_height > cfg.max_image_pixels:
                result.status = "ocr_image_too_large"
                result.errors.append("ocr_image_too_large")
                return self._finish(result, started)

            reader, mode = self._get_reader(cfg)
            result.gpu_mode = mode
            if mode == "gpu_fallback_to_cpu":
                result.warnings.append("ocr_gpu_fallback")
            try:
                input_image = (
                    self._preprocess(image)
                    if cfg.preprocessing_mode == "grayscale_autocontrast"
                    else image
                )
                raw = self._read_image(reader, input_image)
            except Exception as exc:
                if mode == "gpu" and cfg.gpu == "auto":
                    self._gpu_failed_keys.add(self._reader_base_key(cfg))
                    reader, _ = self._get_reader(cfg, force_cpu=True)
                    result.gpu_mode = "gpu_fallback_to_cpu"
                    result.warnings.append("ocr_gpu_fallback")
                    raw = self._read_image(reader, image)
                else:
                    raise exc
            blocks = self._blocks(raw)
            if time.monotonic() - started > cfg.timeout_seconds:
                raise TimeoutError
            accepted = [item for item in blocks if item.confidence >= cfg.min_confidence]
            if (
                sum(len(item.text) for item in accepted) < cfg.min_text_chars
                and cfg.preprocessing_mode == "raw"
            ):
                preprocessed = self._preprocess(image)
                second = self._blocks(self._read_image(reader, preprocessed))
                if sum(len(item.text) for item in second) > sum(
                    len(item.text) for item in blocks
                ):
                    blocks = second
                    accepted = [
                        item for item in blocks if item.confidence >= cfg.min_confidence
                    ]
                    result.warnings.append("ocr_preprocessed_retry")
            result.raw_blocks = blocks
            result.ordered_lines = self._ordered_lines(accepted)
            result.combined_text = "\n".join(result.ordered_lines)[:30_000]
            self._extract_candidates(result, cfg)
            if not blocks:
                result.status = "ocr_no_text_detected"
                result.errors.append("ocr_no_text_detected")
            elif not accepted or not result.combined_text.strip():
                result.status = "ocr_low_confidence"
                result.errors.append("ocr_low_confidence")
            else:
                result.status = "success"
                if not result.title_candidates:
                    result.warnings.append("ocr_title_not_found")
                if len(result.doi_candidates) > 1:
                    result.warnings.append("ocr_identifier_ambiguous")
            result = self._finish(result, started)
            if cfg.cache_enabled and cache_write:
                self._save_cache(cache_path, result)
            return result
        except TimeoutError:
            result.status = "ocr_timeout"
            result.errors.append("ocr_timeout")
            return self._finish(result, started)
        except FileNotFoundError:
            result.status = "ocr_unavailable_model_missing"
            result.errors.append("ocr_unavailable_model_missing")
            return self._finish(result, started)
        except Exception as exc:
            key = (
                "ocr_render_failed"
                if type(exc).__module__.startswith("pdf2image")
                else self._classify_initialization_error(exc)
            )
            result.status = key
            result.errors.append(key)
            result.warnings.append(type(exc).__name__)
            return self._finish(result, started)
        finally:
            if not cfg.keep_debug_images:
                shutil.rmtree(temp_root, ignore_errors=True)

    def _render_first_page(self, path: Path, output_folder: Path, cfg: OcrConfig):
        renderer = self.renderer
        if renderer is None:
            from pdf2image import convert_from_path

            renderer = convert_from_path
        return renderer(
            str(path),
            dpi=cfg.dpi,
            first_page=1,
            last_page=1,
            fmt="png",
            single_file=True,
            thread_count=1,
            output_folder=str(output_folder),
            poppler_path=str(cfg.poppler_path) if cfg.poppler_path else None,
            timeout=cfg.timeout_seconds,
        )

    def _get_reader(
        self, cfg: OcrConfig | None = None, *, force_cpu: bool = False
    ) -> tuple[Any, str]:
        cfg = cfg or self.config
        base_key = self._reader_base_key(cfg)
        prior_gpu_failure = cfg.gpu == "auto" and base_key in self._gpu_failed_keys
        gpu = False if force_cpu else self._gpu_requested(cfg)
        if prior_gpu_failure:
            gpu = False
        key = (*base_key, gpu)
        with self._reader_lock:
            if key in self._reader_cache:
                mode = "gpu" if gpu else "gpu_fallback_to_cpu" if prior_gpu_failure else "cpu"
                return self._reader_cache[key], mode
            try:
                reader = self._create_reader(cfg, gpu)
            except Exception:
                if gpu and cfg.gpu == "auto" and not force_cpu:
                    self._gpu_failed_keys.add(base_key)
                    cpu_reader, _ = self._get_reader(cfg, force_cpu=True)
                    return cpu_reader, "gpu_fallback_to_cpu"
                raise
            self._reader_cache[key] = reader
            mode = "gpu" if gpu else "gpu_fallback_to_cpu" if prior_gpu_failure else "cpu"
            return reader, mode

    def _reader_base_key(self, cfg: OcrConfig) -> tuple[Any, ...]:
        factory_key: Any = self.reader_factory or "easyocr.Reader"
        try:
            hash(factory_key)
        except TypeError:
            factory_key = (type(factory_key).__qualname__, id(factory_key))
        return (
            tuple(cfg.languages),
            str(cfg.model_storage_dir or ""),
            cfg.download_enabled,
            factory_key,
        )

    def _create_reader(self, cfg: OcrConfig, gpu: bool):
        factory = self.reader_factory
        if factory is None:
            import easyocr

            factory = easyocr.Reader
        kwargs: dict[str, Any] = {
            "gpu": gpu,
            "download_enabled": cfg.download_enabled,
            "verbose": False,
        }
        if cfg.model_storage_dir:
            kwargs["model_storage_directory"] = str(cfg.model_storage_dir)
        return factory(list(cfg.languages), **kwargs)

    @staticmethod
    def _read_image(reader: Any, image: Any):
        import numpy as np

        return reader.readtext(np.asarray(image), detail=1, paragraph=False)

    @staticmethod
    def _preprocess(image: Any):
        from PIL import ImageEnhance, ImageOps

        converted = ImageOps.autocontrast(ImageOps.grayscale(image))
        return ImageEnhance.Sharpness(converted).enhance(1.15)

    @staticmethod
    def _blocks(raw: Any) -> list[OcrTextBlock]:
        blocks: list[OcrTextBlock] = []
        for item in raw or []:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            box, text, confidence = item[0], str(item[1] or "").strip(), item[2]
            if not text:
                continue
            try:
                points = [[float(point[0]), float(point[1])] for point in box]
                score = min(1.0, max(0.0, float(confidence)))
            except (TypeError, ValueError, IndexError):
                continue
            blocks.append(
                OcrTextBlock(points, text[:1000], score, normalize_title(text))
            )
        return blocks

    @staticmethod
    def _ordered_lines(blocks: list[OcrTextBlock]) -> list[str]:
        positioned = []
        for block in blocks:
            xs = [point[0] for point in block.bounding_box]
            ys = [point[1] for point in block.bounding_box]
            positioned.append(
                (sum(ys) / len(ys), min(xs), max(ys) - min(ys), block.text)
            )
        positioned.sort(key=lambda item: (item[0], item[1]))
        rows: list[list[tuple[float, str]]] = []
        row_centers: list[float] = []
        for center_y, left, height, text in positioned:
            tolerance = max(8.0, height * 0.65)
            if rows and abs(center_y - row_centers[-1]) <= tolerance:
                rows[-1].append((left, text))
                row_centers[-1] = (row_centers[-1] + center_y) / 2
            else:
                rows.append([(left, text)])
                row_centers.append(center_y)
        return [" ".join(text for _, text in sorted(row)) for row in rows]

    @staticmethod
    def _extract_candidates(result: OcrInspection, cfg: OcrConfig) -> None:
        exact_dois = extract_doi_candidates(result.combined_text, source_type="ocr")
        for item in exact_dois:
            item.confidence = "high"
        corrected_text = re.sub(
            r"\b1[Oo][,.](\d{4,9}/)", r"10.\1", result.combined_text
        )
        corrected_text = re.sub(
            r"\b10,(\d{4,9}/)", r"10.\1", corrected_text
        )
        corrected = extract_doi_candidates(corrected_text, source_type="ocr_corrected")
        exact_values = {item.value for item in exact_dois}
        for item in corrected:
            item.confidence = "medium"
        result.doi_candidates = exact_dois + [
            item for item in corrected if item.value not in exact_values
        ]
        if any(item.source_type == "ocr_corrected" for item in result.doi_candidates):
            result.warnings.append("ocr_identifier_corrected")
        result.pmid_candidates = extract_pmid_candidates(
            result.combined_text, source_type="ocr"
        )
        result.year_candidates = sorted(
            set(re.findall(r"\b(?:19|20)\d{2}\b", result.combined_text))
        )
        top_blocks = []
        for block in result.raw_blocks:
            ys = [point[1] for point in block.bounding_box]
            if not ys or max(ys) > result.image_height * 0.45:
                continue
            if (
                block.confidence < cfg.min_confidence
                or OCR_NOISE.match(block.text.strip())
                or re.search(
                    r"(?:\bv\s*ol(?:ume)?\s*\.?\s*\d+|\bn\s*o\s*\.?\s*\d+|第\s*\d+\s*卷)",
                    block.text,
                    re.I,
                )
            ):
                continue
            xs = [point[0] for point in block.bounding_box]
            top_blocks.append((sum(ys) / len(ys), min(xs), block.text))
        top_texts = [text for _, _, text in sorted(top_blocks)]
        candidates = title_candidates_from_page("\n".join(top_texts), page=1)
        for candidate in candidates:
            candidate.source_type = "ocr_page_top"
            candidate.confidence = "high" if len(candidate.value) >= 20 else "medium"
            candidate.evidence = "easyocr_first_page_top_region"
        result.title_candidates = candidates

    def _cache_path(self, path: Path, cfg: OcrConfig) -> Path:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        try:
            engine_version = importlib.metadata.version("easyocr")
        except importlib.metadata.PackageNotFoundError:
            engine_version = "unavailable"
        key = "|".join(
            (
                digest.hexdigest(),
                "1",
                "easyocr",
                engine_version,
                ",".join(cfg.languages),
                str(cfg.dpi),
                cfg.preprocessing_mode,
                str(cfg.min_confidence),
                cfg.configuration_version,
            )
        )
        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        return (self.settings.ocr_cache_dir or self.settings.state_dir / "ocr_cache") / name

    @staticmethod
    def _load_cache(path: Path) -> OcrInspection | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["raw_blocks"] = [OcrTextBlock(**item) for item in payload["raw_blocks"]]
            payload["title_candidates"] = [
                TitleCandidate(**item) for item in payload["title_candidates"]
            ]
            payload["doi_candidates"] = [
                IdentifierCandidate(**item) for item in payload["doi_candidates"]
            ]
            payload["pmid_candidates"] = [
                IdentifierCandidate(**item) for item in payload["pmid_candidates"]
            ]
            return OcrInspection(**payload)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save_cache(path: Path, result: OcrInspection) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        payload = asdict(result)
        payload["cache_hit"] = False
        payload["combined_text"] = payload["combined_text"][:30_000]
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _poppler_executable(self) -> Path | None:
        names = ("pdftoppm.exe", "pdftocairo.exe", "pdftoppm", "pdftocairo")
        if self.config.poppler_path:
            for name in names:
                candidate = self.config.poppler_path / name
                if candidate.is_file():
                    return candidate
            return None
        found = shutil.which("pdftoppm") or shutil.which("pdftocairo")
        return Path(found) if found else None

    def _temporary_directory_writable(self) -> bool:
        root = self.settings.download_temp_dir or self.settings.state_dir / "tmp"
        try:
            root.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix="ocr-check-", dir=root)
            os.close(handle)
            Path(name).unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def _probe_easyocr_import(self) -> bool:
        cached = type(self)._easyocr_import_available
        if cached is not None:
            return cached
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", "import easyocr"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(30.0, self.config.timeout_seconds),
                check=False,
                creationflags=creation_flags,
            )
            available = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            available = False
        type(self)._easyocr_import_available = available
        return available

    @staticmethod
    def _gpu_requested(cfg: OcrConfig) -> bool:
        if cfg.gpu == "false":
            return False
        if cfg.gpu == "true":
            return True
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False

    @staticmethod
    def _classify_initialization_error(exc: Exception) -> str:
        text = str(exc).casefold()
        if isinstance(exc, FileNotFoundError) or "model" in text and (
            "missing" in text or "not found" in text or "download" in text
        ):
            return "ocr_unavailable_model_missing"
        return "ocr_initialization_failed"

    @staticmethod
    def _finish(result: OcrInspection, started: float) -> OcrInspection:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
