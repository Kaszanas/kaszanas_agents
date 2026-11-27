"""Apply rule fixes as text edits, plus the keyword-insertion fix used by KWA001.

Every rule finding carries fix plans that turn into `TextEdit`s once the token stream is known.
`apply_fix_groups` applies the edits of non-overlapping findings (all-or-nothing per finding) and
re-parses the result; overlapping findings are left for the next pass of the session's fix loop.

Keyword insertion points come from the token stream, not from ast offsets, because ast positions
exclude redundant parentheses (`f((a))`) and a lone generator argument shares the call's
parentheses (`f(x for x in y)`).
"""

from __future__ import annotations

import ast
import bisect
import io
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kaszanas_lint.source_text import SourceText

if TYPE_CHECKING:
    from kaszanas_lint.rules.rule_framework import FixPlan

INSIGNIFICANT_TOKEN_TYPES = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)


@dataclass(frozen=True, slots=True)
class TextEdit:
    """Replace `source[start:end]` with `replacement` (an insertion when start == end)."""

    start: int
    end: int
    replacement: str


@dataclass(frozen=True, slots=True)
class TextInsertion:
    offset: int
    text: str


@dataclass(frozen=True, slots=True)
class KeywordInsertionPlan:
    call: ast.Call
    argument_index: int
    keyword_name: str

    def text_edits(
        self, source_text: SourceText, token_table: SignificantTokenTable
    ) -> list[TextEdit] | None:
        insertions = insertions_for_plan(source_text=source_text, token_table=token_table, plan=self)
        if insertions is None:
            return None
        return [TextEdit(start=item.offset, end=item.offset, replacement=item.text) for item in insertions]


class SignificantTokenTable:
    def __init__(self, source_text: SourceText) -> None:
        self.offsets: list[int] = []
        self.strings: list[str] = []
        token_stream = tokenize.generate_tokens(io.StringIO(source_text.text).readline)
        for token in token_stream:
            if token.type in INSIGNIFICANT_TOKEN_TYPES:
                continue
            line_number, character_column = token.start
            self.offsets.append(
                source_text.offset_from_character_position(
                    line_number=line_number, character_column=character_column
                )
            )
            self.strings.append(token.string)

    def first_index_at_or_after(self, offset: int) -> int:
        return bisect.bisect_left(self.offsets, offset)

    def find_operator_at_or_after(self, offset: int, operator: str) -> int | None:
        for token_index in range(self.first_index_at_or_after(offset=offset), len(self.offsets)):
            if self.strings[token_index] == operator:
                return token_index
        return None


def _node_start(source_text: SourceText, node: ast.expr) -> int:
    return source_text.offset_from_ast_position(line_number=node.lineno, byte_column=node.col_offset)


def _node_end(source_text: SourceText, node: ast.expr) -> int:
    assert node.end_lineno is not None and node.end_col_offset is not None
    return source_text.offset_from_ast_position(line_number=node.end_lineno, byte_column=node.end_col_offset)


def insertions_for_plan(
    source_text: SourceText, token_table: SignificantTokenTable, plan: KeywordInsertionPlan
) -> list[TextInsertion] | None:
    call = plan.call
    argument = call.args[plan.argument_index]
    if plan.argument_index == 0:
        boundary = _node_end(source_text=source_text, node=call.func)
        delimiter_index = token_table.find_operator_at_or_after(offset=boundary, operator="(")
    else:
        boundary = _node_end(source_text=source_text, node=call.args[plan.argument_index - 1])
        delimiter_index = token_table.find_operator_at_or_after(offset=boundary, operator=",")
    if delimiter_index is None or delimiter_index + 1 >= len(token_table.offsets):
        return None
    delimiter_offset = token_table.offsets[delimiter_index]
    argument_start = _node_start(source_text=source_text, node=argument)

    if isinstance(argument, ast.GeneratorExp) and argument_start <= delimiter_offset:
        # `f(x for x in y)`: the generator owns the call parentheses; give it its own.
        argument_end = _node_end(source_text=source_text, node=argument)
        return [
            TextInsertion(offset=delimiter_offset + 1, text=f"{plan.keyword_name}=("),
            TextInsertion(offset=argument_end - 1, text=")"),
        ]
    insertion_offset = token_table.offsets[delimiter_index + 1]
    if insertion_offset > argument_start:
        return None  # unexpected layout: refuse rather than guess
    if isinstance(argument, ast.NamedExpr) and insertion_offset == argument_start:
        # `f(x := g())` is legal but `f(kw=x := g())` is not: a walrus keyword value needs parens.
        return [
            TextInsertion(offset=insertion_offset, text=f"{plan.keyword_name}=("),
            TextInsertion(offset=_node_end(source_text=source_text, node=argument), text=")"),
        ]
    return [TextInsertion(offset=insertion_offset, text=f"{plan.keyword_name}=")]


def _edits_overlap(first: TextEdit, second: TextEdit) -> bool:
    """Touching counts as overlapping: an insertion at the boundary of a replacement is ambiguous."""
    return first.start <= second.end and second.start <= first.end


def apply_text_edits(source: str, edits: list[TextEdit]) -> str:
    """Splice non-overlapping edits into `source` (callers guarantee they don't overlap)."""
    pieces: list[str] = []
    previous_offset = len(source)
    for edit in sorted(edits, key=lambda item: (item.start, item.end), reverse=True):
        pieces.append(source[edit.end : previous_offset])
        pieces.append(edit.replacement)
        previous_offset = edit.start
    pieces.append(source[:previous_offset])
    return "".join(reversed(pieces))


def apply_fix_groups(source: str, fix_groups: Sequence[Sequence[FixPlan]]) -> tuple[str | None, int]:
    """Apply each group's edits all-or-nothing, skipping groups that overlap an earlier one.

    Returns (fixed source or None, number of groups applied). None means nothing could be applied
    safely, including when the combined result no longer parses.
    """
    if not fix_groups:
        return None, 0
    source_text = SourceText(text=source)
    try:
        token_table = SignificantTokenTable(source_text=source_text)
    except (tokenize.TokenError, SyntaxError):
        return None, 0
    chosen_edits: list[TextEdit] = []
    applied_groups = 0
    for group in fix_groups:
        group_edits: list[TextEdit] = []
        for plan in group:
            plan_edits = plan.text_edits(source_text=source_text, token_table=token_table)
            if plan_edits is None:
                group_edits = []
                break
            group_edits.extend(plan_edits)
        if not group_edits:
            continue
        if any(_edits_overlap(first=edit, second=chosen) for edit in group_edits for chosen in chosen_edits):
            continue  # picked up by the next fix pass, after re-analysis
        chosen_edits.extend(group_edits)
        applied_groups += 1
    if not chosen_edits:
        return None, 0
    fixed_source = apply_text_edits(source=source, edits=chosen_edits)
    try:
        ast.parse(fixed_source)
    except SyntaxError:
        return None, 0
    return fixed_source, applied_groups
