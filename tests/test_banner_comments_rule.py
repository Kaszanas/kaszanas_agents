from __future__ import annotations

from pathlib import Path

import pytest
from conftest import lint_project

from kaszanas_lint.rules.banner_comments import CommentKind, classify_comment

COMMENT_CODES = frozenset({"CMT001", "CMT002"})


def check(make_project, source: str, pyproject: str = ""):
    project_root = make_project(files={"main.py": source}, pyproject=pyproject)
    result = lint_project(project_root=project_root, selected_rule_codes=COMMENT_CODES)
    assert not result.errors, result.errors
    return [(item.line, item.code, item.is_fixable) for item in result.diagnostics]


def fix(make_project, source: str, selected_rule_codes=COMMENT_CODES) -> str:
    project_root = make_project(files={"main.py": source})
    result = lint_project(
        project_root=project_root, apply_fixes=True, selected_rule_codes=selected_rule_codes
    )
    (file_result,) = result.file_results
    assert file_result.fixed_source is not None, "expected a fix"
    return file_result.fixed_source


# Classification:


@pytest.mark.parametrize(
    ("comment", "title"),
    [
        ("# ----------------------------------------------------------------- detection", "detection"),
        ("# -------------------------- some --------------------------", "some"),
        ("# ------------------------- Some helpers -------------", "Some helpers"),
        ("#### Helpers ####", "Helpers"),
        ("# ==== Public API", "Public API"),
        ("# Private helpers ****", "Private helpers"),
        ("# ~~~~ tests -> patterns ~~~~", "tests -> patterns"),
        ("# ---- Constants: ----", "Constants"),
        ("# ____ internal ____", "internal"),
    ],
)
def test_titled_banners(comment, title):
    assert classify_comment(comment_text=comment) == (CommentKind.BANNER, title)


@pytest.mark.parametrize(
    "comment",
    ["# ----", "# " + "-" * 60, "# ==========", "##########", "# = = = = =", "#*******"],
)
def test_bare_dividers(comment):
    assert classify_comment(comment_text=comment)[0] is CommentKind.DIVIDER


@pytest.mark.parametrize(
    "comment",
    [
        "# +-----+-----+",  # table/diagram border: more than one decoration character
        "# |  A  |---->| B |",  # diagram line: the run is in the middle
        "# a ----> b",
        "# x: ---  -----",  # interval diagrams (pandas test_libsparse.py)
        "# y:    -------",
        "# r: -------------",
        "# x: -----     -----",
        "# ---",  # runs shorter than four are ordinary punctuation
        "## Heading",
        "### Sub heading ###",
        "# -*- coding: utf-8 -*-",
        "#!/usr/bin/env python",
        "# ---- noqa: E501 ----",  # tool directives keep their exact text
        "# type: ignore  ----",
        "# Plain explanation of the next block.",
        "#",
        "###",
    ],
)
def test_ordinary_comments_are_not_flagged(comment):
    assert classify_comment(comment_text=comment)[0] is CommentKind.TEXT


# Detection:


def test_reports_banners_and_dividers(make_project):
    source = """
        # ------------------------------------------------------------ helpers
        def helper():
            return 1


        # ------------------------------------------------------------


        class Box:
            # ==== geometry ====
            def area(self):
                return 0
    """
    assert check(make_project=make_project, source=source) == [
        (1, "CMT001", True),
        (6, "CMT002", True),
        (10, "CMT001", True),
    ]


def test_boxed_title_is_one_finding(make_project):
    source = """
        # ------------------------------------------------------------
        # Some helpers
        # ------------------------------------------------------------
        def helper():
            return 1
    """
    assert check(make_project=make_project, source=source) == [(1, "CMT001", True)]


def test_fix_strips_box_side_borders(make_project):
    source = """
        ####################
        # CORE             #
        ####################
        DEBUG = False
    """
    assert fix(make_project=make_project, source=source) == "# CORE:\nDEBUG = False\n"


def test_box_around_a_banner_line_is_one_finding(make_project):
    source = """
        # ------------------------------------------------------------
        # ------------------------- Some helpers -------------
        # ----------------------------------------------------------
        x = 1
    """
    assert check(make_project=make_project, source=source) == [(1, "CMT001", True)]


def test_trailing_comments_and_strings_are_ignored(make_project):
    source = '''
        x = 1  # ---------- not a banner ----------
        TEXT = """
        # ---------- inside a string ----------
        """


        def documented():
            """
            # ---------- inside a docstring ----------
            """
    '''
    assert check(make_project=make_project, source=source) == []


def test_noqa_on_the_first_line_suppresses(make_project):
    source = """
        #### Public API ####  # noqa: CMT001
        x = 1
        # ====================  # noqa: CMT002
        y = 2
    """
    assert check(make_project=make_project, source=source) == []


def test_per_file_ignores(make_project):
    source = "# ---- helpers ----\nx = 1\n"
    pyproject = '[tool.kaszanas-lint]\nper-file-ignores = { "main.py" = ["CMT001"] }\n'
    assert check(make_project=make_project, source=source, pyproject=pyproject) == []


# Fixes:


def test_fix_turns_banner_into_heading(make_project):
    source = """
        class Checker:
            # ------------------------------------------------------------------ scopes
            def scope(self):
                return 1

            # ---- tests -> patterns: ----
            def pattern(self):
                return 2
    """
    assert fix(make_project=make_project, source=source) == (
        "class Checker:\n"
        "    # Scopes:\n"
        "    def scope(self):\n"
        "        return 1\n"
        "\n"
        "    # Tests -> patterns:\n"
        "    def pattern(self):\n"
        "        return 2\n"
    )


def test_fix_collapses_boxed_title(make_project):
    source = """
        import os

        # ------------------------------------------------------------
        # some helpers.
        # ------------------------------------------------------------
        def helper():
            return os.sep
    """
    assert (
        fix(make_project=make_project, source=source)
        == "import os\n\n# Some helpers:\ndef helper():\n    return os.sep\n"
    )


def test_fix_deletes_dividers_without_leaving_extra_blank_lines(make_project):
    source = """
        x = 1


        # ============================================================


        y = 2
        # ------------------------------------------------------------
        # ------------------------------------------------------------
        z = 3
    """
    assert fix(make_project=make_project, source=source) == "x = 1\n\n\ny = 2\nz = 3\n"


def test_fix_handles_the_examples_from_the_standard_and_is_idempotent(make_project):
    source = """
        # -------------------------- some --------------------------
        # ------------------------------------------------------------
        # ------------------------- Some stuff -------------
        # ----------------------------------------------------------
        x = 1
    """
    project_root = make_project(files={"main.py": source})
    result = lint_project(project_root=project_root, apply_fixes=True, selected_rule_codes=COMMENT_CODES)
    (file_result,) = result.file_results
    assert file_result.fixed_source == "# Some:\n# Some stuff:\nx = 1\n"
    assert file_result.diagnostics == []
    (project_root / "main.py").write_bytes(file_result.fixed_source.encode("utf-8"))
    second_run = lint_project(project_root=project_root, apply_fixes=True, selected_rule_codes=COMMENT_CODES)
    assert second_run.diagnostics == []
    assert second_run.file_results[0].fixed_source is None


def test_fix_preserves_crlf_and_bom(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "x"\n')
    source = "x = 1\r\n# ---- helpers ----\r\ndef helper():\r\n    return x\r\n# ==========\r\ny = 2\r\n"
    (tmp_path / "main.py").write_bytes(b"\xef\xbb\xbf" + source.encode("utf-8"))
    result = lint_project(project_root=tmp_path, apply_fixes=True, selected_rule_codes=COMMENT_CODES)
    (file_result,) = result.file_results
    assert file_result.fixed_source == "x = 1\r\n# Helpers:\r\ndef helper():\r\n    return x\r\ny = 2\r\n"
    assert file_result.encoding == "utf-8-sig"


def test_fix_combines_with_keyword_argument_fix(make_project):
    source = """
        def area(width, height):
            return width * height


        # ---- usage ----
        total = area(1, 2)
    """
    fixed = fix(make_project=make_project, source=source, selected_rule_codes=COMMENT_CODES | {"KWA001"})
    assert "# Usage:\ntotal = area(width=1, height=2)\n" in fixed


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # A divider above a title (pandas style).
        (
            "class Frame:\n    x = 1\n\n    # ------------------------------------------\n"
            "    # Rendering Methods\n\n    def render(self):\n        return 1\n",
            "class Frame:\n    x = 1\n\n    # Rendering Methods:\n\n"
            "    def render(self):\n        return 1\n",
        ),
        # A title underlined by a divider (CPython http.cookiejar style).
        (
            "# Date/time conversion\n# ------------------\nEPOCH = 1970\n",
            "# Date/time conversion:\nEPOCH = 1970\n",
        ),
        # Bare `#` lines inside the decoration (CPython test_sax style).
        ("# =========\n#\n#   saxutils tests\n#\n# =========\nx = 1\n", "# Saxutils tests:\nx = 1\n"),
        # Identifiers keep their spelling.
        ("# ---- update_wrapper() and wraps() ----\nx = 1\n", "# update_wrapper() and wraps():\nx = 1\n"),
        ("# ---- camelCase helpers ----\nx = 1\n", "# camelCase helpers:\nx = 1\n"),
    ],
)
def test_fix_collapses_decorated_title_blocks(make_project, source, expected):
    assert fix(make_project=make_project, source=source) == expected


def test_dividers_inside_longer_comment_paragraphs_are_deleted_individually(make_project):
    source = (
        "## Notes on Decimal\n## ----------------\n## Decimal has all of the methods.\n"
        "## It is not registered as a Real.\nx = 1\n"
    )
    assert check(make_project=make_project, source=source) == [(2, "CMT002", True)]
    assert fix(make_project=make_project, source=source) == (
        "## Notes on Decimal\n## Decimal has all of the methods.\n## It is not registered as a Real.\nx = 1\n"
    )


def test_shebang_is_never_merged_into_a_heading(make_project):
    source = "#!/usr/bin/env python\n# ----------\n# Helpers\nx = 1\n"
    assert fix(make_project=make_project, source=source) == "#!/usr/bin/env python\n# Helpers\nx = 1\n"


def test_table_header_underlines_are_not_dividers(make_project):
    source = "# Name    Value\n# ----    -----\n# a       1\nx = 1\n"
    assert check(make_project=make_project, source=source) == []
