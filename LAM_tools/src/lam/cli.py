from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from . import __version__
from .command_registry import (
    COMMAND_BY_NAME,
    RUNTIME_COMMAND_BY_NAME,
    canonical_command,
    command_definition,
    command_markdown_table,
)
from .config import Settings
from .exceptions import (
    CatalogueError,
    ConfigurationError,
    FileOperationError,
    LamError,
    NetworkError,
    ProviderError,
)
from .models import MetadataLookupRequest, WorkflowResult, WorkflowStatus
from .run_context import RunContext, activate_run_context
from .services.catalogue_preflight_service import CataloguePreflightService
from .services.invocation_service import InvocationService
from .workflows.catalogue_filing import CatalogueFilingWorkflow
from .workflows.cleanup import CleanupWorkflow
from .workflows.citation_export import CitationExportWorkflow
from .workflows.command_audit import CommandAuditWorkflow
from .workflows.daily_check import DailyCheckWorkflow
from .workflows.doctor import DoctorWorkflow
from .workflows.document_migration import DocumentMigrationWorkflow
from .workflows.inbox_register import InboxRegisterWorkflow
from .workflows.library_init import LibraryInitWorkflow
from .workflows.metadata_query import MetadataQueryWorkflow
from .workflows.migration import MigrationWorkflow
from .workflows.publication_type_repair import PublicationTypeRepairWorkflow
from .workflows.record_normalization import RecordNormalizationWorkflow
from .workflows.recovery import RecoveryWorkflow
from .workflows.review import ReviewWorkflow
from .workflows.status import StatusWorkflow


EXIT_CODES = {
    WorkflowStatus.SUCCESS: 0,
    WorkflowStatus.NEEDS_REVIEW: 2,
    WorkflowStatus.NO_CHANGES: 3,
    WorkflowStatus.FAILED: 30,
}
CALLERS = ("user", "agent", "internal_workflow", "scheduled", "unknown")
JSON_SCHEMA_VERSION = "1"
PUBLIC_COMMAND_METAVAR = (
    "{init,check,register,search,file,export,review,status,recover,migrate,cleanup,doctor,commands}"
)


class CliParserError(Exception):
    pass


class LamArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliParserError(message)


class _PublicChoiceMap(dict[str, argparse.ArgumentParser]):
    """Keep compatibility parsers addressable without advertising them."""

    def __iter__(self):
        return (name for name in super().__iter__() if name in COMMAND_BY_NAME)


def _add_shared_options(parser: argparse.ArgumentParser, *, inherited: bool) -> None:
    default = argparse.SUPPRESS if inherited else None
    parser.add_argument("--root", type=Path, default=default, help="Research library root")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=default if inherited else False,
        help="Machine-readable output",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=default if inherited else False,
        help="Verbose diagnostic logging",
    )
    parser.add_argument(
        "--caller",
        choices=CALLERS,
        default=default,
        help="Invocation source for the audit log",
    )


def _daily_parent(shared: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, parents=[shared])
    parser.add_argument("--dry-run", action="store_true", help="Preview business-state changes")
    return parser


def _maintenance_parent(shared: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, parents=[shared])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview only")
    mode.add_argument("--apply", action="store_true", help="Apply planned changes")
    return parser


def _add_provider_policy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--offline", action="store_true", help="Use valid cache only")
    parser.add_argument("--refresh", action="store_true", help="Ignore valid cache entries")
    parser.add_argument(
        "--no-cache-write",
        action="store_true",
        help="Do not write provider caches or persistent provider quota counters",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = LamArgumentParser(prog="lam", description="Local Archives Manager")
    parser.add_argument("--version", action="version", version=__version__)
    _add_shared_options(parser, inherited=False)

    shared = argparse.ArgumentParser(add_help=False)
    _add_shared_options(shared, inherited=True)
    daily = _daily_parent(shared)
    maintenance = _maintenance_parent(shared)
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar=PUBLIC_COMMAND_METAVAR
    )

    subparsers.add_parser("init", parents=[maintenance], help=command_definition("init").purpose)
    subparsers.add_parser("check", parents=[daily], help=command_definition("check").purpose)

    register = subparsers.add_parser(
        "register", parents=[daily], help=command_definition("register").purpose
    )
    register.add_argument("--max-files", type=int)
    register.add_argument("--filename-only", action="store_true")
    register.add_argument("--skip-pdf-text", action="store_true", help=argparse.SUPPRESS)
    register.add_argument("--ocr", choices=("auto", "never", "always"), default="auto")
    register.add_argument("--ocr-language", action="append", dest="ocr_languages")
    register.add_argument("--ocr-dpi", type=int)
    register.add_argument("--ocr-gpu", choices=("auto", "true", "false"))
    _add_provider_policy_options(register)

    search = subparsers.add_parser(
        "search", parents=[daily], help=command_definition("search").purpose
    )
    for option in ("pmid", "doi", "title", "arxiv-id", "paper-uuid"):
        search.add_argument(f"--{option}")
    search.add_argument("--row", type=int)
    search.add_argument("--missing-metadata", action="store_true")
    search.add_argument("--incomplete-records", action="store_true")
    search.add_argument("--normalize-existing", action="store_true")
    search.add_argument("--provider", choices=("auto", "pubmed", "arxiv", "unpaywall"), default="auto")
    search.add_argument("--max-results", type=int, default=10)
    search.add_argument("--max-records", type=int, default=25)
    _add_provider_policy_options(search)
    search.add_argument("--download", action="store_true")
    search.add_argument("--download-source", choices=("auto", "arxiv", "unpaywall"), default="auto")
    search.add_argument("--max-download-size", type=float, metavar="MB")
    search.add_argument("--download-timeout", type=float, metavar="SECONDS")

    subparsers.add_parser("file", parents=[daily], help=command_definition("file").purpose)

    export = subparsers.add_parser(
        "export", parents=[shared], help=command_definition("export").purpose
    )
    export_subparsers = export.add_subparsers(dest="export_command", required=True)
    zotero = export_subparsers.add_parser("zotero", parents=[maintenance])
    export_target = zotero.add_mutually_exclusive_group(required=True)
    export_target.add_argument("--all", action="store_true", dest="all_records")
    export_target.add_argument("--paper-uuid")
    export_target.add_argument("--topic-folder")
    zotero.add_argument("--format", choices=("nbib", "pubmed-xml"), default="nbib", dest="format_name")
    zotero.add_argument("--official-only", action="store_true")
    _add_provider_policy_options(zotero)
    zotero.add_argument("--output", type=Path)

    review = subparsers.add_parser(
        "review", parents=[maintenance], help=command_definition("review").purpose
    )
    selector = review.add_mutually_exclusive_group(required=True)
    selector.add_argument("--all", action="store_true", dest="all_records")
    selector.add_argument("--paper-uuid")
    selector.add_argument("--document-id")
    review.add_argument("--provider", choices=("auto", "pubmed", "arxiv", "unpaywall"))
    _add_provider_policy_options(review)

    status = subparsers.add_parser(
        "status", parents=[shared], help=command_definition("status").purpose
    )
    status_subparsers = status.add_subparsers(dest="status_command", required=True)
    for name in ("library", "commands", "recovery", "config"):
        status_subparsers.add_parser(name, parents=[shared])
    environment = status_subparsers.add_parser("environment", parents=[shared])
    environment.add_argument("--initialize-ocr-models", action="store_true")

    recover = subparsers.add_parser(
        "recover", parents=[maintenance], help=command_definition("recover").purpose
    )
    recover.add_argument(
        "--scope",
        choices=("auto", "workbook", "inbox", "registered", "publication-types"),
        default="auto",
    )
    _add_provider_policy_options(recover)

    migrate = subparsers.add_parser(
        "migrate", parents=[shared], help=command_definition("migrate").purpose
    )
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command", required=True)
    migrate_subparsers.add_parser("identifiers", parents=[maintenance])
    migrate_topics = migrate_subparsers.add_parser("topics", parents=[maintenance])
    migrate_topics.add_argument("--include-topic", action="append", default=[])

    subparsers.add_parser("cleanup", parents=[maintenance], help=command_definition("cleanup").purpose)
    doctor = subparsers.add_parser("doctor", parents=[shared], help=command_definition("doctor").purpose)
    doctor.add_argument("--initialize-ocr-models", action="store_true")
    subparsers.add_parser("commands", parents=[shared], help=command_definition("commands").purpose)

    # Executable compatibility shims are deliberately absent from the public help.
    normalize = subparsers.add_parser("normalize-records", parents=[maintenance], help=argparse.SUPPRESS)
    normalize.add_argument("--max-records", type=int, default=1000)
    _add_provider_policy_options(normalize)
    repair = subparsers.add_parser("repair-publication-types", parents=[maintenance], help=argparse.SUPPRESS)
    del repair
    old_topics = subparsers.add_parser("migrate-topics", parents=[maintenance], help=argparse.SUPPRESS)
    old_topics.add_argument("--include-topic", action="append", default=[])
    subparsers.add_parser("migrate-identifiers", parents=[maintenance], help=argparse.SUPPRESS)
    subparsers.add_parser("migrate-documents", parents=[maintenance], help=argparse.SUPPRESS)

    hidden = {
        "normalize-records",
        "repair-publication-types",
        "migrate-topics",
        "migrate-identifiers",
        "migrate-documents",
    }
    subparsers._choices_actions = [  # type: ignore[attr-defined]
        action
        for action in subparsers._choices_actions  # type: ignore[attr-defined]
        if action.dest not in hidden
    ]

    for name, command_parser in subparsers.choices.items():
        command_parser.description = command_definition(name).purpose
    visible_map = _PublicChoiceMap(subparsers.choices)
    subparsers.choices = visible_map
    subparsers._name_parser_map = visible_map  # type: ignore[attr-defined]
    return parser


def _configure_logging(
    settings: Settings, verbose: bool, *, file_logging: bool = True
) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    if file_logging:
        settings.ensure_runtime_directories()
        handlers.append(
            RotatingFileHandler(
                settings.logs_dir / "lam.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return handlers


def _close_logging_handlers(handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for handler in handlers:
        try:
            root.removeHandler(handler)
            handler.flush()
            handler.close()
        except Exception:
            pass


def _invoked_command(args: argparse.Namespace) -> str:
    nested = (
        getattr(args, "status_command", None)
        or getattr(args, "migrate_command", None)
        or getattr(args, "export_command", None)
    )
    return f"{args.command} {nested}" if nested else args.command


def _canonical(args: argparse.Namespace) -> str:
    nested = (
        getattr(args, "status_command", None)
        or getattr(args, "migrate_command", None)
        or getattr(args, "export_command", None)
    )
    return canonical_command(args.command, nested)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except CliParserError as exc:
        return _parser_failure(parser, raw, str(exc))

    started_at = datetime.now().astimezone()
    started_clock = time.perf_counter()
    settings: Settings | None = None
    context_data: RunContext | None = None
    result: WorkflowResult | None = None
    exit_code = 30
    error_message: str | None = None
    error_type: str | None = None
    captured_warnings: list[Any] = []
    owned_handlers: list[logging.Handler] = []
    caller = args.caller or os.getenv("LAM_CALLER", "user").strip().casefold() or "user"
    invoked = _invoked_command(args)
    canonical = _canonical(args)

    try:
        if caller not in CALLERS:
            raise ConfigurationError(f"Unsupported caller value: {caller}")
        permissive = args.command in {"init", "status", "doctor", "commands"}
        settings = Settings.from_root(
            args.root,
            require_catalogue=not permissive,
            allow_missing_root=permissive,
        )
        # Init must assess the target before LAM itself writes into it.
        owned_handlers = _configure_logging(
            settings, args.verbose, file_logging=args.command != "init"
        )
        context_data = RunContext.create(
            caller=caller,
            library_root=settings.library_root,
            dry_run=bool(getattr(args, "dry_run", False)),
            top_level_command=invoked,
        )
        definition = command_definition(args.command)
        lock = FileLock(settings.lock_path, timeout=0)
        lock_required = (
            definition.requires_lock
            and args.command != "init"
            and (not getattr(args, "dry_run", False) or args.command == "search")
        )
        lock_context = lock if lock_required else _NullContext()
        context_data.lock_state = "required" if lock_required else "not_required"
        with activate_run_context(context_data), lock_context:
            if lock_required:
                context_data.lock_state = "acquired"
            if (
                definition.modifies_catalogue
                and args.command not in {"init", "migrate", "migrate-identifiers", "migrate-documents"}
            ):
                CataloguePreflightService(settings.catalogue_path).before_modification()
            if args.json_output:
                capture = io.StringIO()
                with contextlib.redirect_stdout(capture):
                    result = _run_command(args, settings)
                unexpected_stdout = capture.getvalue().strip()
                if unexpected_stdout:
                    captured_warnings.append(
                        {"issue": "suppressed_third_party_stdout", "characters": len(unexpected_stdout)}
                    )
            else:
                result = _run_command(args, settings)
        exit_code = 40 if result.status == WorkflowStatus.FAILED and result.details.get("network_failure") else EXIT_CODES[result.status]
    except Timeout as exc:
        exit_code, error_type, error_message = 10, type(exc).__name__, "Another modifying LAM process holds the library lock"
    except ConfigurationError as exc:
        exit_code, error_type, error_message = 10, type(exc).__name__, str(exc)
    except CatalogueError as exc:
        exit_code, error_type, error_message = 20, type(exc).__name__, str(exc)
    except FileOperationError as exc:
        exit_code, error_type, error_message = 30, type(exc).__name__, str(exc)
    except (NetworkError, ProviderError) as exc:
        exit_code, error_type, error_message = 40, type(exc).__name__, str(exc)
    except LamError as exc:
        exit_code, error_type, error_message = 30, type(exc).__name__, str(exc)
    except Exception as exc:  # pragma: no cover
        logging.exception("Unexpected LAM failure")
        exit_code, error_type, error_message = 30, type(exc).__name__, f"Unexpected error: {exc}"
    finally:
        completed_at = datetime.now().astimezone()
        if context_data is None:
            context_data = _minimal_context(invoked, args.root, caller, bool(getattr(args, "dry_run", False)))
        if context_data is not None:
            invocation_dir = settings.invocations_dir if settings is not None else context_data.library_root / ".library_state" / "invocations"
            try:
                InvocationService(invocation_dir).write(
                    context_data,
                    arguments=vars(args),
                    result=result,
                    exit_code=exit_code,
                    duration_ms=round((time.perf_counter() - started_clock) * 1000),
                    canonical_command=canonical,
                    status=result.status.value if result is not None else "failed",
                    error_type=error_type,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            except Exception:
                logging.exception("Could not write invocation audit")
        _close_logging_handlers(owned_handlers)

    if args.json_output:
        print(json.dumps(_json_envelope(
            command=invoked,
            canonical_command=canonical,
            exit_code=exit_code,
            result=result,
            errors=([{"type": error_type, "message": error_message}] if error_message else []),
            warnings=captured_warnings,
            invocation_id=context_data.run_id if context_data else None,
        ), ensure_ascii=True, default=str))
    elif error_message:
        print(f"lam: error: {error_message}", file=sys.stderr)
    elif invoked in {"commands", "status commands"}:
        print(command_markdown_table())
    elif result is not None:
        print(
            f"{result.workflow}: {result.status.value}; files={result.changed_files}; "
            f"rows={result.changed_rows}; report={result.report_path}"
        )
    return exit_code


def _run_command(args: argparse.Namespace, settings: Settings) -> WorkflowResult:
    if args.command == "init":
        return LibraryInitWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "check":
        return DailyCheckWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "file":
        return CatalogueFilingWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "export":
        return CitationExportWorkflow(settings).run(
            dry_run=args.dry_run,
            all_records=args.all_records,
            paper_uuid=args.paper_uuid,
            topic_folder=args.topic_folder,
            format_name=args.format_name,
            official_only=args.official_only,
            offline=args.offline,
            refresh=args.refresh,
            cache_write=not args.no_cache_write,
            output=args.output,
        )
    if args.command in {"doctor", "status"}:
        status_command = "environment" if args.command == "doctor" else args.status_command
        if status_command == "environment":
            return DoctorWorkflow(settings).run(initialize_ocr_models=args.initialize_ocr_models)
        if status_command == "commands":
            return CommandAuditWorkflow(settings).run(write_report=False)
        status = StatusWorkflow(settings)
        return {
            "library": status.library,
            "recovery": status.recovery,
            "config": status.config,
        }[status_command]()
    if args.command == "commands":
        return CommandAuditWorkflow(settings).run(write_report=False)
    if args.command == "cleanup":
        return CleanupWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "review":
        return ReviewWorkflow(settings).run(
            dry_run=args.dry_run,
            all_records=args.all_records,
            paper_uuid=args.paper_uuid,
            document_id=args.document_id,
            provider=args.provider,
            offline=args.offline,
            refresh=args.refresh,
            cache_write=not args.no_cache_write,
        )
    if args.command == "recover":
        return RecoveryWorkflow(settings).run(
            dry_run=args.dry_run,
            scope=args.scope,
            offline=args.offline,
            refresh=args.refresh,
            cache_write=not args.no_cache_write,
        )
    if args.command == "migrate":
        migration = MigrationWorkflow(settings)
        if args.migrate_command == "identifiers":
            return migration.identifiers(dry_run=args.dry_run)
        return migration.topics(dry_run=args.dry_run, include_topics=tuple(args.include_topic))
    if args.command == "migrate-topics":
        return MigrationWorkflow(settings).topics(dry_run=args.dry_run, include_topics=tuple(args.include_topic))
    if args.command == "migrate-identifiers":
        return MigrationWorkflow(settings).identifiers(dry_run=args.dry_run)
    if args.command == "migrate-documents":
        return DocumentMigrationWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "repair-publication-types":
        return PublicationTypeRepairWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "normalize-records":
        if args.max_records <= 0:
            raise ConfigurationError("--max-records must be positive")
        return RecordNormalizationWorkflow(settings).run(
            dry_run=args.dry_run,
            max_records=args.max_records,
            offline=args.offline,
            refresh=args.refresh,
            cache_write=not args.no_cache_write,
        )
    if args.command == "register":
        if args.max_files is not None and args.max_files <= 0:
            raise ConfigurationError("--max-files must be greater than zero")
        if args.ocr_dpi is not None and not 72 <= args.ocr_dpi <= 600:
            raise ConfigurationError("--ocr-dpi must be between 72 and 600")
        return InboxRegisterWorkflow(settings).run(
            dry_run=args.dry_run,
            max_files=args.max_files,
            filename_only=args.filename_only or args.skip_pdf_text,
            skip_pdf_text=args.skip_pdf_text,
            ocr_mode=args.ocr,
            ocr_languages=tuple(args.ocr_languages) if args.ocr_languages else None,
            ocr_dpi=args.ocr_dpi,
            ocr_gpu=args.ocr_gpu,
            offline=args.offline,
            refresh=args.refresh,
            cache_write=not args.no_cache_write,
        )
    return _run_search(args, settings)


def _run_search(args: argparse.Namespace, settings: Settings) -> WorkflowResult:
    if not any((
        args.pmid, args.doi, args.title, args.arxiv_id, args.paper_uuid,
        args.row is not None, args.missing_metadata, args.incomplete_records,
        args.normalize_existing,
    )):
        raise ConfigurationError("search requires an identifier, title, catalogue target, or --missing-metadata")
    if args.row is not None and args.row < 2:
        raise ConfigurationError("--row must be an Excel row number of 2 or greater")
    if args.max_results <= 0 or args.max_records <= 0:
        raise ConfigurationError("--max-results and --max-records must be positive")
    if args.max_download_size is not None and args.max_download_size <= 0:
        raise ConfigurationError("--max-download-size must be positive")
    if args.download_timeout is not None and args.download_timeout <= 0:
        raise ConfigurationError("--download-timeout must be positive")
    request = MetadataLookupRequest(
        pmid=args.pmid,
        doi=args.doi,
        title=args.title,
        arxiv_id=args.arxiv_id,
        provider=args.provider,
        max_results=args.max_results,
        refresh=args.refresh,
        offline=args.offline,
        cache_write=not args.no_cache_write,
    )
    return MetadataQueryWorkflow(settings).run(
        request,
        dry_run=args.dry_run,
        catalogue_row=args.row,
        paper_uuid=args.paper_uuid,
        missing_metadata=args.missing_metadata,
        incomplete_records=args.incomplete_records,
        normalize_existing=args.normalize_existing,
        max_records=args.max_records,
        download=args.download,
        download_source=args.download_source,
        max_download_size_mb=args.max_download_size,
        download_timeout=args.download_timeout,
    )


def _json_envelope(
    *, command: str, canonical_command: str, exit_code: int,
    result: WorkflowResult | None, errors: list[Any], warnings: list[Any],
    invocation_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "canonical_command": canonical_command,
        "status": result.status.value if result is not None else "failed",
        "exit_code": exit_code,
        "errors": errors or (list(result.failures) if result is not None else []),
        "warnings": warnings,
        "report_path": result.report_path if result is not None else None,
        "invocation_id": invocation_id,
        "details": result.to_dict() if result is not None else {},
    }


def _parser_failure(parser: argparse.ArgumentParser, raw: list[str], message: str) -> int:
    command, root, caller = _raw_invocation_context(raw)
    context_data = _minimal_context(command, root, caller, "--dry-run" in raw)
    started = datetime.now().astimezone()
    canonical = _raw_canonical(command, raw)
    if context_data is not None:
        try:
            InvocationService(context_data.library_root / ".library_state" / "invocations").write(
                context_data,
                arguments={"argv": raw}, result=None, exit_code=10, duration_ms=0,
                canonical_command=canonical, status="failed", error_type="ParserError",
                started_at=started, completed_at=datetime.now().astimezone(),
            )
        except Exception:
            pass
    if "--json" in raw:
        print(json.dumps(_json_envelope(
            command=command, canonical_command=canonical, exit_code=10, result=None,
            errors=[{"type": "ParserError", "message": message}], warnings=[],
            invocation_id=context_data.run_id if context_data else None,
        ), ensure_ascii=True))
    else:
        print(parser.format_usage().rstrip(), file=sys.stderr)
        print(f"lam: error: {message}", file=sys.stderr)
    return 10


def _raw_invocation_context(raw: list[str]) -> tuple[str, Path | None, str]:
    command = next((item for item in raw if item in RUNTIME_COMMAND_BY_NAME), "")
    root: Path | None = None
    if "--root" in raw:
        index = raw.index("--root")
        if index + 1 < len(raw):
            root = Path(raw[index + 1])
    elif os.getenv("LIBRARY_ROOT"):
        root = Path(os.environ["LIBRARY_ROOT"])
    caller = os.getenv("LAM_CALLER", "user").strip().casefold() or "user"
    if "--caller" in raw:
        index = raw.index("--caller")
        if index + 1 < len(raw):
            caller = raw[index + 1]
    return command, root, caller


def _raw_canonical(command: str, raw: list[str]) -> str:
    if not command:
        return ""
    nested = next((item for item in ("library", "environment", "commands", "recovery", "config", "identifiers", "topics", "zotero") if item in raw), None)
    try:
        return canonical_command(command, nested)
    except KeyError:
        return command


def _minimal_context(command: str, root: Path | None, caller: str, dry_run: bool) -> RunContext | None:
    if not command:
        return None
    selected = root or Path(__file__).resolve().parents[3]
    try:
        resolved = selected.expanduser().resolve()
    except OSError:
        return None
    if not resolved.exists() and not resolved.parent.is_dir():
        return None
    if resolved.exists() and not resolved.is_dir():
        return None
    return RunContext.create(
        caller=caller if caller in CALLERS else "unknown",
        library_root=resolved,
        dry_run=dry_run,
        top_level_command=command,
    )


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
