from __future__ import annotations

import re
from pathlib import Path

import pytest

from kaszanas_lint.command_line_interface import main
from kaszanas_lint.configuration import ALL_RULE_CODES
from kaszanas_lint.rules.rule_registry import RULE_DESCRIPTIONS, rule_documentation

DOCUMENTATION_DIRECTORY = (
    Path(__file__).resolve().parent.parent / "src" / "kaszanas_lint" / "rule_documentation"
)
REQUIRED_SECTIONS = (
    "## What it does",
    "## Why is this bad?",
    "## Example",
    "## Fix safety",
    "## When it is not reported",
    "## Configuration",
    "## Related rules",
)


def test_configuration_codes_match_the_rule_registry():
    assert set(ALL_RULE_CODES) == set(RULE_DESCRIPTIONS)


def test_every_documentation_page_belongs_to_a_rule():
    page_codes = {path.stem for path in DOCUMENTATION_DIRECTORY.glob("*.md")}
    assert page_codes == set(ALL_RULE_CODES)


@pytest.mark.parametrize("code", ALL_RULE_CODES)
def test_every_rule_has_a_complete_page(code):
    page = rule_documentation(code=code)
    assert page.startswith(f"# {code}: ")
    for section in REQUIRED_SECTIONS:
        assert section in page, f"{code}.md is missing `{section}`"
    for linked_code in re.findall(pattern=r"\]\(([A-Z]{3}\d{3})\.md\)", string=page):
        assert linked_code in ALL_RULE_CODES, f"{code}.md links to unknown rule {linked_code}"


def test_rule_command_prints_the_page(capsys):
    assert main(arguments=["rule", "mat001"]) == 0
    assert capsys.readouterr().out.startswith("# MAT001: ")
    assert main(arguments=["rules"]) == 0
    listing = capsys.readouterr().out
    assert all(code in listing for code in ALL_RULE_CODES)
