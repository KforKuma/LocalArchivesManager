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
from .command_registry import COMMAND_BY_NAME, command_definition, command_markdown_table
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
from .workflows.command_audit import CommandAuditWorkflow
from .workflows.daily_check import DailyCheckWorkflow
from .workflows.doctor import DoctorWorkflow
from .workflows.document_migration import DocumentMigrationWorkflow
from .workflows.identifier_migration import IdentifierMigrationWorkflow
from .workflows.inbox_register import InboxRegisterWorkflow
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
CALLERS = ("user", "agent", "internal_workflow", "scheduled", "unknown")
JSON_SCHEMA_VERSION = "1"


class CliParserError(Exception):
    pass


class LamArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliParserError(message)


def _add_shared_options(parser: argparse.ArgumentParser, *, legacy: bool) -> None:
    default = argparse.SUPPRESS if legacy else None
    parser.add_argument("--root", type=Path, default=default, help="Research library root")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=default if legacy else False,
        help="Machine-readable output",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=default if legacy else False,
        help="Verbose diagnostic logging",
    )
    parser.add_argument(
        "--caller",
        choices=CALLERS,
        default=default,
        help="Invocation source for the audit log",
    )


def _daily_parent(legacy_shared: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, parents=[legacy_shared])
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview business-state changes"
    )
    return parser


def _maintenance_parent(
    legacy_shared: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, parents=[legacy_shared])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview only")
    mode.add_argument("--apply", action="store_true", help="Apply the planned changes")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = LamArgumentParser(prog="lam", description="Local Archives Manager")
    parser.add_argument("--version", action="version", version=__version__)
    _add_shared_options(parser, legacy=False)

    legacy_shared = argparse.ArgumentParser(add_help=False)
    _add_shared_options(legacy_shared, legacy=True)
    daily = _daily_parent(legacy_shared)
    maintenance = _maintenance_parent(legacy_shared)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", parents=[daily], help=command_definition("check").purpose)
    subparsers.add_parser("file", parents=[daily], help=command_definition("file").purpose)

    doctor = subparsers.add_parser(
        "doctor", parents=[legacy_shared], help=command_definition("doctor").purpose
    )
    doctor.add_argument(
        "--initialize-ocr-models",
        action="store_true",
        help="Explicitly allow EasyOCR model initialization and download",
    )
    subparsers.add_parser(
        "commands", parents=[legacy_shared], help=command_definition("commands").purpose
    )

    subparsers.add_parser(
        "cleanup", parents=[maintenance], help=command_definition("cleanup").purpose
    )
    subparsers.add_parser(
        "repair-publication-types",
        parents=[maintenance],
        help=command_definition("repair-publication-types").purpose,
    )
    normalize_records = subparsers.add_parser(
        "normalize-records",
        parents=[maintenance],
        help=command_definition("normalize-records").purpose,
    )
    normalize_records.add_argument("--max-records", type=int, default=1000)
    _add_provider_policy_options(normalize_records)

    migrate_topics = subparsers.add_parser(
        "migrate-topics",
        parents=[maintenance],
        help=command_definition("migrate-topics").purpose,
    )
    migrate_topics.add_argument(
        "--include-topic",
        action="append",
        default=[],
        help="Explicitly mark one root directory as a legacy topic candidate",
    )
    subparsers.add_parser(
        "migrate-documents",
        parents=[maintenance],
        help=command_definition("migrate-documents").purpose,
    )
    subparsers.add_parser(
        "migrate-identifiers",
        parents=[maintenance],
        help=command_definition("migrate-identifiers").purpose,
    )

    register = subparsers.add_parser(
        "register", parents=[daily], help=command_definition("register").purpose
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
        help="Compatibility alias for --filename-only",
    )
    _add_provider_policy_options(register)

    search = subparsers.add_parser(
        "search", parents=[daily], help=command_definition("search").purpose
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
        "--provider", choices=("auto", "pubmed", "arxiv", "unpaywall"), default="auto"
    )
    search.add_argument("--max-results", type=int, default=10)
    search.add_argument("--max-records", type=int, default=25)
    _add_provider_policy_options(search)
    search.add_argument("--download", action="store_true")
    search.add_argument(
        "--download-source", choices=("auto", "arxiv", "unpaywall"), default="auto"
    )
    search.add_argument("--max-download-size", type=float, metavar="MB")
    search.add_argument("--download-timeout", type=float, metavar="SECONDS")

    for name, command_parser in subparsers.choices.items():
        command_parser.description = command_definition(name).purpose
    return parser


def _add_provider_policy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--offline", action="store_true", help="Use valid cache only")
    parser.add_argument("--refresh", action="store_true", help="Ignore valid cache entries")
    parser.add_argument(
        "--no-cache-write",
        action="store_true",
        help="Do not write metadata cache or persistent provider quota counters",
    )


def _configure_logging(settings: Settings, verbose: bool) -> list[logging.Handler]:
    settings.ensure_runtime_directories()
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            settings.logs_dir / "lam.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stderr),
    ]
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


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except CliParserError as exc:
        return _parser_failure(parser, raw, str(exc))

    # --help and --version are argparse actions which exit before dispatch and
    # are intentionally not invocation-audited.
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

    try:
        if caller not in CALLERS:
            raise ConfigurationError(f"Unsupported caller value: {caller}")
        settings = Settings.from_root(args.root)
        owned_handlers = _configure_logging(settings, args.verbose)
        context_data = RunContext.create(
            caller=caller,
            library_root=settings.library_root,
            dry_run=bool(getattr(args, "dry_run", False)),
            top_level_command=args.command,
        )
        definition = command_definition(args.command)
        lock = FileLock(settings.lock_path, timeout=0)
        lock_context = (
            lock
            if definition.requires_lock
            and (not getattr(args, "dry_run", False) or args.command == "search")
            else _NullContext()
        )
        context_data.lock_state = "required" if lock_context is lock else "not_required"
        with activate_run_context(context_data), lock_context:
            if lock_context is lock:
                context_data.lock_state = "acquired"
            if definition.modifies_catalogue:
                CataloguePreflightService(settings.catalogue_path).before_modification()
            if args.json_output:
                capture = io.StringIO()
                with contextlib.redirect_stdout(capture):
                    result = _run_command(args, settings)
                unexpected_stdout = capture.getvalue().strip()
                if unexpected_stdout:
                    captured_warnings.append(
                        {
                            "issue": "suppressed_third_party_stdout",
                            "characters": len(unexpected_stdout),
                        }
                    )
                    logging.warning(
                        "Suppressed %s characters written to stdout during JSON command",
                        len(unexpected_stdout),
                    )
            else:
                result = _run_command(args, settings)
        if result.status == WorkflowStatus.FAILED and result.details.get("network_failure"):
            exit_code = 40
        else:
            exit_code = EXIT_CODES[result.status]
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
    except Exception as exc:  # pragma: no cover - last-resort containment
        logging.exception("Unexpected LAM failure")
        exit_code, error_type, error_message = 30, type(exc).__name__, f"Unexpected error: {exc}"
    finally:
        completed_at = datetime.now().astimezone()
        if context_data is None:
            context_data = _minimal_context(args.command, args.root, caller, bool(getattr(args, "dry_run", False)))
        if context_data is not None:
            invocation_dir = (
                settings.invocations_dir
                if settings is not None
                else context_data.library_root / ".library_state" / "invocations"
            )
            try:
                InvocationService(invocation_dir).write(
                    context_data,
                    arguments=vars(args),
                    result=result,
                    exit_code=exit_code,
                    duration_ms=round((time.perf_counter() - started_clock) * 1000),
                    canonical_command=args.command,
                    status=(result.status.value if result is not None else "failed"),
                    error_type=error_type,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            except Exception:
                logging.exception("Could not write invocation audit")
        _close_logging_handlers(owned_handlers)

    if args.json_output:
        print(
            json.dumps(
                _json_envelope(
                    command=args.command,
                    canonical_command=args.command,
                    exit_code=exit_code,
                    result=result,
                    errors=([{"type": error_type, "message": error_message}] if error_message else []),
                    warnings=captured_warnings,
                    invocation_id=context_data.run_id if context_data else None,
                ),
                ensure_ascii=True,
                default=str,
            )
        )
    elif error_message:
        print(f"lam: error: {error_message}", file=sys.stderr)
    elif args.command == "commands":
        print(command_markdown_table())
    elif result is not None:
        print(
            f"{result.workflow}: {result.status.value}; files={result.changed_files}; "
            f"rows={result.changed_rows}; report={result.report_path}"
        )
    return exit_code


def _run_command(args: argparse.Namespace, settings: Settings) -> WorkflowResult:
    if args.command == "check":
        return DailyCheckWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "file":
        return CatalogueFilingWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "doctor":
        return DoctorWorkflow(settings).run(
            initialize_ocr_models=args.initialize_ocr_models
        )
    if args.command == "commands":
        return CommandAuditWorkflow(settings).run()
    if args.command == "cleanup":
        return CleanupWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "migrate-topics":
        return TopicMigrationWorkflow(settings).run(
            dry_run=args.dry_run,
            include_topics=tuple(args.include_topic),
        )
    if args.command == "migrate-documents":
        return DocumentMigrationWorkflow(settings).run(dry_run=args.dry_run)
    if args.command == "migrate-identifiers":
        return IdentifierMigrationWorkflow(settings).run(dry_run=args.dry_run)
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

    if not any(
        (
            args.pmid, args.doi, args.title, args.arxiv_id, args.paper_uuid,
            args.row is not None, args.missing_metadata, args.incomplete_records,
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


def _json_envelope(
    *,
    command: str,
    canonical_command: str,
    exit_code: int,
    result: WorkflowResult | None,
    errors: list[Any],
    warnings: list[Any],
    invocation_id: str | None,
) -> dict[str, Any]:
    status = result.status.value if result is not None else "failed"
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "canonical_command": canonical_command,
        "status": status,
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
    if context_data is not None:
        try:
            InvocationService(
                context_data.library_root / ".library_state" / "invocations"
            ).write(
                context_data,
                arguments={"argv": raw},
                result=None,
                exit_code=10,
                duration_ms=0,
                canonical_command=command,
                status="failed",
                error_type="ParserError",
                started_at=started,
                completed_at=datetime.now().astimezone(),
            )
        except Exception:
            pass
    if "--json" in raw:
        print(
            json.dumps(
                _json_envelope(
                    command=command,
                    canonical_command=command,
                    exit_code=10,
                    result=None,
                    errors=[{"type": "ParserError", "message": message}],
                    warnings=[],
                    invocation_id=context_data.run_id if context_data else None,
                ),
                ensure_ascii=True,
            )
        )
    else:
        print(parser.format_usage().rstrip(), file=sys.stderr)
        print(f"lam: error: {message}", file=sys.stderr)
    return 10


def _raw_invocation_context(raw: list[str]) -> tuple[str, Path | None, str]:
    command = next((item for item in raw if item in COMMAND_BY_NAME), "")
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


def _minimal_context(
    command: str,
    root: Path | None,
    caller: str,
    dry_run: bool,
) -> RunContext | None:
    if not command:
        return None
    selected = root or (Path(__file__).resolve().parents[3])
    try:
        resolved = selected.expanduser().resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    safe_caller = caller if caller in CALLERS else "unknown"
    return RunContext.create(
        caller=safe_caller,
        library_root=resolved,
        dry_run=dry_run,
        top_level_command=command,
    )


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
