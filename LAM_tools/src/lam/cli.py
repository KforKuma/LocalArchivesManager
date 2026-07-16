from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock, Timeout

from . import __version__
from .config import Settings
from .command_registry import command_definition, command_markdown_table
from .exceptions import (
    CatalogueError,
    ConfigurationError,
    FileOperationError,
    LamError,
    NetworkError,
    ProviderError,
)
from .models import MetadataLookupRequest, WorkflowStatus
from .run_context import RunContext, activate_run_context
from .services.invocation_service import InvocationService
from .services.catalogue_preflight_service import CataloguePreflightService
from .workflows.catalogue_filing import CatalogueFilingWorkflow
from .workflows.command_audit import CommandAuditWorkflow
from .workflows.cleanup import CleanupWorkflow
from .workflows.daily_check import DailyCheckWorkflow
from .workflows.doctor import DoctorWorkflow
from .workflows.document_migration import DocumentMigrationWorkflow
from .workflows.inbox_register import InboxRegisterWorkflow
from .workflows.identifier_migration import IdentifierMigrationWorkflow
from .workflows.metadata_query import MetadataQueryWorkflow
from .workflows.publication_type_repair import PublicationTypeRepairWorkflow
from .workflows.record_normalization import RecordNormalizationWorkflow
from .workflows.topic_migration import TopicMigrationWorkflow


EXIT_CODES = {
    WorkflowStatus.SUCCESS: 0,
    WorkflowStatus.NEEDS_REVIEW: 2,
    WorkflowStatus.NO_CHANGES: 3,
    WorkflowStatus.FAILED: 30,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lam", description="Local Archives Manager")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, help="Research library root")
    common.add_argument("--dry-run", action="store_true", help="Analyze without official changes")
    common.add_argument("--json", action="store_true", dest="json_output", help="Machine-readable output")
    common.add_argument("--verbose", action="store_true", help="Verbose diagnostic logging")
    common.add_argument(
        "--caller",
        choices=("user", "agent", "internal_workflow", "scheduled", "unknown"),
        help="Invocation source for the audit log",
    )
    subparsers.add_parser("check", parents=[common], help=command_definition("check").purpose)
    subparsers.add_parser("file", parents=[common], help=command_definition("file").purpose)
    subparsers.add_parser("doctor", parents=[common], help=command_definition("doctor").purpose)
    subparsers.add_parser("commands", parents=[common], help=command_definition("commands").purpose)
    cleanup = subparsers.add_parser(
        "cleanup",
        parents=[common],
        help=command_definition("cleanup").purpose,
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cleanup plan; otherwise use --dry-run",
    )
    subparsers.add_parser(
        "repair-publication-types",
        parents=[common],
        help="Normalize publication types and repair Registered filenames",
    )
    normalize_records = subparsers.add_parser(
        "normalize-records",
        parents=[common],
        help="Canonicalize existing records by exact identifiers without moving PDFs",
    )
    normalize_records.add_argument("--max-records", type=int, default=1000)
    migrate_topics = subparsers.add_parser(
        "migrate-topics",
        parents=[common],
        help=command_definition("migrate-topics").purpose,
    )
    migrate_topics.add_argument("--apply", action="store_true")
    migrate_topics.add_argument(
        "--include-topic",
        action="append",
        default=[],
        help="Explicitly mark one root directory as a legacy topic candidate",
    )
    migrate_documents = subparsers.add_parser(
        "migrate-documents",
        parents=[common],
        help=command_definition("migrate-documents").purpose,
    )
    migrate_documents.add_argument("--apply", action="store_true")
    migrate_identifiers = subparsers.add_parser(
        "migrate-identifiers",
        parents=[common],
        help=command_definition("migrate-identifiers").purpose,
    )
    migrate_identifiers.add_argument("--apply", action="store_true")
    register = subparsers.add_parser(
        "register", parents=[common], help=command_definition("register").purpose
    )
    register.add_argument("--max-files", type=int, help="Process only the first N Inbox PDFs")
    register.add_argument(
        "--filename-only",
        action="store_true",
        help="Use filenames and catalogue data without PDF page text",
    )
    register.add_argument("--ocr", choices=("auto", "never", "always"), default="auto")
    register.add_argument("--ocr-language", action="append", dest="ocr_languages")
    register.add_argument("--ocr-dpi", type=int)
    register.add_argument("--ocr-gpu", choices=("auto", "true", "false"))
    register.add_argument(
        "--skip-pdf-text",
        action="store_true",
        help="Do not extract PDF page text after filename matching fails",
    )
    search = subparsers.add_parser(
        "search", parents=[common], help=command_definition("search").purpose
    )
    search.add_argument("--pmid")
    search.add_argument("--doi")
    search.add_argument("--title")
    search.add_argument("--arxiv-id")
    search.add_argument("--paper-uuid")
    search.add_argument("--row", type=int)
    search.add_argument("--missing-metadata", action="store_true")
    search.add_argument("--incomplete-records", action="store_true")
    search.add_argument("--normalize-existing", action="store_true")
    search.add_argument(
        "--provider",
        choices=("auto", "pubmed", "arxiv", "unpaywall"),
        default="auto",
    )
    search.add_argument("--max-results", type=int, default=10)
    search.add_argument("--max-records", type=int, default=25)
    search.add_argument("--offline", action="store_true")
    search.add_argument("--refresh", action="store_true")
    search.add_argument("--no-cache-write", action="store_true")
    search.add_argument("--download", action="store_true")
    search.add_argument(
        "--download-source",
        choices=("auto", "arxiv", "unpaywall"),
        default="auto",
    )
    search.add_argument("--max-download-size", type=float, metavar="MB")
    search.add_argument("--download-timeout", type=float, metavar="SECONDS")
    for name, command_parser in subparsers.choices.items():
        command_parser.description = command_definition(name).purpose
    return parser


def _configure_logging(settings: Settings, verbose: bool) -> None:
    settings.ensure_runtime_directories()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(
                settings.logs_dir / "lam.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    try:
        settings = Settings.from_root(args.root)
        _configure_logging(settings, args.verbose)
        caller = args.caller or os.getenv("LAM_CALLER", "user").strip().casefold() or "user"
        if caller not in {"user", "agent", "internal_workflow", "scheduled", "unknown"}:
            raise ConfigurationError(f"Unsupported caller value: {caller}")
        context_data = RunContext.create(
            caller=caller,
            library_root=settings.library_root,
            dry_run=bool(args.dry_run),
            top_level_command=args.command,
        )
        definition = command_definition(args.command)
        lock = FileLock(settings.lock_path, timeout=0)
        lock_context = (
            lock
            if definition.requires_lock
            and (not args.dry_run or args.command == "search")
            else _NullContext()
        )
        context_data.lock_state = "required" if lock_context is lock else "not_required"
        with activate_run_context(context_data), lock_context:
            if lock_context is lock:
                context_data.lock_state = "acquired"
            if definition.modifies_catalogue:
                CataloguePreflightService(settings.catalogue_path).before_modification()
            result = _run_command(args, settings)
        if result.status == WorkflowStatus.FAILED and result.details.get("network_failure"):
            exit_code = 40
        else:
            exit_code = EXIT_CODES[result.status]
        InvocationService(settings.invocations_dir).write(
            context_data,
            arguments=vars(args),
            result=result,
            exit_code=exit_code,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        if args.json_output:
            print(json.dumps(result.to_dict(), ensure_ascii=True, default=str))
        elif args.command == "commands":
            print(command_markdown_table())
        else:
            print(
                f"{result.workflow}: {result.status.value}; "
                f"files={result.changed_files}; rows={result.changed_rows}; "
                f"report={result.report_path}"
            )
        return exit_code
    except Timeout:
        return _error("Another modifying LAM process holds the library lock", 10)
    except ConfigurationError as exc:
        return _error(str(exc), 10)
    except CatalogueError as exc:
        return _error(str(exc), 20)
    except FileOperationError as exc:
        return _error(str(exc), 30)
    except (NetworkError, ProviderError) as exc:
        return _error(str(exc), 40)
    except LamError as exc:
        return _error(str(exc), 30)
    except Exception as exc:  # pragma: no cover - last-resort CLI containment
        logging.exception("Unexpected LAM failure")
        return _error(f"Unexpected error: {exc}", 30)


def _run_command(args: argparse.Namespace, settings: Settings):
    if args.command in {"doctor", "commands"} and args.dry_run:
        raise ConfigurationError(f"{args.command} does not support --dry-run")
    if args.command == "check":
        return DailyCheckWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "file":
        return CatalogueFilingWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "doctor":
        return DoctorWorkflow(settings).run()
    if args.command == "commands":
        return CommandAuditWorkflow(settings).run()
    if args.command == "cleanup":
        if args.dry_run == args.apply:
            raise ConfigurationError("cleanup requires exactly one of --dry-run or --apply")
        return CleanupWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "migrate-topics":
        if args.dry_run == args.apply:
            raise ConfigurationError(
                "migrate-topics requires exactly one of --dry-run or --apply"
            )
        return TopicMigrationWorkflow(settings).run(
            dry_run=args.dry_run,
            include_topics=tuple(args.include_topic),
        )
    if args.command == "migrate-documents":
        if args.dry_run == args.apply:
            raise ConfigurationError(
                "migrate-documents requires exactly one of --dry-run or --apply"
            )
        return DocumentMigrationWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "migrate-identifiers":
        if args.dry_run == args.apply:
            raise ConfigurationError(
                "migrate-identifiers requires exactly one of --dry-run or --apply"
            )
        return IdentifierMigrationWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "register":
        if args.max_files is not None and args.max_files <= 0:
            raise ConfigurationError("--max-files must be greater than zero")
        if args.ocr_dpi is not None and not 72 <= args.ocr_dpi <= 600:
            raise ConfigurationError("--ocr-dpi must be between 72 and 600")
        return InboxRegisterWorkflow(settings).run(
            dry_run=args.dry_run,
            max_files=args.max_files,
            filename_only=args.filename_only,
            skip_pdf_text=args.skip_pdf_text,
            ocr_mode=args.ocr,
            ocr_languages=tuple(args.ocr_languages) if args.ocr_languages else None,
            ocr_dpi=args.ocr_dpi,
            ocr_gpu=args.ocr_gpu,
        )
    if args.command == "repair-publication-types":
        return PublicationTypeRepairWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "normalize-records":
        if args.max_records <= 0:
            raise ConfigurationError("--max-records must be positive")
        return RecordNormalizationWorkflow(settings).run(
            dry_run=args.dry_run,
            max_records=args.max_records,
        )
    if not any(
        (
            args.pmid,
            args.doi,
            args.title,
            args.arxiv_id,
            args.paper_uuid,
            args.row is not None,
            args.missing_metadata,
            args.incomplete_records,
            args.normalize_existing,
        )
    ):
        raise ConfigurationError(
            "search requires an identifier, title, catalogue target, or --missing-metadata"
        )
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


def _error(message: str, code: int) -> int:
    print(json.dumps({"status": "failed", "error": message}, ensure_ascii=True))
    return code


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
