from __future__ import annotations

from ..command_registry import command_registry_payload
from ..config import Settings
from ..models import WorkflowResult
from ..services.report_service import ReportService


class CommandAuditWorkflow:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self) -> WorkflowResult:
        result = WorkflowResult("command_registry", mode="audit")
        result.details["commands"] = command_registry_payload()
        result.completed.append(
            {"action": "listed_public_commands", "count": len(result.details["commands"])}
        )
        ReportService(self.settings.reports_dir).write(result)
        return result
