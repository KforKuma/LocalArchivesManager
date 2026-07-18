"""PyInstaller entry point that preserves package-relative imports."""

from lam.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
