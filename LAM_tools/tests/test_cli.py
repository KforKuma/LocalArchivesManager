from __future__ import annotations

import json
from pathlib import Path

import pytest

from lam.cli import main
from lam.command_registry import COMMANDS, command_markdown_table


def test_cli_check_emits_json_and_stable_exit_codes(library_factory, capsys):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Example",
                "topic_folder": "Topic_A",
                "pdf_status": "registered",
                "pdf_filename": "paper.pdf",
                "pdf_relative_path": "Registered/paper.pdf",
            }
        ],
        {"Registered/paper.pdf": b"paper"},
    )
    first_code = main(["check", "--root", str(root), "--json"])
    first_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    second_code = main(["check", "--root", str(root), "--json"])
    second_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first_code == 0
    assert first_payload["status"] == "success"
    assert second_code == 3
    assert second_payload["status"] == "no_changes"


def test_cli_register_help_and_max_files_validation(capsys):
    from lam.cli import build_parser

    args = build_parser().parse_args(
        ["register", "--max-files", "2", "--filename-only"]
    )
    assert args.command == "register"
    assert args.max_files == 2
    assert args.filename_only is True
    ocr_args = build_parser().parse_args(
        [
            "register",
            "--ocr",
            "always",
            "--ocr-language",
            "en",
            "--ocr-language",
            "ch_sim",
            "--ocr-dpi",
            "250",
            "--ocr-gpu",
            "false",
        ]
    )
    assert ocr_args.ocr == "always"
    assert ocr_args.ocr_languages == ["en", "ch_sim"]
    assert ocr_args.ocr_dpi == 250
    assert ocr_args.ocr_gpu == "false"
    assert build_parser().parse_args(["doctor"]).command == "doctor"
    repair = build_parser().parse_args(["repair-publication-types", "--dry-run"])
    assert repair.command == "repair-publication-types"
    assert repair.dry_run is True
    normalize = build_parser().parse_args(["normalize-records", "--dry-run"])
    assert normalize.command == "normalize-records"
    assert normalize.dry_run is True
    cleanup = build_parser().parse_args(["cleanup", "--dry-run"])
    assert cleanup.command == "cleanup"
    assert cleanup.dry_run is True
    assert cleanup.apply is False


def test_cli_cleanup_requires_explicit_mode(library_factory, capsys):
    root = library_factory([])
    assert main(["cleanup", "--root", str(root)]) == 10
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "exactly one" in payload["error"]


def test_cli_version_and_search_arguments(capsys):
    from lam.cli import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "0.5.1"

    args = build_parser().parse_args(
        [
            "search",
            "--doi",
            "10.1000/example",
            "--provider",
            "pubmed",
            "--offline",
            "--no-cache-write",
        ]
    )
    assert args.command == "search"
    assert args.provider == "pubmed"
    assert args.offline is True
    assert args.no_cache_write is True
    batch = build_parser().parse_args(
        ["search", "--incomplete-records", "--normalize-existing"]
    )
    assert batch.incomplete_records is True
    assert batch.normalize_existing is True


def test_commands_registry_matches_parser_and_is_machine_readable(
    library_factory, capsys
):
    from lam.cli import build_parser

    root = library_factory([])
    parser = build_parser()
    subparser_action = next(
        action for action in parser._actions if action.dest == "command"
    )
    assert set(subparser_action.choices) == {item.name for item in COMMANDS}
    for definition in COMMANDS:
        assert definition.purpose in subparser_action.choices[definition.name].format_help()
    code = main(["commands", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert {item["name"] for item in payload["commands"]} == {
        item.name for item in COMMANDS
    }
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )
    assert command_markdown_table() in readme


def test_agent_invocation_is_logged_once_without_secrets(
    library_factory, capsys, monkeypatch
):
    root = library_factory([])
    monkeypatch.setenv("NCBI_API_KEY", "do-not-log-this-secret")
    code = main(
        ["check", "--root", str(root), "--json", "--caller", "agent"]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert payload["command"] == "check"
    assert payload["caller"] == "agent"
    assert payload["version"] == "0.5.1"
    assert payload["invocation_id"]
    assert set(
        (
            "command",
            "workflow",
            "version",
            "caller",
            "status",
            "dry_run",
            "completed",
            "skipped",
            "needs_review",
            "failures",
            "final_check",
            "invocation_id",
        )
    ).issubset(payload)
    logs = list((root / ".library_state" / "invocations").glob("*.jsonl"))
    assert len(logs) == 1
    lines = logs[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    invocation = json.loads(lines[0])
    assert invocation["caller"] == "agent"
    assert invocation["invocation_id"] == payload["invocation_id"]
    assert "do-not-log-this-secret" not in lines[0]


def test_nested_final_check_does_not_duplicate_top_level_invocation(
    library_factory, capsys
):
    root = library_factory(
        [
            {
                "id": "P1",
                "title": "Example",
                "topic_folder": "Topic_A",
                "pdf_status": "registered",
                "pdf_filename": "paper.pdf",
                "pdf_relative_path": "Registered/paper.pdf",
            }
        ],
        {"Registered/paper.pdf": b"paper"},
    )
    code = main(["file", "--root", str(root), "--json", "--caller", "agent"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert payload["final_check"]
    log = next((root / ".library_state" / "invocations").glob("*.jsonl"))
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
