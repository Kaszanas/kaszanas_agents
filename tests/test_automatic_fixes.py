from __future__ import annotations

import textwrap

import pytest
from conftest import lint_project

DEFINITIONS = """
def area(width, height):
    return width * height

def wrap(items):
    return list(items)

class Box:
    def __init__(self, width, height):
        self.width = width

    def scale(self, factor, offset):
        return self
"""


@pytest.mark.parametrize(
    ("call_source", "expected_source"),
    [
        ("area(1, 2)", "area(width=1, height=2)"),
        ("area(1, height=2)", "area(width=1, height=2)"),
        ("area((1), (2))", "area(width=(1), height=(2))"),
        ("area(area(1, 2), 3)", "area(width=area(width=1, height=2), height=3)"),
        ("wrap(x for x in range(3))", "wrap(items=(x for x in range(3)))"),
        ("wrap((x for x in range(3)))", "wrap(items=(x for x in range(3)))"),
        ('area("zażółć", "🙂")', 'area(width="zażółć", height="🙂")'),
        (
            "area(\n    1,  # first (with parens)\n    2,\n)",
            "area(\n    width=1,  # first (with parens)\n    height=2,\n)",
        ),
        ("Box(1, 2).scale(3, 4)", "Box(width=1, height=2).scale(factor=3, offset=4)"),
        ("area(*[1, 2])", "area(*[1, 2])"),
        ("area(x := 1, 2)", "area(width=(x := 1), height=2)"),
        ("area((x := 1), 2)", "area(width=(x := 1), height=2)"),
    ],
)
def test_fix_rewrites_call_sites(make_project, call_source, expected_source):
    source = DEFINITIONS + "\n" + call_source + "\n"
    project_root = make_project(files={"main.py": source})
    result = lint_project(project_root=project_root, apply_fixes=True)
    (file_result,) = result.file_results
    fixed = file_result.fixed_source if file_result.fixed_source is not None else source
    assert fixed.endswith("\n" + expected_source + "\n")
    assert not [item for item in file_result.diagnostics if item.code == "KWA001"]


def test_fix_preserves_crlf_line_endings_and_bom(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    source = "def area(width, height):\r\n    return width\r\n\r\narea(1, 2)\r\n"
    (tmp_path / "main.py").write_bytes(b"\xef\xbb\xbf" + source.encode("utf-8"))
    result = lint_project(project_root=tmp_path, apply_fixes=True)
    (file_result,) = result.file_results
    assert file_result.encoding == "utf-8-sig"
    assert file_result.fixed_source == source.replace("area(1, 2)", "area(width=1, height=2)")


def test_fix_reports_remaining_unfixable_diagnostics(make_project):
    project_root = make_project(
        files={
            "main.py": textwrap.dedent(
                """
                import third_party

                def area(width, height):
                    return width * height

                area(1, 2)
                third_party.call(1, 2, 3)
                """
            )
        }
    )
    result = lint_project(project_root=project_root, apply_fixes=True)
    (file_result,) = result.file_results
    assert file_result.fixed_violation_count == 1
    assert [item.code for item in file_result.diagnostics] == ["KWA002"]
