from __future__ import annotations

from conftest import codes_by_line, lint_project


def check(make_project, files: dict[str, str], target: str = "main.py", **overrides):
    project_root = make_project(files=files)
    result = lint_project(project_root=project_root, **overrides)
    assert not result.errors, result.errors
    return codes_by_line(diagnostics=result.diagnostics, relative_path=target), result


def test_module_level_function_called_positionally(make_project):
    found, result = check(
        make_project=make_project,
        files={
            "main.py": """
                def area(width, height):
                    return width * height

                area(1, 2)
                area(width=1, height=2)
                area(1, height=2)
            """
        },
    )
    assert found == [(4, "KWA001"), (6, "KWA001")]
    assert "width=, height=" in result.diagnostics[0].message
    assert result.diagnostics[0].is_fixable


def test_positional_only_and_variadic_parameters_are_exempt(make_project):
    found, _ = check(
        make_project=make_project,
        files={
            "main.py": """
                def only_positional(a, b, /):
                    pass

                def mixed(a, /, b):
                    pass

                def variadic(first, *rest):
                    pass

                only_positional(1, 2)
                mixed(1, 2)
                variadic(1, 2, 3)
                variadic(1)
            """
        },
    )
    # mixed: only `b` is convertible; variadic(1,2,3) spills into *rest so nothing is possible.
    assert found == [(11, "KWA001"), (13, "KWA001")]


def test_cross_module_imports_with_aliases_and_relative_imports(make_project):
    found, _ = check(
        make_project=make_project,
        files={
            "src/pkg/__init__.py": "from .geometry import area as exported_area\n",
            "src/pkg/geometry.py": """
                def area(width, height):
                    return width * height
            """,
            "src/pkg/consumer.py": """
                from . import geometry
                from .geometry import area as compute_area
                import pkg.geometry as geo
                from pkg import exported_area

                compute_area(1, 2)
                geometry.area(1, 2)
                geo.area(1, 2)
                exported_area(1, 2)
            """,
        },
        target="src/pkg/consumer.py",
    )
    assert found == [(6, "KWA001"), (7, "KWA001"), (8, "KWA001"), (9, "KWA001")]


def test_methods_via_self_cls_class_and_super(make_project):
    found, result = check(
        make_project=make_project,
        files={
            "main.py": """
                class Base:
                    def greet(self, name, punctuation):
                        return name + punctuation

                    @classmethod
                    def build(cls, size):
                        return cls()

                    @staticmethod
                    def helper(value, other):
                        return value

                class Child(Base):
                    def greet(self, name, punctuation):
                        super().greet(name, punctuation)
                        self.helper(1, 2)
                        Base.helper(1, 2)
                        Base.greet(self, "a", "b")
                        return self.build(3)

                    @classmethod
                    def make(cls):
                        return cls.build(4)
            """
        },
    )
    assert found == [
        (15, "KWA001"),
        (16, "KWA001"),
        (17, "KWA001"),
        (18, "KWA001"),
        (19, "KWA001"),
        (23, "KWA001"),
    ]
    messages = {diagnostic.line: diagnostic.message for diagnostic in result.diagnostics}
    assert "name=, punctuation=" in messages[15]
    assert "value=, other=" in messages[16]
    # Unbound call through the class: `self` stays positional, the rest become keywords.
    assert "name=, punctuation=" in messages[18]
    assert "size=" in messages[19]


def test_constructors_init_dataclass_and_namedtuple(make_project):
    found, result = check(
        make_project=make_project,
        files={
            "main.py": """
                from dataclasses import KW_ONLY, dataclass, field
                from typing import NamedTuple

                class Plain:
                    def __init__(self, a, b):
                        pass

                @dataclass
                class Point:
                    x: int
                    y: int
                    _: KW_ONLY
                    label: str = ""

                @dataclass
                class Point3(Point):
                    z: int = 0
                    cache: dict = field(init=False)

                class Pair(NamedTuple):
                    left: int
                    right: int

                Plain(1, 2)
                Point(1, 2)
                Point3(1, 2, 3)
                Pair(1, 2)
            """
        },
    )
    assert found == [(24, "KWA001"), (25, "KWA001"), (26, "KWA001"), (27, "KWA001")]
    assert "x=, y=, z=" in result.diagnostics[2].message


def test_shadowing_and_unknown_decorators_fall_back_to_external_rule(make_project):
    found, _ = check(
        make_project=make_project,
        files={
            "main.py": """
                import functools

                def area(width, height):
                    return width * height

                def decorator(function):
                    return function

                @decorator
                def wrapped(a, b):
                    pass

                @functools.lru_cache(maxsize=None)
                def cached(a, b):
                    pass

                def shadowing(area):
                    area(1, 2)

                def local_assignment():
                    area = print
                    area(1, 2)

                wrapped(1, 2)
                cached(1, 2)
                [area(1, 2) for area in [print]]
            """
        },
    )
    assert found == [(25, "KWA001")]


def test_rebound_module_names_are_not_trusted(make_project):
    found, _ = check(
        make_project=make_project,
        files={
            "main.py": """
                def area(width, height):
                    return width * height

                area = lambda *values: 0

                area(1, 2)
            """
        },
    )
    assert found == []


def test_external_rule_counts_positional_arguments(make_project):
    found, result = check(
        make_project=make_project,
        files={
            "main.py": """
                import logging
                import os.path
                import re
                import third_party

                third_party.connect("host", 5432, "db")
                third_party.connect("host", 5432)
                re.sub("a", "b", "abc")
                os.path.join("a", "b", "c")
                isinstance(1, int)
                logging.getLogger(__name__).info("%s %s %s", 1, 2, 3)
                getattr(object, "x", None)
            """
        },
    )
    assert found == [(6, "KWA002"), (8, "KWA002")]
    assert "pattern=, repl=, string=" in result.diagnostics[1].message


def test_rule_selection_and_allow_positional(make_project):
    files = {
        "main.py": """
            import third_party

            def area(width, height, depth):
                return width * height * depth

            area(1, 2, 3)
            third_party.call(1, 2, 3)
        """
    }
    found, result = check(make_project=make_project, files=files, selected_rule_codes=frozenset({"KWA001"}))
    assert found == [(6, "KWA001")]
    found, result = check(make_project=make_project, files=files, allowed_leading_positional_arguments=1)
    assert found == [(6, "KWA001"), (7, "KWA002")]
    assert "height=, depth=" in result.diagnostics[0].message


def test_noqa_and_per_file_ignores(make_project):
    project_root = make_project(
        files={
            "main.py": """
                def area(width, height):
                    return width * height

                area(1, 2)  # noqa: KWA001
                area(1, 2)  # noqa
                area(1, 2)  # noqa: E501
            """,
            "tests/test_area.py": """
                from main import area

                area(1, 2)
            """,
        },
        pyproject="""
            [tool.kaszanas-lint]
            per-file-ignores = { "tests/**" = ["KWA001"] }
        """,
    )
    result = lint_project(project_root=project_root)
    assert [(item.relative_path, item.line) for item in result.diagnostics] == [("main.py", 6)]


def test_checking_one_file_still_resolves_definitions_elsewhere(make_project):
    project_root = make_project(
        files={
            "library.py": "def area(width, height):\n    return width * height\n",
            "app.py": "from library import area\n\narea(1, 2)\n",
        }
    )
    result = lint_project(project_root=project_root, paths=["app.py"])
    assert [(item.relative_path, item.code) for item in result.diagnostics] == [("app.py", "KWA001")]


def test_syntax_errors_are_reported_not_fatal(make_project):
    project_root = make_project(
        files={"broken.py": "def oops(:\n", "main.py": "def f(a):\n    pass\n\nf(1)\n"}
    )
    result = lint_project(project_root=project_root)
    assert [(item.relative_path, item.code) for item in result.diagnostics] == [("main.py", "KWA001")]
    assert any("broken.py" in error for error in result.errors)
