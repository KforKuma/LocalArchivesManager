from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from lam.config import DEFAULT_USER_AGENT, Settings
from lam.exceptions import ConfigurationError
from lam.versions import JSON_SCHEMA_VERSION, LIBRARY_SCHEMA_VERSION, PACKAGE_VERSION


def test_settings_never_infers_library_from_source_parent(monkeypatch):
    monkeypatch.delenv("LIBRARY_ROOT", raising=False)
    monkeypatch.delenv("LAM_TESTING", raising=False)

    with pytest.raises(ConfigurationError, match="pass --root or set LIBRARY_ROOT"):
        Settings.from_root(require_catalogue=False, allow_missing_root=True)


def test_standalone_source_root_has_environment_and_matching_metadata():
    source_root = Path(__file__).resolve().parents[1]
    pyproject_text = (source_root / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    compatibility_pyproject = tomllib.loads(
        (source_root.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    environment = (source_root / "environment.yml").read_text(encoding="utf-8")

    assert pyproject["project"]["version"] == PACKAGE_VERSION
    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert pyproject["build-system"] == compatibility_pyproject["build-system"]
    assert pyproject["project"]["dependencies"] == compatibility_pyproject[
        "project"
    ]["dependencies"]
    assert pyproject["project"]["optional-dependencies"] == compatibility_pyproject[
        "project"
    ]["optional-dependencies"]
    assert "python=3.14" in environment
    assert "poppler" in environment
    assert "-e .[dev]" in environment
    assert DEFAULT_USER_AGENT == f"LAM/{PACKAGE_VERSION}"


def test_status_library_reports_all_public_versions(current_library_factory):
    from lam.workflows.status import StatusWorkflow

    root = current_library_factory()
    result = StatusWorkflow(Settings.from_root(root)).library()

    assert result.details["package_version"] == PACKAGE_VERSION
    assert result.details["library_schema_version"] == LIBRARY_SCHEMA_VERSION
    assert result.details["json_schema_version"] == JSON_SCHEMA_VERSION
