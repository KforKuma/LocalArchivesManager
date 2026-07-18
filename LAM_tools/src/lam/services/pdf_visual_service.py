from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..models import PdfVisualType, VisualPdfInspection
from .run_workspace import RunWorkspace


class PdfVisualService:
    """Low-cost visual strategy selection; never changes the source PDF."""

    ANALYSIS_DPI = 72
    MAX_PAGES = 3
    CHROME_SIMILARITY = 0.965

    def __init__(
        self,
        settings: Settings,
        *,
        renderer: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.renderer = renderer
        self.last_cleanup_error = ""

    def inspect(
        self,
        path: Path,
        *,
        native_text_chars: int,
        page_count: int,
        page_image_signals: list[dict[str, Any]],
        run_id: str,
    ) -> VisualPdfInspection:
        full_page_image = any(
            bool(item.get("large_page_image")) for item in page_image_signals
        )
        images = self._render(path, page_count=page_count, run_id=run_id)
        try:
            top_similarity, bottom_similarity, compared = self._chrome_similarity(images)
        finally:
            for image in images:
                close = getattr(image, "close", None)
                if callable(close):
                    close()
        repeated_top = compared > 0 and top_similarity >= self.CHROME_SIMILARITY
        repeated_bottom = compared > 0 and bottom_similarity >= self.CHROME_SIMILARITY
        repeated_chrome = repeated_top or repeated_bottom
        if native_text_chars >= 400 and not full_page_image:
            visual_type = PdfVisualType.NATIVE_TEXT
            confidence = 0.95
        elif full_page_image and repeated_chrome:
            visual_type = PdfVisualType.SCREENSHOT_WRAPPED
            confidence = min(0.99, max(top_similarity, bottom_similarity))
        elif full_page_image:
            visual_type = PdfVisualType.SCANNED_ARTICLE
            confidence = 0.85
        elif native_text_chars >= 80:
            visual_type = PdfVisualType.NATIVE_TEXT
            confidence = 0.80
        else:
            visual_type = PdfVisualType.UNKNOWN_IMAGE
            confidence = 0.60 if images else 0.35

        top_crop = 0.10 if repeated_top and full_page_image else 0.0
        bottom_crop = 0.90 if repeated_bottom and full_page_image else 1.0
        content_crop = (0.0, top_crop, 1.0, bottom_crop)
        inspection = VisualPdfInspection(
            pdf_visual_type=visual_type,
            full_page_image_detected=full_page_image,
            repeated_chrome_detected=repeated_chrome and full_page_image,
            content_crop_applied=(top_crop > 0.0 or bottom_crop < 1.0),
            content_crop=content_crop,
            confidence=confidence,
            repeated_top_similarity=top_similarity,
            repeated_bottom_similarity=bottom_similarity,
            pages_compared=compared,
        )
        if self.last_cleanup_error:
            inspection.warnings.append("visual_temporary_cleanup_failed")
        return inspection

    def _render(self, path: Path, *, page_count: int, run_id: str) -> list[Any]:
        self.last_cleanup_error = ""
        if page_count <= 0:
            return []
        renderer = self.renderer
        if renderer is None:
            try:
                from pdf2image import convert_from_path
            except ImportError:
                return []
            renderer = convert_from_path
        workspace = RunWorkspace.create(
            self.settings,
            run_id=run_id,
            workflow="pdf-visual",
            artifact_type="production_temporary_artifact",
            cleanup_policy=(
                "debug_retention" if self.settings.ocr.keep_debug_images else "immediate"
            ),
        )
        temp = workspace.subdirectory("visual")
        rendered: list[Any] = []
        try:
            rendered = list(
                renderer(
                    str(path),
                    dpi=self.ANALYSIS_DPI,
                    first_page=1,
                    last_page=min(page_count, self.MAX_PAGES),
                    fmt="png",
                    thread_count=1,
                    output_folder=str(temp),
                    poppler_path=(
                        str(self.settings.ocr.poppler_path)
                        if self.settings.ocr.poppler_path
                        else None
                    ),
                    timeout=min(30.0, self.settings.ocr.timeout_seconds),
                )
            )
            detached: list[Any] = []
            for image in rendered:
                copy = image.copy()
                load = getattr(copy, "load", None)
                if callable(load):
                    load()
                detached.append(copy)
            return detached
        except Exception:
            return []
        finally:
            for image in rendered:
                close = getattr(image, "close", None)
                if callable(close):
                    close()
            cleanup = workspace.cleanup(
                status="completed",
                retain=self.settings.ocr.keep_debug_images,
            )
            self.last_cleanup_error = cleanup.error

    @classmethod
    def _chrome_similarity(cls, images: list[Any]) -> tuple[float, float, int]:
        if len(images) < 2:
            return 0.0, 0.0, 0
        prepared = [cls._normalized_image(image) for image in images]
        top_scores = []
        bottom_scores = []
        for left, right in zip(prepared, prepared[1:]):
            width = min(left.width, right.width)
            height = min(left.height, right.height)
            if width <= 0 or height <= 0:
                continue
            strip = max(1, int(height * 0.12))
            left_top = left.crop((0, 0, width, strip))
            right_top = right.crop((0, 0, width, strip))
            left_bottom = left.crop((0, height - strip, width, height))
            right_bottom = right.crop((0, height - strip, width, height))
            top_scores.append(
                cls._similarity(left_top, right_top)
                if cls._has_visual_content(left_top)
                and cls._has_visual_content(right_top)
                else 0.0
            )
            bottom_scores.append(
                cls._similarity(left_bottom, right_bottom)
                if cls._has_visual_content(left_bottom)
                and cls._has_visual_content(right_bottom)
                else 0.0
            )
        if not top_scores:
            return 0.0, 0.0, 0
        return (
            sum(top_scores) / len(top_scores),
            sum(bottom_scores) / len(bottom_scores),
            len(top_scores),
        )

    @staticmethod
    def _normalized_image(image: Any):
        converted = image.convert("L")
        width = 256
        height = max(1, round(converted.height * width / max(1, converted.width)))
        return converted.resize((width, height))

    @staticmethod
    def _similarity(left: Any, right: Any) -> float:
        from PIL import ImageChops, ImageStat

        difference = ImageChops.difference(left, right)
        mean = ImageStat.Stat(difference).mean[0]
        return max(0.0, min(1.0, 1.0 - mean / 255.0))

    @staticmethod
    def _has_visual_content(image: Any) -> bool:
        from PIL import ImageStat

        statistics = ImageStat.Stat(image)
        mean = statistics.mean[0]
        deviation = statistics.stddev[0]
        # Identical white page margins are not browser chrome. A flat colored
        # viewer bar still counts, as does a mostly white strip containing text.
        return mean < 248.0 or deviation >= 3.0


def metadata_region_boxes(
    visual: VisualPdfInspection,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    _, top, _, bottom = visual.content_crop
    height = max(0.2, bottom - top)
    regions = [
        ("journal_header", (0.04, top, 0.96, top + 0.12 * height)),
        ("title_author", (0.04, top + 0.06 * height, 0.96, top + 0.35 * height)),
        (
            "article_information",
            (0.04, top + 0.27 * height, 0.96, top + 0.46 * height),
        ),
        (
            "article_doi_region",
            (0.04, top + 0.08 * height, 0.96, top + 0.60 * height),
        ),
        ("abstract_header", (0.04, top + 0.42 * height, 0.96, top + 0.56 * height)),
    ]
    if visual.pdf_visual_type == PdfVisualType.SCREENSHOT_WRAPPED:
        regions.append(("viewer_footer_url", (0.0, 0.88, 1.0, 1.0)))
    return regions
