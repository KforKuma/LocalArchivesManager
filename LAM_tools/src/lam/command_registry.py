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
    aliases: tuple[str, ...] = ()
    supports_json: bool = True
    uses_ocr: bool = False
    modifies_business_state: bool = False
    writes_runtime_artifacts: bool = True
    writes_cache: bool = False
    uses_network: bool = False
    may_download_models: bool = False
    modifies_managed_files: bool = False
    modifies_catalogue: bool = False
    requires_lock: bool = False
    requires_export_lock: bool = False
    writes_export_artifacts: bool = False
    runs_final_check: bool = False
    supports_dry_run: bool = False
    actual_exit_codes: tuple[int, ...] = (0, 10, 30)
    report_type: str = ""
    visibility: str = "public"
    canonical_command: str = ""

    @property
    def read_only(self) -> bool:
        return not self.modifies_business_state

    @property
    def modifies_files(self) -> bool:
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
        payload["canonical_command"] = self.canonical_command or self.name
        return payload


GLOBAL = ("--root", "--json", "--verbose", "--caller")
DAILY = (*GLOBAL, "--dry-run")
EXPLICIT = (*GLOBAL, "--dry-run", "--apply")
PROVIDER = ("--offline", "--refresh", "--no-cache-write")


COMMANDS = (
    CommandDefinition(
        "init",
        "Initialize a new empty LAM library",
        "Initialization",
        "setup",
        EXPLICIT,
        modifies_business_state=True,
        modifies_managed_files=True,
        modifies_catalogue=True,
        requires_lock=False,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=(0, 3, 10, 20, 30),
        report_type="library_init",
    ),
    CommandDefinition(
        "check",
        "Reconcile Catalogue and managed file state",
        "Workflow 1",
        "daily",
        DAILY,
        modifies_business_state=True,
        modifies_catalogue=True,
        requires_lock=True,
        supports_dry_run=True,
        actual_exit_codes=LOCAL_WORKFLOW_CODES,
        report_type="daily_check",
    ),
    CommandDefinition(
        "register",
        "Register Inbox PDFs, supplements, and reference text",
        "Workflow 3",
        "daily",
        (*DAILY, "--max-files", "--filename-only", "--skip-pdf-text", "--ocr", "--ocr-language", "--ocr-dpi", "--ocr-gpu", "--reference-text", "--reference-file", "--max-references", "--download-missing", "--require-download", *PROVIDER),
        uses_ocr=True,
        modifies_business_state=True,
        writes_cache=True,
        uses_network=True,
        modifies_managed_files=True,
        modifies_catalogue=True,
        requires_lock=True,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=NETWORK_WORKFLOW_CODES,
        report_type="inbox_register",
    ),
    CommandDefinition(
        "search",
        "Query providers and optionally update, normalize, or download records",
        "Workflow 2",
        "daily",
        (*DAILY, "--pmid", "--doi", "--title", "--arxiv-id", "--paper-uuid", "--row", "--missing-metadata", "--incomplete-records", "--normalize-existing", "--provider", "--max-results", "--max-records", *PROVIDER, "--download", "--download-source", "--max-download-size", "--download-timeout"),
        modifies_business_state=True,
        writes_cache=True,
        uses_network=True,
        modifies_managed_files=True,
        modifies_catalogue=True,
        requires_lock=True,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=NETWORK_WORKFLOW_CODES,
        report_type="metadata_query",
    ),
    CommandDefinition(
        "file",
        "File or refile registered Documents under Topics/",
        "Workflow 4",
        "daily",
        DAILY,
        modifies_business_state=True,
        modifies_managed_files=True,
        modifies_catalogue=True,
        requires_lock=True,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=LOCAL_WORKFLOW_CODES,
        report_type="catalogue_filing",
    ),
    CommandDefinition(
        "export",
        "Export registered citations for Zotero without modifying the library",
        "Citation export",
        "export",
        (
            *GLOBAL,
            "zotero",
            "--all",
            "--paper-uuid",
            "--topic-folder",
            "--format",
            "--official-only",
            "--offline",
            "--refresh",
            "--no-cache-write",
            "--output",
            "--dry-run",
            "--apply",
        ),
        writes_cache=True,
        uses_network=True,
        requires_export_lock=True,
        writes_export_artifacts=True,
        supports_dry_run=True,
        actual_exit_codes=NETWORK_WORKFLOW_CODES,
        report_type="citation_export",
    ),
    CommandDefinition(
        "review",
        "Recheck and clear objectively resolved machine blockers",
        "Review",
        "maintenance",
        (*EXPLICIT, "--all", "--paper-uuid", "--document-id", "--provider", *PROVIDER),
        modifies_business_state=True,
        writes_cache=True,
        uses_network=True,
        modifies_catalogue=True,
        requires_lock=True,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=NETWORK_WORKFLOW_CODES,
        report_type="review",
    ),
    CommandDefinition(
        "status",
        "Inspect library, environment, commands, recovery, or configuration",
        "Diagnostic",
        "diagnostic",
        (*GLOBAL, "library|environment|commands|recovery|config", "--initialize-ocr-models"),
        uses_ocr=True,
        uses_network=True,
        may_download_models=True,
        actual_exit_codes=(0, 2, 10, 20, 30),
        report_type="status",
    ),
    CommandDefinition(
        "recover",
        "Recover interrupted operations and unambiguous record bindings",
        "Recovery",
        "maintenance",
        (*EXPLICIT, "--scope", *PROVIDER),
        uses_ocr=True,
        modifies_business_state=True,
        writes_cache=True,
        uses_network=True,
        modifies_managed_files=True,
        modifies_catalogue=True,
        requires_lock=True,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=NETWORK_WORKFLOW_CODES,
        report_type="recover",
    ),
    CommandDefinition(
        "migrate",
        "Upgrade identifiers/Documents schema or legacy Topics layout",
        "Migration",
        "migration",
        (*EXPLICIT, "identifiers|topics", "--include-topic"),
        modifies_business_state=True,
        modifies_managed_files=True,
        modifies_catalogue=True,
        requires_lock=True,
        runs_final_check=True,
        supports_dry_run=True,
        actual_exit_codes=LOCAL_WORKFLOW_CODES,
        report_type="migration",
    ),
    CommandDefinition(
        "cleanup",
        "Apply allowlisted generated-file retention",
        "Cleanup",
        "maintenance",
        (*EXPLICIT, "--include-test-artifacts"),
        modifies_business_state=True,
        requires_lock=True,
        supports_dry_run=True,
        actual_exit_codes=(0, 3, 10, 30),
        report_type="cleanup",
    ),
    CommandDefinition(
        "doctor",
        "Alias for status environment",
        "Diagnostic",
        "diagnostic",
        (*GLOBAL, "--initialize-ocr-models"),
        aliases=("status environment",),
        uses_ocr=True,
        uses_network=True,
        may_download_models=True,
        actual_exit_codes=(0, 2, 10, 30),
        report_type="doctor",
        canonical_command="status environment",
    ),
    CommandDefinition(
        "commands",
        "Alias for status commands",
        "Command registry",
        "diagnostic",
        GLOBAL,
        aliases=("status commands",),
        actual_exit_codes=(0, 10, 30),
        report_type="command_registry",
        canonical_command="status commands",
    ),
)


COMPATIBILITY_COMMANDS = (
    CommandDefinition(
        "normalize-records", "Compatibility alias", "Workflow 2", "deprecated",
        (*EXPLICIT, "--max-records", *PROVIDER), visibility="hidden",
        canonical_command="search --normalize-existing", modifies_business_state=True,
        writes_cache=True, uses_network=True, modifies_catalogue=True, requires_lock=True,
        runs_final_check=True, supports_dry_run=True, actual_exit_codes=NETWORK_WORKFLOW_CODES,
    ),
    CommandDefinition(
        "repair-publication-types", "Compatibility alias", "Recovery", "deprecated",
        EXPLICIT, visibility="hidden", canonical_command="recover --scope publication-types",
        modifies_business_state=True, modifies_managed_files=True, modifies_catalogue=True,
        requires_lock=True, runs_final_check=True, supports_dry_run=True,
        actual_exit_codes=LOCAL_WORKFLOW_CODES,
    ),
    CommandDefinition(
        "migrate-topics", "Compatibility alias", "Migration", "deprecated",
        (*EXPLICIT, "--include-topic"), visibility="hidden", canonical_command="migrate topics",
        modifies_business_state=True, modifies_managed_files=True, modifies_catalogue=True,
        requires_lock=True, runs_final_check=True, supports_dry_run=True,
        actual_exit_codes=LOCAL_WORKFLOW_CODES,
    ),
    CommandDefinition(
        "migrate-identifiers", "Compatibility alias", "Migration", "deprecated",
        EXPLICIT, visibility="hidden", canonical_command="migrate identifiers",
        modifies_business_state=True, modifies_catalogue=True, requires_lock=True,
        runs_final_check=True, supports_dry_run=True, actual_exit_codes=LOCAL_WORKFLOW_CODES,
    ),
    CommandDefinition(
        "migrate-documents", "Compatibility alias", "Migration", "deprecated",
        EXPLICIT, visibility="hidden", canonical_command="migrate identifiers",
        modifies_business_state=True, modifies_catalogue=True, requires_lock=True,
        runs_final_check=True, supports_dry_run=True, actual_exit_codes=LOCAL_WORKFLOW_CODES,
    ),
)


COMMAND_BY_NAME = {item.name: item for item in COMMANDS}
RUNTIME_COMMAND_BY_NAME = {
    item.name: item for item in (*COMMANDS, *COMPATIBILITY_COMMANDS)
}


def command_definition(name: str) -> CommandDefinition:
    return RUNTIME_COMMAND_BY_NAME[name]


def canonical_command(name: str, subcommand: str | None = None) -> str:
    definition = command_definition(name)
    if name in {"status", "migrate", "export"} and subcommand:
        return f"{name} {subcommand}"
    return definition.canonical_command or name


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
