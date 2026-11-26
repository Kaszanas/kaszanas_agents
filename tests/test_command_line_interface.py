from __future__ import annotations

import json

import pytest

from kaszanas_lint.command_line_interface import main

SOURCE = "def area(width, height):\n    return width * height\n\narea(1, 2)\n"


@pytest.fixture
def project(make_project, monkeypatch):
    project_root = make_project(files={"main.py": SOURCE})
    monkeypatch.chdir(project_root)
    return project_root


def test_check_reports_and_exits_one(project, capsys):
    assert main(arguments=["check", "--no-cache"]) == 1
    output = capsys.readouterr().out
    assert "main.py:4:1: KWA001 [*]" in output
    assert "1 fixable" in output


def test_bare_paths_default_to_check(project, capsys):
    assert main(arguments=["main.py", "--no-cache"]) == 1


def test_fix_writes_file_and_exits_zero(project, capsys):
    assert main(arguments=["check", "--fix"]) == 0
    assert (project / "main.py").read_text(encoding="utf-8").endswith("area(width=1, height=2)\n")
    assert "Fixed 1 error." in capsys.readouterr().out
    # Second run hits the summary cache and stays clean.
    assert main(arguments=["check"]) == 0
    assert (project / ".kaszanas_lint_cache" / ".gitignore").exists()


def test_diff_does_not_write(project, capsys):
    assert main(arguments=["check", "--diff", "--no-cache"]) == 1
    output = capsys.readouterr().out
    assert "+area(width=1, height=2)" in output
    assert (project / "main.py").read_text(encoding="utf-8") == SOURCE


def test_json_output(project, capsys):
    main(arguments=["check", "--output-format", "json", "--no-cache"])
    (entry,) = json.loads(capsys.readouterr().out)
    assert entry["code"] == "KWA001" and entry["fixable"] is True


def test_github_output_and_exit_zero(project, capsys):
    assert main(arguments=["check", "--output-format", "github", "--exit-zero", "--no-cache"]) == 0
    assert capsys.readouterr().out.startswith("::error title=kaszanas-lint (KWA001)")


def test_select_ignore_and_rules_command(project, capsys):
    assert main(arguments=["check", "--ignore", "KWA001", "--no-cache"]) == 0
    assert main(arguments=["rules"]) == 0
    assert "KWA002" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(arguments=["check", "--select", "NOPE1"])
