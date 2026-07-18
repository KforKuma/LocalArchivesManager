from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import tempfile

from ..config import Settings
from ..models import WorkflowResult
from ..runtime_resources import runtime_layout
from ..services.ocr_service import OcrService
from ..services.report_service import ReportService


class DoctorWorkflow:
    def __init__(self, settings: Settings, ocr_service: OcrService | None = None):
        self.settings = settings
        self.ocr_service = ocr_service or OcrService(settings)

    def run(self, *, initialize_ocr_models: bool = False) -> WorkflowResult:
        result = WorkflowResult("doctor", mode="diagnostic")
        availability = self.ocr_service.check_availability(
            deep=True,
            initialize_models=initialize_ocr_models,
        )
        layout = runtime_layout()
        ocr = asdict(availability)
        ocr_details = availability.details
        templates_root = Path(__file__).resolve().parents[1] / "resources"
        package_templates = {
            name: {
                "path": str(templates_root / name),
                "present": (templates_root / name).is_file(),
            }
            for name in ("AGENTS.md", "Workflows.md")
        }
        writable_runtime_paths = {
            name: {
                "path": str(path),
                "writable": self._probe_writable(path),
            }
            for name, path in (
                ("state", self.settings.state_dir),
                ("reports", self.settings.reports_dir),
                ("logs", self.settings.logs_dir),
                ("temporary", self.settings.download_temp_dir or self.settings.state_dir / "tmp"),
                ("exports", self.settings.exports_dir),
            )
        }
        result.details = {
            "is_frozen": layout.is_frozen,
            "bundle_root": str(layout.bundle_root) if layout.bundle_root else None,
            "easyocr_import": availability.easyocr_available,
            "model_path": ocr_details.get("model_storage_dir"),
            "model_integrity": ocr_details.get("model_integrity"),
            "model_download_disabled": ocr_details.get(
                "model_download_disabled", True
            ),
            "poppler_path": ocr_details.get("poppler_path"),
            "poppler_executables": ocr_details.get("poppler_executables", {}),
            "package_templates": package_templates,
            "writable_runtime_paths": writable_runtime_paths,
            "ocr": ocr,
            "uses_network": bool(
                ocr_details.get(
                    "uses_network", initialize_ocr_models and not layout.is_frozen
                )
            ),
            "may_download_models": bool(
                ocr_details.get(
                    "may_download_models",
                    initialize_ocr_models and not layout.is_frozen,
                )
            ),
        }
        if availability.available:
            result.completed.append({"action": "ocr_runtime_available"})
        else:
            result.needs_review.append(
                {"issue": availability.status, "details": availability.details}
            )
        missing_templates = [
            name for name, item in package_templates.items() if not item["present"]
        ]
        if missing_templates:
            result.needs_review.append(
                {"issue": "package_templates_missing", "files": missing_templates}
            )
        unwritable = [
            name
            for name, item in writable_runtime_paths.items()
            if not item["writable"]
        ]
        if unwritable:
            result.needs_review.append(
                {"issue": "runtime_paths_unwritable", "paths": unwritable}
            )
        if result.needs_review:
            result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result

    @staticmethod
    def _probe_writable(path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix="lam-doctor-", dir=path)
            os.close(handle)
            Path(name).unlink(missing_ok=True)
            return True
        except OSError:
            return False
