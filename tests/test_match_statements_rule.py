from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import lint_project

MATCH_CODES = frozenset({"MAT001", "MAT002"})
PYPROJECT_PY310 = 'requires-python = ">=3.10"\n'


def check(make_project, source: str, pyproject: str = PYPROJECT_PY310, **overrides):
    project_root = make_project(files={"main.py": source}, pyproject=pyproject)
    overrides.setdefault("selected_rule_codes", MATCH_CODES)
    result = lint_project(project_root=project_root, **overrides)
    assert not result.errors, result.errors
    return [(item.line, item.code, item.is_fixable) for item in result.diagnostics], result


def fix(make_project, source: str, pyproject: str = PYPROJECT_PY310) -> str:
    project_root = make_project(files={"main.py": source}, pyproject=pyproject)
    result = lint_project(project_root=project_root, apply_fixes=True, selected_rule_codes=MATCH_CODES)
    (file_result,) = result.file_results
    assert file_result.fixed_source is not None, "expected a fix"
    return file_result.fixed_source


class Shape:
    pass


EQUIVALENCE_INPUTS = [0, 1, 2, 3, -1, 1.0, 2.5, True, False, None, "a", "b", "", b"a", 3j, (), [], object()]


def assert_equivalent(original: str, fixed: str, function_name: str = "classify") -> None:
    """Run both versions over awkward inputs (bool/float equality, identity, bytes...) and compare."""
    original_namespace: dict[str, object] = {}
    fixed_namespace: dict[str, object] = {}
    exec(compile(source=textwrap.dedent(original), filename="<original>", mode="exec"), original_namespace)
    exec(compile(source=fixed, filename="<fixed>", mode="exec"), fixed_namespace)
    # Each namespace builds its own EXTRA_INPUTS (instances of its own classes); compare by position.
    original_inputs = [*EQUIVALENCE_INPUTS, *original_namespace.get("EXTRA_INPUTS", [])]
    fixed_inputs = [*EQUIVALENCE_INPUTS, *fixed_namespace.get("EXTRA_INPUTS", [])]
    for original_value, fixed_value in zip(original_inputs, fixed_inputs, strict=True):
        outcomes = []
        for namespace, value in ((original_namespace, original_value), (fixed_namespace, fixed_value)):
            try:
                outcomes.append(("ok", namespace[function_name](value)))
            except Exception as error:
                outcomes.append(("error", type(error)))
        assert outcomes[0] == outcomes[1], f"behaviour differs for {original_value!r}: {outcomes}"


# ----------------------------------------------------------------------------- detection


def test_literal_chain_with_else_is_fixable(make_project):
    found, result = check(
        make_project=make_project,
        source="""
            def classify(value):
                if value == 1:
                    return "one"
                elif value == 2:
                    return "two"
                else:
                    return "many"
        """,
    )
    # Returns of constants with a literal subject: a dict lookup beats both if/elif and match.
    assert found == [(2, "MAT002", False)]
    assert "dict lookup" in result.diagnostics[0].message


@pytest.mark.parametrize(
    "condition_chain",
    [
        "value == Color.RED / value == Color.GREEN / value == Color.BLUE",
        "value in (1, 2) / value in [3, 4] / value == -5",
        "value == 1 or value == 2 / value is None / value is True",
        "isinstance(value, int) / isinstance(value, (str, bytes)) / value is None",
        "value == 1 and flag / value == 2 / value == 3",
    ],
)
def test_supported_tests_are_reported_and_fixable(make_project, condition_chain):
    tests = condition_chain.split(" / ")
    lines = [
        "import enum",
        "class Color(enum.Enum):",
        "    RED = 1",
        "    GREEN = 2",
        "    BLUE = 3",
        "flag = True",
        "def classify(value):",
        "    result = 0",
    ]
    for index, test in enumerate(tests):
        keyword = "if" if index == 0 else "elif"
        lines += [f"    {keyword} {test}:", f"        result = compute({index})"]
    lines += ["    return result", "def compute(number):", "    return number"]
    found, _ = check(make_project=make_project, source="\n".join(lines) + "\n")
    assert [(code, fixable) for _, code, fixable in found] == [("MAT001", True)]


@pytest.mark.parametrize(
    ("description", "tests"),
    [
        (
            "bare-name constant would become a capture pattern",
            ["value == RED", "value == GREEN", "value == BLUE"],
        ),
        ("== None is not identity", ["value == None", "value == 1", "value == 2"]),
        ("mixed subjects", ["value == 1", "other == 2", "value == 3"]),
        ("call subject is evaluated once by match", ["read() == 1", "read() == 2", "read() == 3"]),
        ("inequality has no pattern", ["value != 1", "value == 2", "value == 3"]),
        ("ordering has no pattern", ["value < 1", "value == 2", "value == 3"]),
        ("set membership raises for unhashable subjects", ["value in {1, 2}", "value == 3", "value == 4"]),
    ],
)
def test_unsupported_tests_are_not_reported(make_project, description, tests):
    lines = [
        "RED, GREEN, BLUE = 1, 2, 3",
        "other = 0",
        "def read():",
        "    return 1",
        "def classify(value):",
        "    result = []",
    ]
    for index, test in enumerate(tests):
        keyword = "if" if index == 0 else "elif"
        lines += [f"    {keyword} {test}:", f"        result.append({index})"]
    lines += ["    return result"]
    found, _ = check(make_project=make_project, source="\n".join(lines) + "\n")
    assert found == [], description


def test_else_if_and_short_chains_are_not_chains(make_project):
    found, _ = check(
        make_project=make_project,
        source="""
            def classify(value):
                items = []
                if value == 1:
                    items.append(1)
                else:
                    if value == 2:
                        items.append(2)
                    elif value == 3:
                        items.append(3)
                return items
        """,
    )
    assert found == []


def test_isinstance_with_unverifiable_class_is_report_only(make_project):
    found, _ = check(
        make_project=make_project,
        source="""
            import numpy
            TYPES = (int, str)

            def classify(value):
                kinds = []
                if isinstance(value, numpy.ndarray):
                    kinds.append("array")
                elif isinstance(value, TYPES):
                    kinds.append("scalar")
                elif value is None:
                    kinds.append("none")
                return kinds
        """,
    )
    assert found == [(6, "MAT001", False)]


def test_target_version_below_310_disables_match_but_not_dict_hint(make_project):
    source = """
        def classify(value):
            items = []
            if value == 1:
                items.append(1)
            elif value == 2:
                items.append(2)
            elif value == 3:
                items.append(3)
            return items

        def lookup(value):
            if value == 1:
                return "a"
            elif value == 2:
                return "b"
            else:
                return "c"
    """
    found, _ = check(make_project=make_project, source=source, pyproject='requires-python = ">=3.9"\n')
    assert found == [(12, "MAT002", False)]
    found, _ = check(
        make_project=make_project, source=source, pyproject="[tool.kaszanas-lint]\ntarget-version = 'py38'\n"
    )
    assert found == [(12, "MAT002", False)]


def test_min_match_branches_and_noqa(make_project):
    source = """
        def classify(value):
            items = []
            if value == 1:  # noqa: MAT001
                items.append(1)
            elif value == 2:
                items.append(2)
            elif value == 3:
                items.append(3)
            if value == 4:
                items.append(4)
            elif value == 5:
                items.append(5)
            elif value == 6:
                items.append(6)
            return items
    """
    found, _ = check(make_project=make_project, source=source)
    assert found == [(9, "MAT001", True)]
    found, _ = check(make_project=make_project, source=source, minimum_match_branches=4)
    assert found == []


def test_dictionary_hint_for_assignment_chains(make_project):
    found, _ = check(
        make_project=make_project,
        source="""
            def classify(value):
                if value == "a":
                    label = 1
                elif value == "b":
                    label = 2
                elif value == "c":
                    label = 3
                else:
                    label = 0
                return label
        """,
    )
    assert found == [(2, "MAT002", False)]


def test_one_computed_body_means_no_dictionary_hint(make_project):
    # Regression: constant assignments in some branches must not make the whole chain MAT002.
    found, _ = check(
        make_project=make_project,
        source="""
            def classify(value):
                if value == 1:
                    label = compute(value)
                elif value == 2:
                    label = 2
                elif value == 3:
                    label = 3
                else:
                    label = 0
                return label

            def compute(number):
                return number
        """,
    )
    assert found == [(2, "MAT001", True)]


# ----------------------------------------------------------------------------- fixes


FIX_CASES = {
    "literals_alternatives_singletons_comments": (
        """
        def classify(value):
            if value == 1:  # the first
                result = "one"
            elif value in (2, 3):
                # small numbers
                result = "small"
            elif value is None:
                result = "nothing"
            else:  # anything else
                result = str(value)
            return result
        """,
        """
        def classify(value):
            match value:
                case 1:  # the first
                    result = "one"
                case 2 | 3:
                    # small numbers
                    result = "small"
                case None:
                    result = "nothing"
                case _:  # anything else
                    result = str(value)
            return result
        """,
    ),
    "class_patterns_and_guards": (
        """
        class Shape:
            pass

        EXTRA_INPUTS = [Shape()]

        def classify(value):
            if isinstance(value, bool):
                return f"bool {value}"
            elif isinstance(value, (int, float)) and value > 1:
                return "big number"
            elif isinstance(value, Shape):
                return "shape"
            return "other"
        """,
        """
        class Shape:
            pass

        EXTRA_INPUTS = [Shape()]

        def classify(value):
            match value:
                case bool():
                    return f"bool {value}"
                case int() | float() if value > 1:
                    return "big number"
                case Shape():
                    return "shape"
            return "other"
        """,
    ),
    "nested_chains_need_two_passes": (
        """
        def classify(value):
            if value == 1:
                if value == 1.0:
                    inner = "float-equal"
                elif value is True:
                    inner = "true"
                else:
                    inner = "int"
                result = [inner]
            elif value == "a":
                result = ["letter"]
            elif value == b"a":
                result = ["bytes"]
            else:
                result = []
            return result
        """,
        """
        def classify(value):
            match value:
                case 1:
                    match value:
                        case 1.0:
                            inner = "float-equal"
                        case True:
                            inner = "true"
                        case _:
                            inner = "int"
                    result = [inner]
                case "a":
                    result = ["letter"]
                case b"a":
                    result = ["bytes"]
                case _:
                    result = []
            return result
        """,
    ),
}


@pytest.mark.parametrize("case_name", list(FIX_CASES))
def test_fix_produces_expected_and_equivalent_code(make_project, case_name):
    original, expected = FIX_CASES[case_name]
    fixed = fix(make_project=make_project, source=original)
    assert fixed == textwrap.dedent(expected).lstrip("\n")
    assert_equivalent(original=original, fixed=fixed)


def test_fix_combines_with_keyword_argument_fix_in_the_same_region(make_project):
    source = """
        def scale(amount, factor):
            return amount * factor

        def classify(value):
            if value == 1:
                result = scale(1, 2)
            elif value == 2:
                result = scale(2, 3)
            elif value == 3:
                result = 0
            else:
                result = -1
            return result
    """
    project_root = make_project(files={"main.py": source}, pyproject=PYPROJECT_PY310)
    result = lint_project(project_root=project_root, apply_fixes=True)
    (file_result,) = result.file_results
    assert "match value:" in file_result.fixed_source
    assert "scale(amount=1, factor=2)" in file_result.fixed_source
    assert file_result.fixed_violation_count == 3
    assert_equivalent(original=source, fixed=file_result.fixed_source)


def test_fix_preserves_crlf(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "x"\nrequires-python = ">=3.10"\n')
    source = (
        "def classify(value):\r\n    out = []\r\n    if value == 1:\r\n        out.append(1)\r\n"
        "    elif value == 2:\r\n        out.append(2)\r\n"
        "    else:\r\n        out.append(3)\r\n    return out\r\n"
    )
    (tmp_path / "main.py").write_bytes(source.encode("utf-8"))
    result = lint_project(project_root=tmp_path, apply_fixes=True, selected_rule_codes=MATCH_CODES)
    (file_result,) = result.file_results
    assert "\r\n    match value:\r\n        case 1:\r\n" in file_result.fixed_source
    assert "\n" not in file_result.fixed_source.replace("\r\n", "")


@pytest.mark.parametrize(
    "awkward_body",
    [
        'out.append("""multi\nline""")',  # re-indenting would change the string
        "out.append(1); out.append(2)",  # fine on its own; combined with the inline case below
    ],
)
def test_layouts_that_cannot_be_reproduced_are_report_only(make_project, awkward_body):
    if "multi" not in awkward_body:
        source = (
            "def classify(value):\n    out = []\n    if value == 1: out.append(1)\n"
            "    elif value == 2: out.append(2)\n    elif value == 3: out.append(3)\n    return out\n"
        )
    else:
        source = (
            "def classify(value):\n    out = []\n    if value == 1:\n        " + awkward_body + "\n"
            "    elif value == 2:\n        out.append(2)\n"
            "    elif value == 3:\n        out.append(3)\n    return out\n"
        )
    project_root = make_project(files={"main.py": source}, pyproject=PYPROJECT_PY310)
    result = lint_project(project_root=project_root, selected_rule_codes=MATCH_CODES)
    assert [(item.code, item.is_fixable) for item in result.diagnostics] == [("MAT001", False)]
