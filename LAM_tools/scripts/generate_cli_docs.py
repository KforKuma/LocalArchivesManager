from __future__ import annotations

import argparse
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from lam.release_docs import render_cli_commands, write_cli_commands  # noqa: E402


DEFAULT_OUTPUT = SOURCE_ROOT / "docs" / "CLI_COMMANDS.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the public CLI reference")
    parser.add_argument("--check", action="store_true", help="Fail if the generated file is stale")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    expected = render_cli_commands()
    if args.check:
        try:
            current = args.output.read_text(encoding="utf-8")
        except OSError:
            print(f"CLI documentation is missing: {args.output}", file=sys.stderr)
            return 1
        if current != expected:
            print(
                f"CLI documentation drift detected: {args.output}; run "
                "python scripts/generate_cli_docs.py",
                file=sys.stderr,
            )
            return 1
        return 0
    write_cli_commands(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

