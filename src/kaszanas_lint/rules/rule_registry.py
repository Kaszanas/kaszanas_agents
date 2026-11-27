"""Every rule family kaszanas-lint runs. Add new families here (and their codes to configuration)."""

from __future__ import annotations

import ast
from importlib import resources

from kaszanas_lint.rules import keyword_arguments, match_statements
from kaszanas_lint.rules.rule_framework import RuleContext, RuleFinding

RULE_FAMILIES = (keyword_arguments, match_statements)

RULE_DESCRIPTIONS: dict[str, str] = {
    code: description for family in RULE_FAMILIES for code, description in family.RULE_DESCRIPTIONS.items()
}

RULE_DOCUMENTATION_URL = (
    "https://github.com/Kaszanas/kaszanas_agents/blob/main/src/kaszanas_lint/rule_documentation/{code}.md"
)


def rule_documentation_path(code: str) -> str:
    """Path of the rule page inside the installed package (for display)."""
    return f"src/kaszanas_lint/rule_documentation/{code}.md"


def rule_documentation(code: str) -> str:
    """The Markdown page documenting one rule, read from the installed package."""
    page = resources.files("kaszanas_lint").joinpath("rule_documentation", f"{code}.md")
    return page.read_text(encoding="utf-8")


def run_all_rules(tree: ast.Module, context: RuleContext) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    for family in RULE_FAMILIES:
        findings.extend(family.check_module_tree(tree=tree, context=context))
    return findings
