"""CMT rules: decorative banner and divider comments in source code.

CMT001  A section banner made of decoration runs around a title (`# ---- helpers ----`,
        `#### Helpers ####`), or a title boxed between two divider lines. Fixed to `# Helpers:`.
CMT002  A bare divider line (`# ----------`, `# ==========`). Fixed by deleting the line.

Comments are not part of the AST, so this family works on the token stream. Only full-line
comments are considered; a comment after code is never reported.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from enum import Enum

from kaszanas_lint.automatic_fixes import SignificantTokenTable, TextEdit
from kaszanas_lint.rules.rule_framework import RuleContext, RuleFinding, SourceLocation
from kaszanas_lint.source_text import SourceText

BANNER_COMMENT_CODE = "CMT001"
DIVIDER_COMMENT_CODE = "CMT002"

RULE_DESCRIPTIONS = {
    BANNER_COMMENT_CODE: "Decorative banner comment around a section title; write `# Title:` instead "
    "(fixable).",
    DIVIDER_COMMENT_CODE: "Decorative divider comment line with no text; delete it (fixable).",
}

DECORATION_CHARACTERS = "-=*#~_+"
MINIMUM_DECORATION_RUN = 4


def _decoration_run(group_name: str) -> str:
    """Regex for MINIMUM_DECORATION_RUN or more copies of one decoration character."""
    character_class = "[" + re.escape(DECORATION_CHARACTERS) + "]"
    return f"(?P<{group_name}>{character_class})(?P={group_name}){{{MINIMUM_DECORATION_RUN - 1},}}"


# One decoration character repeated, optionally with single spaces (`= = = =`). Wider gaps are
# table-header underlines (`# ----    -----`), not dividers.
DIVIDER_PATTERN = re.compile(
    rf"^(?P<character>[{re.escape(DECORATION_CHARACTERS)}])"
    rf"(?: ?(?P=character)){{{MINIMUM_DECORATION_RUN - 1},}}$"
)
BANNER_PATTERN = re.compile(
    rf"^(?P<leading>{_decoration_run(group_name='leading_character')})?\s*(?P<title>.*?)\s*"
    rf"(?P<trailing>{_decoration_run(group_name='trailing_character')})?$"
)
# Tool directives must keep their exact text, whatever decoration surrounds them.
DIRECTIVE_PATTERN = re.compile(
    pattern=r"^#!|\b(noqa|type:|fmt:|pragma|pylint:|pyright:|mypy:|isort:|ruff:|coding[:=])",
    flags=re.IGNORECASE,
)


class CommentKind(Enum):
    TEXT = "text"
    DIVIDER = "divider"
    BANNER = "banner"


@dataclass(frozen=True, slots=True)
class FullLineComment:
    line_number: int
    column: int  # character column of the `#`
    text: str  # the comment token, starting with `#`
    kind: CommentKind
    title: str = ""  # cleaned title for banners, stripped text for plain comments


def classify_comment(comment_text: str) -> tuple[CommentKind, str]:
    """Kind of one comment, plus the cleaned title (banners) or the stripped text (plain comments)."""
    if comment_text.startswith("#!"):
        return CommentKind.TEXT, comment_text  # a shebang is content: never merged into a heading
    hash_count = len(comment_text) - len(comment_text.lstrip("#"))
    body = comment_text.lstrip("#").strip()
    if not body:
        return (CommentKind.DIVIDER if hash_count >= MINIMUM_DECORATION_RUN else CommentKind.TEXT), ""
    if DIVIDER_PATTERN.match(body):
        return CommentKind.DIVIDER, ""
    if DIRECTIVE_PATTERN.search(body):
        return CommentKind.TEXT, body
    banner_match = BANNER_PATTERN.match(body)
    if banner_match is None:
        return CommentKind.TEXT, body
    has_leading_run = hash_count >= MINIMUM_DECORATION_RUN or bool(banner_match["leading"])
    has_trailing_run = bool(banner_match["trailing"])
    raw_title = banner_match["title"]
    if not (has_leading_run or has_trailing_run) or _looks_like_ascii_diagram(
        raw_title=raw_title, has_leading_run=has_leading_run, has_trailing_run=has_trailing_run
    ):
        return CommentKind.TEXT, body
    title = clean_title(text=raw_title)
    if not is_meaningful_title(title=title):
        return CommentKind.TEXT, body  # mixed-character art such as `+-----+`
    return CommentKind.BANNER, title


def clean_title(text: str) -> str:
    """Strip leftover decoration, box borders, whitespace and end punctuation from both ends."""
    return text.strip(DECORATION_CHARACTERS + "|" + " \t:.!?")


def is_meaningful_title(title: str) -> bool:
    return any(character.isalpha() for character in title) and (
        sum(character.isalnum() for character in title) >= 2
    )


def _looks_like_ascii_diagram(raw_title: str, has_leading_run: bool, has_trailing_run: bool) -> bool:
    """Interval and bar diagrams (`# x: ---  -----`, `# y:    -------`): a `label:` followed only by
    bars, or several bar groups on one side. Banners put the decoration around the title instead."""
    decoration_group = f"[{re.escape(DECORATION_CHARACTERS)}]{{2,}}"
    if (
        has_trailing_run
        and not has_leading_run
        and raw_title.rstrip(DECORATION_CHARACTERS + " ").endswith(":")
    ):
        return True
    if has_trailing_run and re.search(rf"\s{decoration_group}$", raw_title):
        return True
    return has_leading_run and re.match(rf"^{decoration_group}\s", raw_title) is not None


PLAIN_FIRST_WORD_PATTERN = re.compile(r"^[a-z]+\b(?![(.\[])")


def heading_comment(title: str) -> str:
    """`# Title:`. Only a plain lowercase first word is capitalized; identifiers such as
    `update_wrapper()` or `camelCase` keep their spelling."""
    if PLAIN_FIRST_WORD_PATTERN.match(title):
        title = title[:1].upper() + title[1:]
    return f"# {title}:"


def collect_full_line_comments(source: str) -> list[FullLineComment]:
    comments: list[FullLineComment] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            line_number, column = token.start
            if token.line[:column].strip():
                continue  # a comment after code
            kind, title = classify_comment(comment_text=token.string)
            comments.append(
                FullLineComment(
                    line_number=line_number, column=column, text=token.string, kind=kind, title=title
                )
            )
    except (tokenize.TokenError, SyntaxError):
        return []
    return comments


@dataclass(frozen=True, slots=True)
class BannerRewritePlan:
    """Replace comment lines `first_line..last_line` with one `# Title:` comment (same indentation),
    and delete the first `blank_lines_to_remove` blank lines after them."""

    first_line: int
    last_line: int
    column: int
    replacement: str
    blank_lines_to_remove: int = 0

    def text_edits(self, source_text: SourceText, token_table: SignificantTokenTable) -> list[TextEdit]:
        start = source_text.offset_from_character_position(
            line_number=self.first_line, character_column=self.column
        )
        end_line = self.last_line + self.blank_lines_to_remove
        end_line_text = source_text.lines[end_line - 1].rstrip("\r")
        end = source_text.offset_from_character_position(
            line_number=end_line, character_column=len(end_line_text)
        )
        return [TextEdit(start=start, end=end, replacement=self.replacement)]


def removable_blank_lines_after(
    source_text: SourceText, last_line: int, column: int, max_blank_lines: int
) -> int:
    """Blank lines after a heading beyond `max_blank_lines`, so the heading sits on the code it
    introduces. Zero when the next non-blank line is another comment, is indented differently
    (the heading closes a block) or does not exist."""
    lines = source_text.lines
    heading_indentation = lines[last_line - 1][:column]
    next_index = last_line  # 0-based index of the line after the heading
    while next_index < len(lines) and not lines[next_index].strip():
        next_index += 1
    if next_index >= len(lines):
        return 0
    next_line = lines[next_index]
    code = next_line.lstrip()
    if code.startswith("#") or next_line[: len(next_line) - len(code)] != heading_indentation:
        return 0
    return max(0, next_index - last_line - max_blank_lines)


@dataclass(frozen=True, slots=True)
class DividerRemovalPlan:
    """Delete one divider line. When blank lines surround it, the remaining gap keeps the size of the
    larger side (two blanks, divider, two blanks -> two blanks), not the sum of both."""

    line_number: int

    def text_edits(self, source_text: SourceText, token_table: SignificantTokenTable) -> list[TextEdit]:
        lines = source_text.lines
        line_count = len(lines)
        start = source_text.line_start_offsets[self.line_number - 1]

        def is_blank(line_number: int) -> bool:
            return 1 <= line_number < line_count and not lines[line_number - 1].strip()

        blanks_before = 0
        while is_blank(line_number=self.line_number - 1 - blanks_before):
            blanks_before += 1
        blanks_after = 0
        while is_blank(line_number=self.line_number + 1 + blanks_after):
            blanks_after += 1
        last_deleted_line = self.line_number + min(blanks_before, blanks_after)
        if last_deleted_line < line_count:
            end = source_text.line_start_offsets[last_deleted_line]
        else:
            end = len(source_text.text)
        return [TextEdit(start=start, end=end, replacement="")]


def _byte_column(source_text: SourceText, line_number: int, character_column: int) -> int:
    return len(source_text.lines[line_number - 1][:character_column].encode("utf-8"))


def _location(source_text: SourceText, first: FullLineComment, last: FullLineComment) -> SourceLocation:
    return SourceLocation(
        lineno=first.line_number,
        col_offset=_byte_column(
            source_text=source_text, line_number=first.line_number, character_column=first.column
        ),
        end_lineno=last.line_number,
        end_col_offset=_byte_column(
            source_text=source_text,
            line_number=last.line_number,
            character_column=last.column + len(last.text),
        ),
    )


def group_comment_blocks(comments: list[FullLineComment]) -> list[list[FullLineComment]]:
    """Maximal runs of full-line comments on consecutive lines at the same column."""
    blocks: list[list[FullLineComment]] = []
    for comment in comments:
        previous = blocks[-1][-1] if blocks else None
        if previous and comment.line_number == previous.line_number + 1 and comment.column == previous.column:
            blocks[-1].append(comment)
        else:
            blocks.append([comment])
    return blocks


def block_heading_title(block: list[FullLineComment]) -> str | None:
    """The title when a block is decoration around exactly one title line: a box, a divider above
    a title, or a title underlined by a divider. Bare `#` lines count as decoration."""
    if not any(comment.kind is CommentKind.DIVIDER for comment in block):
        return None
    content = [comment for comment in block if comment.kind is not CommentKind.DIVIDER and comment.title]
    if len(content) != 1 or DIRECTIVE_PATTERN.search(content[0].title):
        return None
    title = clean_title(text=content[0].title)
    return title if is_meaningful_title(title=title) else None


def _banner_finding(
    source_text: SourceText,
    first: FullLineComment,
    last: FullLineComment,
    title: str,
    kind_label: str,
    max_blank_lines_after: int,
) -> RuleFinding:
    heading = heading_comment(title=title)
    blank_lines_to_remove = removable_blank_lines_after(
        source_text=source_text,
        last_line=last.line_number,
        column=first.column,
        max_blank_lines=max_blank_lines_after,
    )
    return RuleFinding(
        code=BANNER_COMMENT_CODE,
        node=_location(source_text=source_text, first=first, last=last),
        message=f"{kind_label}; write `{heading}` instead",
        fix_plans=(
            BannerRewritePlan(
                first_line=first.line_number,
                last_line=last.line_number,
                column=first.column,
                replacement=heading,
                blank_lines_to_remove=blank_lines_to_remove,
            ),
        ),
    )


def check_module_tree(tree: ast.Module, context: RuleContext) -> list[RuleFinding]:
    source_text = context.source_text
    max_blank_lines_after = context.configuration.max_blank_lines_after_heading
    findings: list[RuleFinding] = []
    for block in group_comment_blocks(comments=collect_full_line_comments(source=source_text.text)):
        block_title = block_heading_title(block=block)
        if block_title is not None:
            findings.append(
                _banner_finding(
                    source_text=source_text,
                    first=block[0],
                    last=block[-1],
                    title=block_title,
                    kind_label="Section title decorated with divider lines",
                    max_blank_lines_after=max_blank_lines_after,
                )
            )
            continue
        for comment in block:
            if comment.kind is CommentKind.BANNER:
                findings.append(
                    _banner_finding(
                        source_text=source_text,
                        first=comment,
                        last=comment,
                        title=comment.title,
                        kind_label="Decorative section banner",
                        max_blank_lines_after=max_blank_lines_after,
                    )
                )
            elif comment.kind is CommentKind.DIVIDER:
                findings.append(
                    RuleFinding(
                        code=DIVIDER_COMMENT_CODE,
                        node=_location(source_text=source_text, first=comment, last=comment),
                        message="Decorative divider comment; delete it",
                        fix_plans=(DividerRemovalPlan(line_number=comment.line_number),),
                    )
                )
    return findings
