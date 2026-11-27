"""Load `[tool.kaszanas-lint]` from pyproject.toml and merge command-line overrides."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

# Kept in sync with the rule registry by tests/test_rule_documentation.py.
ALL_RULE_CODES = ("KWA001", "KWA002", "MAT001", "MAT002", "CMT001", "CMT002")
DEFAULT_MINIMUM_MATCH_BRANCHES = 3

DEFAULT_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".env",
        "env",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".kaszanas_lint_cache",
        "site-packages",
        ".eggs",
        "__pypackages__",
    }
)

DEFAULT_SAFE_DECORATORS = frozenset(
    {
        "staticmethod",
        "classmethod",
        "property",
        "abstractmethod",
        "cache",
        "lru_cache",
        "cached_property",
        "wraps",
        "override",
        "final",
        "contextmanager",
        "asynccontextmanager",
        "deprecated",
        "total_ordering",
        "dataclass",
        "unique",
        "runtime_checkable",
        "setter",
        "getter",
        "deleter",
        "singledispatchmethod",
    }
)

DEFAULT_IGNORED_CALL_PATTERNS = (
    # Logging-style calls conventionally take a format string plus positional values.
    r"(.*\.)?(debug|info|warning|warn|error|exception|critical|fatal|log)",
    # printf-style and string helpers.
    r"(.*\.)?(format|join|replace|startswith|endswith|split|rsplit|strip)",
)


@dataclass(frozen=True, slots=True)
class LintConfiguration:
    project_root: Path
    source_roots: tuple[Path, ...]
    exclude_patterns: tuple[str, ...] = ()
    selected_rule_codes: frozenset[str] = frozenset(ALL_RULE_CODES)
    per_file_ignores: tuple[tuple[str, frozenset[str]], ...] = ()
    allowed_leading_positional_arguments: int = 0
    max_positional_arguments_for_external_calls: int = 2
    ignored_call_patterns: tuple[str, ...] = DEFAULT_IGNORED_CALL_PATTERNS
    safe_decorators: frozenset[str] = DEFAULT_SAFE_DECORATORS
    inspect_standard_library: bool = True
    cache_enabled: bool = True
    minimum_match_branches: int = DEFAULT_MINIMUM_MATCH_BRANCHES
    # Lowest Python version the project supports; None when it can't be determined.
    target_python_version: tuple[int, int] | None = None

    def with_overrides(self, **overrides: object) -> LintConfiguration:
        return replace(self, **{key: value for key, value in overrides.items() if value is not None})


def find_project_root(start_directory: Path) -> Path:
    """Nearest ancestor holding pyproject.toml, else nearest holding .git, else start."""
    start_directory = start_directory.resolve()
    candidates = [start_directory, *start_directory.parents]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return start_directory


def _read_pyproject(pyproject_path: Path) -> dict[str, object]:
    if not pyproject_path.is_file():
        return {}
    with pyproject_path.open(mode="rb") as pyproject_file:
        return tomllib.load(pyproject_file)


def _tool_table(document: dict[str, object], pyproject_path: Path) -> dict[str, object]:
    table = document.get("tool", {}).get("kaszanas-lint", {})
    if not isinstance(table, dict):
        raise ValueError(f"[tool.kaszanas-lint] in {pyproject_path} must be a table")
    return table


def parse_python_version(raw_version: str) -> tuple[int, int] | None:
    """Accept ruff-style `py310` and dotted `3.10`."""
    compact = re.fullmatch(pattern=r"py(\d)(\d+)", string=raw_version.strip())
    if compact:
        return int(compact.group(1)), int(compact.group(2))
    dotted = re.fullmatch(pattern=r"(\d+)\.(\d+)(?:\.\d+)?", string=raw_version.strip())
    if dotted:
        return int(dotted.group(1)), int(dotted.group(2))
    return None


def minimum_version_from_requires_python(specifier: str) -> tuple[int, int] | None:
    """Lower bound of a PEP 440 specifier such as `>=3.10`, `~=3.11` or `>=3.9,<4`."""
    for clause in specifier.split(","):
        match = re.fullmatch(pattern=r"\s*(>=|~=|==|>)\s*(\d+)\.(\d+).*", string=clause)
        if match:
            operator, major, minor = match.group(1), int(match.group(2)), int(match.group(3))
            return (major, minor + 1) if operator == ">" else (major, minor)
    return None


def detect_target_python_version(
    document: dict[str, object], table: dict[str, object]
) -> tuple[int, int] | None:
    """`[tool.kaszanas-lint] target-version`, else `requires-python`, else ruff's target-version."""
    explicit = table.get("target-version")
    if isinstance(explicit, str):
        parsed = parse_python_version(raw_version=explicit)
        if parsed is None:
            raise ValueError(f"[tool.kaszanas-lint] `target-version` is not a version: {explicit!r}")
        return parsed
    project_table = document.get("project", {})
    requires_python = project_table.get("requires-python") if isinstance(project_table, dict) else None
    if isinstance(requires_python, str):
        detected = minimum_version_from_requires_python(specifier=requires_python)
        if detected is not None:
            return detected
    ruff_table = document.get("tool", {}).get("ruff", {})
    ruff_target = ruff_table.get("target-version") if isinstance(ruff_table, dict) else None
    if isinstance(ruff_target, str):
        return parse_python_version(raw_version=ruff_target)
    return None


def _string_list(table: dict[str, object], key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"[tool.kaszanas-lint] `{key}` must be a list of strings")
    return value


def _default_source_roots(project_root: Path) -> tuple[Path, ...]:
    source_directory = project_root / "src"
    if source_directory.is_dir():
        return (source_directory, project_root)
    return (project_root,)


def load_configuration(project_root: Path, explicit_config_path: Path | None = None) -> LintConfiguration:
    pyproject_path = explicit_config_path or project_root / "pyproject.toml"
    document = _read_pyproject(pyproject_path=pyproject_path)
    table = _tool_table(document=document, pyproject_path=pyproject_path)

    source_roots = tuple(project_root / entry for entry in _string_list(table=table, key="src"))
    if not source_roots:
        source_roots = _default_source_roots(project_root=project_root)

    selected = set(_string_list(table=table, key="select") or ALL_RULE_CODES)
    selected -= set(_string_list(table=table, key="ignore"))

    raw_per_file_ignores = table.get("per-file-ignores", {})
    if not isinstance(raw_per_file_ignores, dict):
        raise ValueError("[tool.kaszanas-lint] `per-file-ignores` must be a table")
    per_file_ignores = tuple((pattern, frozenset(codes)) for pattern, codes in raw_per_file_ignores.items())

    ignored_call_patterns = tuple(
        _string_list(table=table, key="ignore-call-patterns") or DEFAULT_IGNORED_CALL_PATTERNS
    ) + tuple(_string_list(table=table, key="extend-ignore-call-patterns"))

    safe_decorators = frozenset(
        _string_list(table=table, key="safe-decorators") or DEFAULT_SAFE_DECORATORS
    ) | frozenset(_string_list(table=table, key="extend-safe-decorators"))

    return LintConfiguration(
        project_root=project_root,
        source_roots=source_roots,
        exclude_patterns=tuple(
            _string_list(table=table, key="exclude") + _string_list(table=table, key="extend-exclude")
        ),
        selected_rule_codes=frozenset(selected),
        per_file_ignores=per_file_ignores,
        allowed_leading_positional_arguments=int(table.get("allow-positional", 0)),
        max_positional_arguments_for_external_calls=int(table.get("max-positional-external", 2)),
        ignored_call_patterns=ignored_call_patterns,
        safe_decorators=safe_decorators,
        inspect_standard_library=bool(table.get("inspect-stdlib", True)),
        minimum_match_branches=int(table.get("min-match-branches", DEFAULT_MINIMUM_MATCH_BRANCHES)),
        target_python_version=detect_target_python_version(document=document, table=table),
    )
