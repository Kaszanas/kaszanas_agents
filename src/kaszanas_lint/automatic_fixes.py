"""Turn "argument N of this call should be `name=`" plans into safe text insertions.

Insertion points come from the token stream, not from ast offsets, because ast positions exclude
redundant parentheses (`f((a))`) and a lone generator argument shares the call's parentheses
(`f(x for x in y)`).
"""

from __future__ import annotations

import ast
import bisect
import io
import tokenize
from dataclasses import dataclass

from kaszanas_lint.source_text import SourceText

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
class KeywordInsertionPlan:
    call: ast.Call
    argument_index: int
    keyword_name: str


@dataclass(frozen=True, slots=True)
class TextInsertion:
    offset: int
    text: str


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


def apply_keyword_insertions(source: str, plans: list[KeywordInsertionPlan]) -> str | None:
    """Return the fixed source, or None when nothing could be applied safely."""
    if not plans:
        return None
    source_text = SourceText(text=source)
    try:
        token_table = SignificantTokenTable(source_text=source_text)
    except (tokenize.TokenError, SyntaxError):
        return None
    insertions: list[TextInsertion] = []
    for plan in plans:
        plan_insertions = insertions_for_plan(source_text=source_text, token_table=token_table, plan=plan)
        if plan_insertions is not None:
            insertions.extend(plan_insertions)
    if not insertions:
        return None
    pieces: list[str] = []
    previous_offset = len(source)
    for insertion in sorted(insertions, key=lambda item: item.offset, reverse=True):
        pieces.append(source[insertion.offset : previous_offset])
        pieces.append(insertion.text)
        previous_offset = insertion.offset
    pieces.append(source[:previous_offset])
    fixed_source = "".join(reversed(pieces))
    try:
        ast.parse(fixed_source)
    except SyntaxError:
        return None
    return fixed_source
