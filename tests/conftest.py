from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from kaszanas_lint.configuration import load_configuration
from kaszanas_lint.diagnostics import Diagnostic
from kaszanas_lint.linting_session import LintRunResult, run_lint

ProjectFactory = Callable[..., Path]


@pytest.fixture
def make_project(tmp_path: Path) -> ProjectFactory:
    """Write `{relative_path: source}` into a fresh project (dedented) and return its root."""

    def factory(files: dict[str, str], pyproject: str = "") -> Path:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\n" + textwrap.dedent(pyproject), encoding="utf-8"
        )
        for relative_path, source in files.items():
            file_path = tmp_path / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(textwrap.dedent(source).lstrip("\n").encode("utf-8"))
        return tmp_path

    return factory


def lint_project(
    project_root: Path, paths: list[str] | None = None, apply_fixes: bool = False, **overrides
) -> LintRunResult:
    configuration = load_configuration(project_root=project_root).with_overrides(
        cache_enabled=False, **overrides
    )
    requested = [project_root / item for item in (paths or ["."])]
    return run_lint(configuration=configuration, requested_paths=requested, apply_fixes=apply_fixes, jobs=1)


def codes_by_line(diagnostics: list[Diagnostic], relative_path: str) -> list[tuple[int, str]]:
    return [
        (diagnostic.line, diagnostic.code)
        for diagnostic in diagnostics
        if diagnostic.relative_path == relative_path
    ]
