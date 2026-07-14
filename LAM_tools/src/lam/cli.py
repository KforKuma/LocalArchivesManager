from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from .config import Settings
from .exceptions import CatalogueError, ConfigurationError, FileOperationError, LamError
from .models import WorkflowStatus
from .workflows.catalogue_filing import CatalogueFilingWorkflow
from .workflows.daily_check import DailyCheckWorkflow
from .workflows.inbox_register import InboxRegisterWorkflow


EXIT_CODES = {
    WorkflowStatus.SUCCESS: 0,
    WorkflowStatus.NEEDS_REVIEW: 2,
    WorkflowStatus.NO_CHANGES: 3,
    WorkflowStatus.FAILED: 30,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lam", description="Local Archives Manager")
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
        context = lock if not args.dry_run else _NullContext()
        with context:
            if args.command == "check":
                result = DailyCheckWorkflow(settings).run(dry_run=args.dry_run)
            elif args.command == "file":
                result = CatalogueFilingWorkflow(settings).run(dry_run=args.dry_run)
            else:
                if args.max_files is not None and args.max_files <= 0:
                    raise ConfigurationError("--max-files must be greater than zero")
                result = InboxRegisterWorkflow(settings).run(
                    dry_run=args.dry_run,
                    max_files=args.max_files,
                    filename_only=args.filename_only,
                    skip_pdf_text=args.skip_pdf_text,
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
        print(json.dumps(payload, ensure_ascii=False))
        return EXIT_CODES[result.status]
    except Timeout:
        return _error("Another modifying LAM process holds the library lock", 10)
    except ConfigurationError as exc:
        return _error(str(exc), 10)
    except CatalogueError as exc:
        return _error(str(exc), 20)
    except FileOperationError as exc:
        return _error(str(exc), 30)
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
