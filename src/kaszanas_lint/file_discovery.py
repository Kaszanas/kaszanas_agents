"""Find the project's Python files: `git ls-files` when available, a pruned walk otherwise."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from kaszanas_lint.configuration import DEFAULT_EXCLUDED_DIRECTORY_NAMES
from kaszanas_lint.suppression_comments import matches_path_pattern


def relative_posix_path(file_path: Path, project_root: Path) -> str:
    """Pure string computation: callers pass already-resolved absolute paths (no filesystem hits)."""
    try:
        return os.path.relpath(file_path, project_root).replace(os.sep, "/")
    except ValueError:  # another drive on Windows
        return file_path.as_posix()


def is_excluded(relative_path: str, exclude_patterns: tuple[str, ...]) -> bool:
    components = relative_path.split("/")
    if any(component in DEFAULT_EXCLUDED_DIRECTORY_NAMES for component in components[:-1]):
        return True
    return any(
        matches_path_pattern(relative_posix_path=relative_path, pattern=pattern)
        for pattern in exclude_patterns
    )


def _files_from_git(project_root: Path) -> list[Path] | None:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
            cwd=project_root,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    entries = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [project_root / entry for entry in entries if entry]


def _files_from_walk(project_root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, subdirectory_names, file_names in os.walk(project_root):
        subdirectory_names[:] = [
            name
            for name in subdirectory_names
            if name not in DEFAULT_EXCLUDED_DIRECTORY_NAMES and not name.endswith(".egg-info")
        ]
        found.extend(Path(directory) / name for name in file_names if name.endswith(".py"))
    return found


def discover_project_python_files(project_root: Path, exclude_patterns: tuple[str, ...]) -> list[Path]:
    project_root = project_root.resolve()
    candidates = _files_from_git(project_root=project_root)
    if candidates is None:
        candidates = _files_from_walk(project_root=project_root)
    discovered: list[Path] = []
    for candidate in candidates:
        relative_path = relative_posix_path(file_path=candidate, project_root=project_root)
        if is_excluded(relative_path=relative_path, exclude_patterns=exclude_patterns):
            continue
        if os.path.isfile(candidate):  # git lists tracked files that were deleted locally
            discovered.append(candidate)
    return sorted(set(discovered))
