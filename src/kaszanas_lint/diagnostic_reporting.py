"""Render diagnostics as ruff-style text, JSON, or GitHub Actions annotations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from kaszanas_lint.diagnostics import Diagnostic


def display_path(relative_path: str, project_root: Path, working_directory: Path) -> str:
    absolute_path = project_root / relative_path
    try:
        return os.path.relpath(absolute_path, working_directory)
    except ValueError:  # different drive on Windows
        return str(absolute_path)


def format_text(diagnostics: list[Diagnostic], project_root: Path, working_directory: Path) -> str:
    lines = []
    for diagnostic in diagnostics:
        path = display_path(
            relative_path=diagnostic.relative_path,
            project_root=project_root,
            working_directory=working_directory,
        )
        fixable_marker = " [*]" if diagnostic.is_fixable else ""
        lines.append(
            f"{path}:{diagnostic.line}:{diagnostic.column}: "
            f"{diagnostic.code}{fixable_marker} {diagnostic.message}"
        )
    return "\n".join(lines)


def format_json(diagnostics: list[Diagnostic], project_root: Path) -> str:
    return json.dumps(
        [
            {
                "code": diagnostic.code,
                "message": diagnostic.message,
                "filename": str(project_root / diagnostic.relative_path),
                "location": {"row": diagnostic.line, "column": diagnostic.column},
                "end_location": {"row": diagnostic.end_line, "column": diagnostic.end_column},
                "fixable": diagnostic.is_fixable,
            }
            for diagnostic in diagnostics
        ],
        indent=2,
    )


def format_github(diagnostics: list[Diagnostic], project_root: Path, working_directory: Path) -> str:
    lines = []
    for diagnostic in diagnostics:
        path = display_path(
            relative_path=diagnostic.relative_path,
            project_root=project_root,
            working_directory=working_directory,
        ).replace("\\", "/")
        lines.append(
            f"::error title=kaszanas-lint ({diagnostic.code}),file={path},"
            f"line={diagnostic.line},col={diagnostic.column},endLine={diagnostic.end_line},"
            f"endColumn={diagnostic.end_column}::{diagnostic.code} {diagnostic.message}"
        )
    return "\n".join(lines)


def format_summary(remaining: list[Diagnostic], fixed_count: int, would_fix: bool) -> str:
    parts = []
    if fixed_count:
        verb = "Would fix" if would_fix else "Fixed"
        parts.append(f"{verb} {fixed_count} error{'s' if fixed_count != 1 else ''}.")
    if remaining:
        fixable = sum(1 for item in remaining if item.is_fixable)
        message = f"Found {len(remaining)} error{'s' if len(remaining) != 1 else ''}."
        if fixable:
            message += f"\n[*] {fixable} fixable with `kaszanas-lint check --fix`."
        parts.append(message)
    elif not fixed_count:
        parts.append("All checks passed!")
    return "\n".join(parts)
