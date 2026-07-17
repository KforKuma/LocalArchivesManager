"""Retired pre-LAM writer.

This path intentionally performs no imports with network or workbook side
effects. Use the audited public CLI instead.
"""

from __future__ import annotations

import sys


MESSAGE = (
    "scripts/search_literature.py is retired and cannot access the network or "
    "write catalogue.xlsx. Use 'lam search' or 'python -m lam search'."
)


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 10


if __name__ == "__main__":
    raise SystemExit(main())
