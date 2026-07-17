from __future__ import annotations

from dataclasses import asdict, dataclass


LOCAL_WORKFLOW_CODES = (0, 2, 3, 10, 20, 30)
NETWORK_WORKFLOW_CODES = (*LOCAL_WORKFLOW_CODES, 40)


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    name: str
    purpose: str
    workflow: str
    category: str
    arguments: tuple[str, ...]
    aliases: tuple[str, ...]
    supports_json: bool
    uses_ocr: bool
    modifies_business_state: bool
    writes_runtime_artifacts: bool
    writes_cache: bool
    uses_network: bool
    may_download_models: bool
    modifies_managed_files: bool
    modifies_catalogue: bool
    requires_lock: bool
    runs_final_check: bool
    supports_dry_run: bool
    actual_exit_codes: tuple[int, ...]
    report_type: str

    @property
    def read_only(self) -> bool:
        return not self.modifies_business_state

    @property
    def modifies_files(self) -> bool:
        """Backward-compatible name for managed-library file mutation."""
        return self.modifies_managed_files

    @property
    def exit_codes(self) -> tuple[int, ...]:
        return self.actual_exit_codes

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["arguments"] = list(self.arguments)
        payload["aliases"] = list(self.aliases)
        payload["actual_exit_codes"] = list(self.actual_exit_codes)
        payload["read_only"] = self.read_only
        payload["modifies_files"] = self.modifies_files
        payload["exit_codes"] = list(self.actual_exit_codes)
        return payload


GLOBAL = ("--root", "--json", "--verbose", "--caller")
DAILY = (*GLOBAL, "--dry-run")
MAINTENANCE = (*GLOBAL, "--dry-run", "--apply")


COMMANDS = (
    CommandDefinition(
        "check", "Reconcile Catalogue and managed file state", "Workflow 1", "daily",
        DAILY, (), True, False, True, True, False, False, False, False, True,
        True, False, True, LOCAL_WORKFLOW_CODES, "daily_check",
    ),
    CommandDefinition(
        "register", "Identify and register Inbox papers and supplementary documents", "Workflow 3", "daily",
        (*DAILY, "--max-files", "--filename-only", "--skip-pdf-text", "--ocr", "--ocr-language", "--ocr-dpi", "--ocr-gpu", "--offline", "--refresh", "--no-cache-write"),
        (), True, True, True, True, True, True, False, True, True, True, True, True,
        NETWORK_WORKFLOW_CODES, "inbox_register",
    ),
    CommandDefinition(
        "search", "Query providers and optionally update or download records", "Workflow 2", "daily",
        (*DAILY, "--pmid", "--doi", "--title", "--arxiv-id", "--paper-uuid", "--row", "--missing-metadata", "--incomplete-records", "--normalize-existing", "--provider", "--max-results", "--max-records", "--offline", "--refresh", "--no-cache-write", "--download", "--download-source", "--max-download-size", "--download-timeout"),
        (), True, False, True, True, True, True, False, True, True, True, True, True,
        NETWORK_WORKFLOW_CODES, "metadata_query",
    ),
    CommandDefinition(
        "file", "File or refile registered Documents under Topics/", "Workflow 4", "daily",
        DAILY, (), True, False, True, True, False, False, False, True, True, True,
        True, True, LOCAL_WORKFLOW_CODES, "catalogue_filing",
    ),
    CommandDefinition(
        "cleanup", "Apply allowlisted generated-file retention", "Maintenance", "maintenance",
        MAINTENANCE, (), True, False, True, True, False, False, False, False, False,
        True, False, True, (0, 3, 10, 30), "cleanup",
    ),
    CommandDefinition(
        "normalize-records", "Canonicalize existing records by exact identifiers", "Maintenance", "maintenance",
        (*MAINTENANCE, "--max-records", "--offline", "--refresh", "--no-cache-write"),
        (), True, False, True, True, True, True, False, False, True, True, True, True,
        NETWORK_WORKFLOW_CODES, "record_normalization",
    ),
    CommandDefinition(
        "repair-publication-types", "Normalize publication types and Registered filenames", "Maintenance", "maintenance",
        MAINTENANCE, (), True, False, True, True, False, False, False, True, True,
        True, True, True, LOCAL_WORKFLOW_CODES, "publication_type_repair",
    ),
    CommandDefinition(
        "migrate-topics", "Move legacy root topic directories into Topics/", "Maintenance", "maintenance",
        (*MAINTENANCE, "--include-topic"), (), True, False, True, True, False, False,
        False, True, True, True, True, True, LOCAL_WORKFLOW_CODES, "topic_migration",
    ),
    CommandDefinition(
        "migrate-documents", "Create Documents sheet and migrate legacy main PDFs", "Maintenance", "maintenance",
        MAINTENANCE, (), True, False, True, True, False, False, False, False, True,
        True, True, True, LOCAL_WORKFLOW_CODES, "document_migration",
    ),
    CommandDefinition(
        "migrate-identifiers", "Adopt paper_uuid and remove legacy Catalogue identity/file columns", "Maintenance", "maintenance",
        MAINTENANCE, (), True, False, True, True, False, False, False, False, True,
        True, True, True, LOCAL_WORKFLOW_CODES, "identifier_migration",
    ),
    CommandDefinition(
        "doctor", "Check OCR and local runtime availability", "Diagnostic", "diagnostic",
        (*GLOBAL, "--initialize-ocr-models"), (), True, True, False, True, False,
        True, True, False, False, False, False, False, (0, 2, 10, 30), "doctor",
    ),
    CommandDefinition(
        "commands", "List the public CLI command registry", "Audit", "audit",
        GLOBAL, (), True, False, False, True, False, False, False, False, False,
        False, False, False, (0, 10, 30), "command_registry",
    ),
)

COMMAND_BY_NAME = {item.name: item for item in COMMANDS}


def command_definition(name: str) -> CommandDefinition:
    return COMMAND_BY_NAME[name]


def command_registry_payload() -> list[dict[str, object]]:
    return [item.to_dict() for item in COMMANDS]


def command_markdown_table() -> str:
    lines = [
        "| Command | Category | Purpose | Dry run | Network |",
        "|---|---|---|---:|---:|",
    ]
    for item in COMMANDS:
        lines.append(
            f"| `lam {item.name}` | {item.category} | {item.purpose} | "
            f"{'yes' if item.supports_dry_run else 'no'} | "
            f"{'yes' if item.uses_network else 'no'} |"
        )
    return "\n".join(lines)
