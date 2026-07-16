from __future__ import annotations

from dataclasses import asdict, dataclass


STANDARD_EXIT_CODES = (0, 2, 3, 10, 20, 30, 40)


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    name: str
    purpose: str
    workflow: str
    category: str
    read_only: bool
    modifies_catalogue: bool
    modifies_files: bool
    uses_network: bool
    requires_lock: bool
    runs_final_check: bool
    supports_dry_run: bool
    exit_codes: tuple[int, ...]
    report_type: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["exit_codes"] = list(self.exit_codes)
        return payload


COMMANDS = (
    CommandDefinition("check", "Reconcile Catalogue and managed file state", "Workflow 1", "daily", False, True, False, False, True, False, True, STANDARD_EXIT_CODES, "daily_check"),
    CommandDefinition("register", "Identify and register Inbox papers and supplementary documents", "Workflow 3", "daily", False, True, True, True, True, True, True, STANDARD_EXIT_CODES, "inbox_register"),
    CommandDefinition("search", "Query providers and optionally update or download records", "Workflow 2", "daily", False, True, True, True, True, True, True, STANDARD_EXIT_CODES, "metadata_query"),
    CommandDefinition("file", "File or refile registered Documents under Topics/", "Workflow 4", "daily", False, True, True, False, True, True, True, STANDARD_EXIT_CODES, "catalogue_filing"),
    CommandDefinition("cleanup", "Apply allowlisted generated-file retention", "Maintenance", "maintenance", False, False, True, False, True, False, True, STANDARD_EXIT_CODES, "cleanup"),
    CommandDefinition("normalize-records", "Canonicalize existing records by exact identifiers", "Maintenance", "maintenance", False, True, False, True, True, True, True, STANDARD_EXIT_CODES, "record_normalization"),
    CommandDefinition("repair-publication-types", "Normalize publication types and Registered filenames", "Maintenance", "maintenance", False, True, True, False, True, True, True, STANDARD_EXIT_CODES, "publication_type_repair"),
    CommandDefinition("migrate-topics", "Move legacy root topic directories into Topics/", "Maintenance", "maintenance", False, True, True, False, True, True, True, STANDARD_EXIT_CODES, "topic_migration"),
    CommandDefinition("migrate-documents", "Create Documents sheet and migrate legacy main PDFs", "Maintenance", "maintenance", False, True, False, False, True, True, True, STANDARD_EXIT_CODES, "document_migration"),
    CommandDefinition("doctor", "Check OCR and local runtime availability", "Maintenance", "maintenance", True, False, False, False, False, False, False, STANDARD_EXIT_CODES, "doctor"),
    CommandDefinition("commands", "List the public CLI command registry", "Audit", "audit", True, False, False, False, False, False, False, STANDARD_EXIT_CODES, "command_registry"),
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
