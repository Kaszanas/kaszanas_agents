"""MAT rules: if/elif chains that read better as `match` (MAT001) or are really dict lookups (MAT002).

MAT001  An if/elif chain of at least `min-match-branches` branches where every test is a
        pattern on one subject. Fixable when the rewrite is provably equivalent.
MAT002  An if/elif chain that maps literal values of one subject to constants: a dict lookup is
        the constant-time alternative. Report only; suppresses MAT001 for the same chain.

`match` is a readability tool, not a speed-up: CPython compiles it to the same sequential
compare-and-jump code as if/elif (see docs/benchmarks/match-statements.md).
"""

from __future__ import annotations

import ast
import bisect
import builtins
import io
import tokenize
from dataclasses import dataclass
from functools import cached_property

from kaszanas_lint.automatic_fixes import SignificantTokenTable, TextEdit
from kaszanas_lint.external_signatures import is_standard_library_class
from kaszanas_lint.module_summarization import dotted_name_of
from kaszanas_lint.rules.rule_framework import RuleContext, RuleFinding
from kaszanas_lint.source_text import SourceText
from kaszanas_lint.symbol_resolution import ExternalSymbol
from kaszanas_lint.symbols import ClassSymbol

MATCH_STATEMENT_CODE = "MAT001"
DICTIONARY_LOOKUP_CODE = "MAT002"
MINIMUM_PYTHON_FOR_MATCH = (3, 10)

RULE_DESCRIPTIONS = {
    MATCH_STATEMENT_CODE: "if/elif chain testing one subject reads better as a `match` statement "
    "(fixable when provably equivalent).",
    DICTIONARY_LOOKUP_CODE: "if/elif chain maps literal values to constants; use a dict lookup "
    "(constant time).",
}

LITERAL_CONSTANT_TYPES = (int, float, complex, str, bytes)
GUARD_COMPONENTS_NEEDING_PARENTHESES = (ast.BoolOp, ast.IfExp, ast.Lambda, ast.NamedExpr)
LAYOUT_IGNORED_TOKEN_TYPES = frozenset(
    {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
)


@dataclass(frozen=True, slots=True)
class PatternCandidate:
    """One way of reading a test as `subject` + `case pattern [if guard]`."""

    subject_key: str
    subject: ast.expr
    pattern_text: str
    pattern_kinds: frozenset[str]
    is_fix_certain: bool
    guard_text: str | None = None
    guard_has_calls: bool = False


@dataclass(frozen=True, slots=True)
class IfElifChain:
    branches: tuple[ast.If, ...]
    else_body: tuple[ast.stmt, ...]

    @property
    def branch_count(self) -> int:
        return len(self.branches) + (1 if self.else_body else 0)

    @property
    def last_statement(self) -> ast.stmt:
        return self.else_body[-1] if self.else_body else self.branches[-1].body[-1]


@dataclass(frozen=True, slots=True)
class ChainAnalysis:
    subject: ast.expr
    subject_text: str
    branch_patterns: tuple[PatternCandidate, ...]
    is_fix_certain: bool


class SourceLayout:
    """Comments, `else` keywords and multi-line string rows, from one tokenize pass."""

    def __init__(self, source_text: SourceText) -> None:
        self.comments_by_line: dict[int, str] = {}
        self.else_keyword_positions: list[tuple[int, int]] = []
        self.multiline_token_rows: set[int] = set()
        for token in tokenize.generate_tokens(io.StringIO(source_text.text).readline):
            start_row, start_column = token.start
            end_row = token.end[0]
            if token.type == tokenize.COMMENT:
                self.comments_by_line[start_row] = token.string
            elif token.type == tokenize.NAME and token.string == "else":
                self.else_keyword_positions.append((start_row, start_column))
            if token.type not in LAYOUT_IGNORED_TOKEN_TYPES and end_row != start_row:
                self.multiline_token_rows.update(range(start_row, end_row + 1))


class MatchStatementChecker:
    def __init__(self, tree: ast.Module, context: RuleContext) -> None:
        self.tree = tree
        self.context = context
        self.source_text = context.source_text
        configuration = context.configuration
        target = configuration.target_python_version
        self.match_rule_enabled = MATCH_STATEMENT_CODE in configuration.selected_rule_codes and (
            target is None or target >= MINIMUM_PYTHON_FOR_MATCH
        )
        self.dictionary_rule_enabled = DICTIONARY_LOOKUP_CODE in configuration.selected_rule_codes
        self.minimum_branches = configuration.minimum_match_branches
        self.isinstance_is_builtin = (
            not any(
                isinstance(node, ast.Name) and node.id == "isinstance" and isinstance(node.ctx, ast.Store)
                for node in ast.walk(tree)
            )
            and "isinstance" not in context.module_summary.module_level_bound_names
        )
        self.findings: list[RuleFinding] = []

    @cached_property
    def layout(self) -> SourceLayout:
        return SourceLayout(source_text=self.source_text)

    @cached_property
    def token_table(self) -> SignificantTokenTable:
        return SignificantTokenTable(source_text=self.source_text)

    # ------------------------------------------------------------------ source helpers

    def segment(self, node: ast.AST) -> str:
        start = self.source_text.offset_from_ast_position(
            line_number=node.lineno, byte_column=node.col_offset
        )
        end = self.source_text.offset_from_ast_position(
            line_number=node.end_lineno, byte_column=node.end_col_offset
        )
        return self.source_text.text[start:end]

    def line_content(self, line_number: int) -> str:
        return self.source_text.lines[line_number - 1].rstrip("\r")

    def is_elif(self, node: ast.If) -> bool:
        return self.line_content(line_number=node.lineno)[node.col_offset :].startswith("elif")

    # ------------------------------------------------------------------ chains

    def run(self) -> list[RuleFinding]:
        if not (self.match_rule_enabled or self.dictionary_rule_enabled):
            return []
        continuation_ids: set[int] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.If) or id(node) in continuation_ids:
                continue
            chain = self.collect_chain(head=node)
            continuation_ids.update(id(branch) for branch in chain.branches[1:])
            if chain.branch_count >= self.minimum_branches:
                self.check_chain(chain=chain)
        return self.findings

    def collect_chain(self, head: ast.If) -> IfElifChain:
        branches = [head]
        current = head
        while (
            len(current.orelse) == 1
            and isinstance(current.orelse[0], ast.If)
            and self.is_elif(node=current.orelse[0])
        ):
            current = current.orelse[0]
            branches.append(current)
        return IfElifChain(branches=tuple(branches), else_body=tuple(current.orelse))

    def check_chain(self, chain: IfElifChain) -> None:
        analysis = self.analyze_chain(chain=chain)
        if analysis is None:
            return
        if self.dictionary_rule_enabled and self.is_dictionary_lookup(chain=chain, analysis=analysis):
            self.findings.append(
                RuleFinding(
                    code=DICTIONARY_LOOKUP_CODE,
                    node=chain.branches[0],
                    message=(
                        f"This {chain.branch_count}-branch if/elif chain maps `{analysis.subject_text}` to "
                        "constants; use a dict lookup instead (constant time, unlike if/elif or match)"
                    ),
                )
            )
            return
        if not self.match_rule_enabled:
            return
        fix_plans = ()
        if analysis.is_fix_certain:
            plan = MatchRewritePlan(checker=self, chain=chain, analysis=analysis)
            # Only advertise `[*]` when the rewrite can actually be produced for this layout.
            if plan.text_edits(source_text=self.source_text, token_table=self.token_table) is not None:
                fix_plans = (plan,)
        self.findings.append(
            RuleFinding(
                code=MATCH_STATEMENT_CODE,
                node=chain.branches[0],
                message=(
                    f"Use a `match` statement on `{analysis.subject_text}` instead of this "
                    f"{chain.branch_count}-branch if/elif chain"
                ),
                fix_plans=fix_plans,
            )
        )

    def analyze_chain(self, chain: IfElifChain) -> ChainAnalysis | None:
        candidates_per_branch = [self.candidates_for_test(test=branch.test) for branch in chain.branches]
        if any(not candidates for candidates in candidates_per_branch):
            return None
        common_keys = set.intersection(
            *({candidate.subject_key for candidate in candidates} for candidates in candidates_per_branch)
        )
        subject_key = next(
            (
                candidate.subject_key
                for candidate in candidates_per_branch[0]
                if candidate.subject_key in common_keys
            ),
            None,
        )
        if subject_key is None:
            return None
        chosen = tuple(
            next(candidate for candidate in candidates if candidate.subject_key == subject_key)
            for candidates in candidates_per_branch
        )
        subject = chosen[0].subject
        subject_is_plain_name = isinstance(subject, ast.Name)
        texts = [candidate.pattern_text for candidate in chosen] + [
            candidate.guard_text for candidate in chosen if candidate.guard_text
        ]
        is_fix_certain = (
            all(candidate.is_fix_certain for candidate in chosen)
            and not any("\n" in text for text in texts)
            # `match` evaluates an attribute subject once; a guard calling code could change it.
            and (subject_is_plain_name or not any(candidate.guard_has_calls for candidate in chosen))
        )
        return ChainAnalysis(
            subject=subject,
            subject_text=self.segment(node=subject),
            branch_patterns=chosen,
            is_fix_certain=is_fix_certain,
        )

    # ------------------------------------------------------------------ tests -> patterns

    @staticmethod
    def subject_key(node: ast.expr) -> str | None:
        return ast.dump(node) if dotted_name_of(expression=node) is not None else None

    def comparable_value(self, node: ast.expr) -> tuple[str, str] | None:
        """(pattern text, kind) for values a literal or value pattern compares with `==`."""
        if isinstance(node, ast.Constant) and type(node.value) in LITERAL_CONSTANT_TYPES:
            return self.segment(node=node), "literal"
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float, complex)
        ):
            return self.segment(node=node), "literal"
        dotted = dotted_name_of(expression=node) if isinstance(node, ast.Attribute) else None
        if dotted is not None:
            return dotted, "value"  # a bare name would be a capture pattern, so dots are required
        return None

    def is_known_class(self, node: ast.expr) -> bool:
        dotted = dotted_name_of(expression=node)
        if dotted is None:
            return False
        resolved = self.context.resolver.resolve_name_in_module_by_name(
            module_name=self.context.module_summary.module_name, dotted_name=dotted
        )
        if isinstance(resolved, ClassSymbol):
            return True
        if isinstance(resolved, ExternalSymbol):
            if resolved.dotted_name.startswith("builtins."):
                return isinstance(
                    getattr(builtins, resolved.dotted_name.split(".", maxsplit=1)[1], None), type
                )
            return is_standard_library_class(dotted_name=resolved.dotted_name)
        return False

    def candidates_for_test(self, test: ast.expr, allow_guard: bool = True) -> list[PatternCandidate]:
        if any(isinstance(node, ast.NamedExpr) for node in ast.walk(test)):
            return []
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            return self.alternative_candidates(values=test.values)
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) and allow_guard:
            return self.guarded_candidates(values=test.values)
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            return self.comparison_candidates(left=test.left, operator=test.ops[0], right=test.comparators[0])
        if isinstance(test, ast.Call):
            return self.isinstance_candidates(call=test)
        return []

    def alternative_candidates(self, values: list[ast.expr]) -> list[PatternCandidate]:
        per_value = [self.candidates_for_test(test=value, allow_guard=False) for value in values]
        if any(not candidates for candidates in per_value):
            return []
        common_keys = set.intersection(
            *({candidate.subject_key for candidate in group} for group in per_value)
        )
        results = []
        for candidate in per_value[0]:
            if candidate.subject_key not in common_keys:
                continue
            members = [
                next(item for item in group if item.subject_key == candidate.subject_key)
                for group in per_value
            ]
            results.append(
                PatternCandidate(
                    subject_key=candidate.subject_key,
                    subject=candidate.subject,
                    pattern_text=" | ".join(member.pattern_text for member in members),
                    pattern_kinds=frozenset().union(*(member.pattern_kinds for member in members))
                    | {"alternatives"},
                    is_fix_certain=all(member.is_fix_certain for member in members),
                )
            )
        return results

    def guarded_candidates(self, values: list[ast.expr]) -> list[PatternCandidate]:
        first_candidates = self.candidates_for_test(test=values[0], allow_guard=False)
        if not first_candidates:
            return []
        guard_parts = []
        for value in values[1:]:
            text = self.segment(node=value)
            guard_parts.append(
                f"({text})" if isinstance(value, GUARD_COMPONENTS_NEEDING_PARENTHESES) else text
            )
        guard_text = " and ".join(guard_parts)
        guard_has_calls = any(isinstance(node, ast.Call) for value in values[1:] for node in ast.walk(value))
        return [
            PatternCandidate(
                subject_key=candidate.subject_key,
                subject=candidate.subject,
                pattern_text=candidate.pattern_text,
                pattern_kinds=candidate.pattern_kinds | {"guard"},
                is_fix_certain=candidate.is_fix_certain,
                guard_text=guard_text,
                guard_has_calls=guard_has_calls,
            )
            for candidate in first_candidates
        ]

    def comparison_candidates(
        self, left: ast.expr, operator: ast.cmpop, right: ast.expr
    ) -> list[PatternCandidate]:
        results: list[PatternCandidate] = []
        match operator:
            case ast.Eq():
                for subject_side, value_side in ((left, right), (right, left)):
                    key = self.subject_key(node=subject_side)
                    value = self.comparable_value(node=value_side)
                    if key is None or value is None or ast.dump(value_side) == key:
                        continue
                    results.append(
                        PatternCandidate(
                            subject_key=key,
                            subject=subject_side,
                            pattern_text=value[0],
                            pattern_kinds=frozenset({value[1]}),
                            is_fix_certain=True,
                        )
                    )
            case ast.Is():
                for subject_side, value_side in ((left, right), (right, left)):
                    key = self.subject_key(node=subject_side)
                    is_singleton = isinstance(value_side, ast.Constant) and (
                        value_side.value is None or isinstance(value_side.value, bool)
                    )
                    if key is not None and is_singleton:
                        results.append(
                            PatternCandidate(
                                subject_key=key,
                                subject=subject_side,
                                pattern_text=repr(value_side.value),
                                pattern_kinds=frozenset({"singleton"}),
                                is_fix_certain=True,
                            )
                        )
            case ast.In() if isinstance(right, ast.Tuple | ast.List):
                key = self.subject_key(node=left)
                values = [self.comparable_value(node=element) for element in right.elts]
                if key is not None and values and all(value is not None for value in values):
                    results.append(
                        PatternCandidate(
                            subject_key=key,
                            subject=left,
                            pattern_text=" | ".join(value[0] for value in values),
                            pattern_kinds=frozenset(value[1] for value in values) | {"alternatives"},
                            is_fix_certain=True,
                        )
                    )
        return results

    def isinstance_candidates(self, call: ast.Call) -> list[PatternCandidate]:
        if not (
            self.isinstance_is_builtin
            and isinstance(call.func, ast.Name)
            and call.func.id == "isinstance"
            and len(call.args) == 2
            and not call.keywords
        ):
            return []
        subject, class_expression = call.args
        key = self.subject_key(node=subject)
        classes = (
            list(class_expression.elts) if isinstance(class_expression, ast.Tuple) else [class_expression]
        )
        if key is None or not classes or any(dotted_name_of(expression=item) is None for item in classes):
            return []
        return [
            PatternCandidate(
                subject_key=key,
                subject=subject,
                pattern_text=" | ".join(f"{dotted_name_of(expression=item)}()" for item in classes),
                pattern_kinds=frozenset({"class"}),
                # `case TYPES():` fails at runtime when TYPES is a tuple variable: verify each class.
                is_fix_certain=all(self.is_known_class(node=item) for item in classes),
            )
        ]

    # ------------------------------------------------------------------ MAT002

    def is_dictionary_lookup(self, chain: IfElifChain, analysis: ChainAnalysis) -> bool:
        allowed_kinds = {"literal", "value", "alternatives"}
        if any(not candidate.pattern_kinds <= allowed_kinds for candidate in analysis.branch_patterns):
            return False
        bodies = [list(branch.body) for branch in chain.branches]
        if chain.else_body:
            bodies.append(list(chain.else_body))
        if any(len(body) != 1 for body in bodies):
            return False
        statements = [body[0] for body in bodies]
        if all(
            isinstance(statement, ast.Return) and self.is_constant_like(node=statement.value)
            for statement in statements
        ):
            return True
        every_body_assigns_a_constant = all(
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and self.is_constant_like(node=statement.value)
            for statement in statements
        )
        if not every_body_assigns_a_constant:
            return False
        return len({statement.targets[0].id for statement in statements}) == 1

    def is_constant_like(self, node: ast.expr | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return isinstance(node.operand, ast.Constant)
        if isinstance(node, ast.Tuple):
            return all(self.is_constant_like(node=element) for element in node.elts)
        return isinstance(node, ast.Attribute) and dotted_name_of(expression=node) is not None


@dataclass(frozen=True, slots=True)
class MatchRewritePlan:
    """Replace a whole if/elif chain with an equivalent `match` statement."""

    checker: MatchStatementChecker
    chain: IfElifChain
    analysis: ChainAnalysis

    def text_edits(
        self, source_text: SourceText, token_table: SignificantTokenTable
    ) -> list[TextEdit] | None:
        try:
            return self._build_text_edits(source_text=source_text, token_table=token_table)
        except _RewriteRefused:
            return None

    def _build_text_edits(
        self, source_text: SourceText, token_table: SignificantTokenTable
    ) -> list[TextEdit] | None:
        checker = self.checker
        layout = checker.layout
        branches = self.chain.branches
        head = branches[0]
        head_line = checker.line_content(line_number=head.lineno)
        indent = head_line[: head.col_offset]
        if indent.strip() or "\t" in indent or not head_line[len(indent) :].startswith("if"):
            return None
        last_line = self.chain.last_statement.end_lineno
        if any(row in layout.multiline_token_rows for row in range(head.lineno, last_line + 1)):
            return None  # re-indenting would change the contents of a multi-line string
        newline = "\r\n" if source_text.lines[head.lineno - 1].endswith("\r") else "\n"

        unit = self._indent_unit(indent=indent, first_body_statement=head.body[0], header_line=head.lineno)
        if unit is None:
            return None

        output = [f"{indent}match {self.analysis.subject_text}:"]
        for index, (branch, candidate) in enumerate(
            zip(branches, self.analysis.branch_patterns, strict=True)
        ):
            header_end_line = self._header_end_line(test=branch.test, token_table=token_table)
            if header_end_line is None or branch.body[0].lineno <= header_end_line:
                return None  # body on the header line, or an unexpected layout
            header_comments = [
                layout.comments_by_line[row]
                for row in range(branch.lineno, header_end_line + 1)
                if row in layout.comments_by_line
            ]
            if header_end_line > branch.lineno and header_comments:
                return None  # comments inside a multi-line condition would be lost
            if index + 1 < len(branches):
                block_end_line = branches[index + 1].lineno - 1
            elif self.chain.else_body:
                else_line = self._else_line(after_line=branch.body[-1].end_lineno, indent=indent)
                if else_line is None:
                    return None
                block_end_line = else_line - 1
            else:
                block_end_line = last_line
            case_line = f"{indent}{unit}case {candidate.pattern_text}"
            if candidate.guard_text:
                case_line += f" if {candidate.guard_text}"
            case_line += ":" + (f"  {header_comments[0]}" if header_comments else "")
            output.append(case_line)
            output.extend(
                self._reindented(first_line=header_end_line + 1, last_line=block_end_line, unit=unit)
            )

        if self.chain.else_body:
            else_line = self._else_line(after_line=branches[-1].body[-1].end_lineno, indent=indent)
            if else_line is None or self.chain.else_body[0].lineno <= else_line:
                return None
            else_comment = layout.comments_by_line.get(else_line)
            output.append(f"{indent}{unit}case _:" + (f"  {else_comment}" if else_comment else ""))
            output.extend(self._reindented(first_line=else_line + 1, last_line=last_line, unit=unit))

        start = source_text.line_start_offsets[head.lineno - 1]
        end = source_text.line_start_offsets[last_line - 1] + len(checker.line_content(line_number=last_line))
        return [TextEdit(start=start, end=end, replacement=newline.join(output))]

    def _indent_unit(self, indent: str, first_body_statement: ast.stmt, header_line: int) -> str | None:
        if first_body_statement.lineno == header_line:
            return None
        body_line = self.checker.line_content(line_number=first_body_statement.lineno)
        body_indent = body_line[: len(body_line) - len(body_line.lstrip())]
        unit = body_indent[len(indent) :]
        if not body_indent.startswith(indent) or not unit or unit.strip(" "):
            return None
        return unit

    def _header_end_line(self, test: ast.expr, token_table: SignificantTokenTable) -> int | None:
        source_text = self.checker.source_text
        test_end = source_text.offset_from_ast_position(
            line_number=test.end_lineno, byte_column=test.end_col_offset
        )
        colon_index = token_table.find_operator_at_or_after(offset=test_end, operator=":")
        if colon_index is None:
            return None
        return bisect.bisect_right(source_text.line_start_offsets, token_table.offsets[colon_index])

    def _else_line(self, after_line: int, indent: str) -> int | None:
        for row, column in self.checker.layout.else_keyword_positions:
            if row > after_line and column == len(indent):
                return row
        return None

    def _reindented(self, first_line: int, last_line: int, unit: str) -> list[str]:
        lines = []
        for line_number in range(first_line, last_line + 1):
            content = self.checker.line_content(line_number=line_number)
            if "\t" in content[: len(content) - len(content.lstrip())]:
                raise _RewriteRefused
            lines.append(f"{unit}{content}" if content.strip() else content)
        return lines


class _RewriteRefused(Exception):
    """Raised from deep inside the rewrite when a layout can't be reproduced exactly."""


def check_module_tree(tree: ast.Module, context: RuleContext) -> list[RuleFinding]:
    return MatchStatementChecker(tree=tree, context=context).run()
