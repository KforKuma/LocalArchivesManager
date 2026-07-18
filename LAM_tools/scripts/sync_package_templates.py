from __future__ import annotations

import argparse
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SOURCE_ROOT.parent
RESOURCE_ROOT = SOURCE_ROOT / "src" / "lam" / "resources"
TEMPLATE_NAMES = ("AGENTS.md", "Workflows.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize root policy documents into package resources"
    )
    parser.add_argument("--check", action="store_true", help="Fail if templates drift")
    args = parser.parse_args(argv)
    stale: list[str] = []
    for name in TEMPLATE_NAMES:
        source = REPOSITORY_ROOT / name
        target = RESOURCE_ROOT / name
        payload = source.read_bytes()
        if args.check:
            try:
                current = target.read_bytes()
            except OSError:
                current = b""
            if current != payload:
                stale.append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    if stale:
        print(
            "Package template drift detected: "
            + ", ".join(stale)
            + "; run python scripts/sync_package_templates.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

