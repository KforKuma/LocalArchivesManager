from __future__ import annotations

import json

from lam.cli import main


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
