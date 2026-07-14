from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from . import __version__
from .config import Settings
from .exceptions import (
    CatalogueError,
    ConfigurationError,
    FileOperationError,
    LamError,
    NetworkError,
    ProviderError,
)
from .models import MetadataLookupRequest, WorkflowStatus
from .workflows.catalogue_filing import CatalogueFilingWorkflow
from .workflows.daily_check import DailyCheckWorkflow
from .workflows.inbox_register import InboxRegisterWorkflow
from .workflows.metadata_query import MetadataQueryWorkflow


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
    subparsers.add_parser("check", parents=[common], help="Run Workflow 1")
    subparsers.add_parser("file", parents=[common], help="Run Workflow 4")
    register = subparsers.add_parser("register", parents=[common], help="Run Workflow 3")
    register.add_argument("--max-files", type=int, help="Process only the first N Inbox PDFs")
    register.add_argument(
        "--filename-only",
        action="store_true",
        help="Use filenames and catalogue data without PDF page text",
    )
    register.add_argument(
        "--skip-pdf-text",
        action="store_true",
        help="Do not extract PDF page text after filename matching fails",
    )
    search = subparsers.add_parser("search", parents=[common], help="Run Workflow 2")
    search.add_argument("--pmid")
    search.add_argument("--doi")
    search.add_argument("--title")
    search.add_argument("--arxiv-id")
    search.add_argument("--catalogue-id")
    search.add_argument("--row", type=int)
    search.add_argument("--missing-metadata", action="store_true")
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
    return parser


def _configure_logging(settings: Settings, verbose: bool) -> None:
    settings.ensure_runtime_directories()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(settings.logs_dir / "lam.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_root(args.root)
        _configure_logging(settings, args.verbose)
        lock = FileLock(settings.lock_path, timeout=0)
        context = lock if (not args.dry_run or args.command == "search") else _NullContext()
        with context:
            if args.command == "check":
                result = DailyCheckWorkflow(settings).run(dry_run=args.dry_run)
            elif args.command == "file":
                result = CatalogueFilingWorkflow(settings).run(dry_run=args.dry_run)
            elif args.command == "register":
                if args.max_files is not None and args.max_files <= 0:
                    raise ConfigurationError("--max-files must be greater than zero")
                result = InboxRegisterWorkflow(settings).run(
                    dry_run=args.dry_run,
                    max_files=args.max_files,
                    filename_only=args.filename_only,
                    skip_pdf_text=args.skip_pdf_text,
                )
            else:
                if not any(
                    (
                        args.pmid,
                        args.doi,
                        args.title,
                        args.arxiv_id,
                        args.catalogue_id,
                        args.row is not None,
                        args.missing_metadata,
                    )
                ):
                    raise ConfigurationError(
                        "search requires an identifier, title, catalogue target, or --missing-metadata"
                    )
                if args.row is not None and args.row < 2:
                    raise ConfigurationError("--row must be an Excel row number of 2 or greater")
                if args.max_results <= 0 or args.max_records <= 0:
                    raise ConfigurationError("--max-results and --max-records must be positive")
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
                result = MetadataQueryWorkflow(settings).run(
                    request,
                    dry_run=args.dry_run,
                    catalogue_row=args.row,
                    catalogue_id=args.catalogue_id,
                    missing_metadata=args.missing_metadata,
                    max_records=args.max_records,
                )
        payload = {
            "status": result.status.value,
            "report": result.report_path,
            "changed_files": result.changed_files,
            "changed_rows": result.changed_rows,
        }
        if not args.json_output:
            print(
                f"{result.workflow}: {result.status.value}; "
                f"files={result.changed_files}; rows={result.changed_rows}"
            )
        print(
            json.dumps(
                result.to_dict() if args.json_output and args.command == "search" else payload,
                ensure_ascii=False,
                default=str,
            )
        )
        if result.status == WorkflowStatus.FAILED and result.details.get("network_failure"):
            return 40
        return EXIT_CODES[result.status]
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


def _error(message: str, code: int) -> int:
    print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
    return code


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
