from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .exceptions import ConfigurationError


def _load_optional_dotenv(project_root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(project_root / ".env", override=False)


@dataclass(frozen=True, slots=True)
class Settings:
    library_root: Path
    project_root: Path
    catalogue_path: Path
    state_dir: Path
    reports_dir: Path
    logs_dir: Path
    inbox_dir: Path
    registered_dir: Path
    changes_log_path: Path
    lock_path: Path
    max_filename_length: int = 180

    @classmethod
    def from_root(cls, root: str | Path | None = None) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        _load_optional_dotenv(project_root)
        selected = root or os.getenv("LIBRARY_ROOT") or project_root.parent
        library_root = Path(selected).expanduser().resolve()
        if not library_root.is_dir():
            raise ConfigurationError(f"Library root does not exist: {library_root}")
        catalogue = library_root / "catalogue.xlsx"
        if not catalogue.is_file():
            raise ConfigurationError(f"Required catalogue is missing: {catalogue}")
        return cls(
            library_root=library_root,
            project_root=project_root,
            catalogue_path=catalogue,
            state_dir=library_root / ".library_state",
            reports_dir=library_root / ".library_state" / "reports",
            logs_dir=library_root / ".library_state" / "logs",
            inbox_dir=library_root / "Inbox",
            registered_dir=library_root / "Registered",
            changes_log_path=library_root / "library_changes.md",
            lock_path=library_root / ".library_state" / "lam.lock",
        )

    def ensure_runtime_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

