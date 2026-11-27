"""Types shared by every rule family: what a rule receives and what it reports."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kaszanas_lint.automatic_fixes import SignificantTokenTable, TextEdit
    from kaszanas_lint.configuration import LintConfiguration
    from kaszanas_lint.source_text import SourceText
    from kaszanas_lint.symbol_resolution import SymbolResolver
    from kaszanas_lint.symbols import ModuleSummary


class FixPlan(Protocol):
    """Something that can turn itself into text edits once the token stream is known."""

    def text_edits(
        self, source_text: SourceText, token_table: SignificantTokenTable
    ) -> list[TextEdit] | None: ...


@dataclass(frozen=True, slots=True)
class RuleContext:
    module_summary: ModuleSummary
    resolver: SymbolResolver
    configuration: LintConfiguration
    source_text: SourceText


@dataclass(frozen=True, slots=True)
class RuleFinding:
    """One violation. `node` provides the reported range; all `fix_plans` apply together or not at all."""

    code: str
    node: ast.AST
    message: str
    fix_plans: tuple[FixPlan, ...] = ()
