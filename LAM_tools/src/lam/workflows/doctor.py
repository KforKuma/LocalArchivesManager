from __future__ import annotations

from dataclasses import asdict

from ..config import Settings
from ..models import WorkflowResult
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
        result.details = {
            "ocr": asdict(availability),
            "uses_network": bool(initialize_ocr_models),
            "may_download_models": bool(initialize_ocr_models),
        }
        if availability.available:
            result.completed.append({"action": "ocr_runtime_available"})
        else:
            result.needs_review.append(
                {"issue": availability.status, "details": availability.details}
            )
            result.finalize_status()
        ReportService(self.settings.reports_dir).write(result)
        return result
