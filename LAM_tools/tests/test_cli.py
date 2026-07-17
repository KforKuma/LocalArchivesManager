from __future__ import annotations

import json
from pathlib import Path

import pytest

from lam import __version__
from lam.cli import CliParserError, build_parser, main
from lam.command_registry import COMMANDS, command_markdown_table
from lam.models import WorkflowResult
from lam.services.invocation_service import InvocationService


ENVELOPE_FIELDS = {
    "schema_version",
    "command",
    "canonical_command",
    "status",
    "exit_code",
    "errors",
    "warnings",
    "report_path",
    "invocation_id",
    "details",
}


def _json_stdout(capsys) -> dict:
    output = capsys.readouterr().out.strip()
    assert output.count("\n") == 0
    return json.loads(output)


def test_cli_check_emits_stable_envelope_and_exit_codes(current_library_factory, capsys):
    root = current_library_factory()
    first_code = main(["--root", str(root), "--json", "check"])
    first = _json_stdout(capsys)
    second_code = main(["check", "--root", str(root), "--json"])
    second = _json_stdout(capsys)
    assert first_code == 0
    assert first["status"] == "success"
    assert second_code == 3
    assert second["status"] == "no_changes"
    assert set(first) == ENVELOPE_FIELDS
    assert first["schema_version"] == "1"


def test_top_level_and_legacy_shared_option_order_are_both_supported():
    top = build_parser().parse_args(
        ["--root", "D:/library", "--json", "--caller", "agent", "register", "--offline"]
    )
    legacy = build_parser().parse_args(
        ["register", "--root", "D:/library", "--json", "--caller", "agent", "--offline"]
    )
    for parsed in (top, legacy):
        assert parsed.command == "register"
        assert parsed.root == Path("D:/library")
        assert parsed.json_output is True
        assert parsed.caller == "agent"
        assert parsed.offline is True


def test_parser_arguments_and_explicit_maintenance_modes():
    register = build_parser().parse_args(
        ["register", "--ocr", "always", "--ocr-language", "en", "--ocr-dpi", "250", "--no-cache-write"]
    )
    assert register.ocr == "always"
    assert register.ocr_languages == ["en"]
    assert register.no_cache_write is True
    normalize = build_parser().parse_args(
        ["normalize-records", "--dry-run", "--offline", "--max-records", "5"]
    )
    assert normalize.dry_run is True
    assert normalize.offline is True
    with pytest.raises(CliParserError):
        build_parser().parse_args(["normalize-records"])
    with pytest.raises(CliParserError):
        build_parser().parse_args(["repair-publication-types"])


def test_diagnostic_help_does_not_expose_invalid_modes():
    parser = build_parser()
    action = next(item for item in parser._actions if item.dest == "command")
    for name in ("doctor", "commands"):
        help_text = action.choices[name].format_help()
        assert "--dry-run" not in help_text
        assert "--apply" not in help_text
    assert "--initialize-ocr-models" in action.choices["doctor"].format_help()


def test_cli_version_uses_package_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_parser_json_error_uses_exit_10_and_stable_envelope(
    current_library_factory, capsys
):
    root = current_library_factory()
    code = main(["--root", str(root), "--json", "not-a-command"])
    payload = _json_stdout(capsys)
    assert code == 10
    assert payload["exit_code"] == 10
    assert payload["status"] == "failed"
    assert payload["errors"][0]["type"] == "ParserError"
    assert set(payload) == ENVELOPE_FIELDS


def test_dispatched_parser_error_writes_minimal_invocation(
    current_library_factory, capsys
):
    root = current_library_factory()
    code = main(["--root", str(root), "--json", "cleanup"])
    payload = _json_stdout(capsys)
    assert code == 10
    assert payload["command"] == "cleanup"
    assert payload["invocation_id"]
    log = next((root / ".library_state" / "invocations").glob("*.jsonl"))
    invocation = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert invocation["command"] == "cleanup"
    assert invocation["error_type"] == "ParserError"


def test_commands_registry_matches_parser_and_is_machine_readable(
    current_library_factory, capsys
):
    root = current_library_factory()
    parser = build_parser()
    action = next(item for item in parser._actions if item.dest == "command")
    assert set(action.choices) == {item.name for item in COMMANDS}
    for definition in COMMANDS:
        assert definition.purpose in action.choices[definition.name].format_help()
        assert definition.actual_exit_codes != ()
    assert len({item.actual_exit_codes for item in COMMANDS}) > 1
    code = main(["--root", str(root), "--json", "commands"])
    payload = _json_stdout(capsys)
    commands = payload["details"]["commands"]
    assert code == 0
    assert {item["name"] for item in commands} == {item.name for item in COMMANDS}
    assert all("actual_exit_codes" in item for item in commands)
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert command_markdown_table() in readme


def test_failed_dispatched_invocation_is_logged(current_library_factory, capsys):
    root = current_library_factory()
    (root / "~$catalogue.xlsx").write_text("locked", encoding="utf-8")
    code = main(["--root", str(root), "--json", "check", "--caller", "agent"])
    payload = _json_stdout(capsys)
    assert code == 20
    assert payload["status"] == "failed"
    log = next((root / ".library_state" / "invocations").glob("*.jsonl"))
    invocation = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert invocation["command"] == "check"
    assert invocation["canonical_command"] == "check"
    assert invocation["status"] == "failed"
    assert invocation["exit_code"] == 20
    assert invocation["error_type"] == "CatalogueError"
    assert invocation["started_at"]
    assert invocation["completed_at"]


def test_json_suppresses_third_party_stdout(current_library_factory, capsys, monkeypatch):
    root = current_library_factory()

    def noisy_run(self, *, initialize_ocr_models=False):
        print("third party noise")
        result = WorkflowResult("doctor")
        result.details = {
            "uses_network": initialize_ocr_models,
            "may_download_models": initialize_ocr_models,
        }
        return result

    monkeypatch.setattr("lam.cli.DoctorWorkflow.run", noisy_run)
    code = main(["--root", str(root), "--json", "doctor"])
    payload = _json_stdout(capsys)
    assert code == 0
    assert payload["warnings"][0]["issue"] == "suppressed_third_party_stdout"


def test_embedded_cli_closes_owned_log_handler(current_library_factory, capsys):
    root = current_library_factory()
    assert main(["--root", str(root), "--json", "commands"]) == 0
    _json_stdout(capsys)
    log = root / ".library_state" / "logs" / "lam.log"
    renamed = log.with_name("lam.closed.log")
    log.replace(renamed)
    assert renamed.is_file()


def test_agent_invocation_sanitizes_sensitive_arguments(current_library_factory, capsys):
    root = current_library_factory()
    code = main(
        ["--root", str(root), "--json", "--caller", "agent", "search", "--title", "private title", "--offline", "--dry-run"]
    )
    _json_stdout(capsys)
    assert code in {2, 3, 40}
    log = next((root / ".library_state" / "invocations").glob("*.jsonl"))
    raw = log.read_text(encoding="utf-8").splitlines()[-1]
    invocation = json.loads(raw)
    assert invocation["caller"] == "agent"
    assert invocation["sanitized_arguments"]["title"] == "[REDACTED]"
    assert "private title" not in raw
    assert InvocationService._sanitize(
        {"argv": ["search", "--title", "private title", "--json"]}
    )["argv"] == ["search", "--title", "[REDACTED]", "--json"]
