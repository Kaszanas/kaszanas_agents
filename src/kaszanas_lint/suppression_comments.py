"""`# noqa` comments and per-file-ignores, ruff-compatible."""

from __future__ import annotations

import re
from fnmatch import fnmatch

NOQA_COMMENT_PATTERN = re.compile(
    r"#\s*noqa(?::\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*))?",
    flags=re.IGNORECASE,
)


def is_suppressed_by_noqa_comment(line_text: str, code: str) -> bool:
    if "noqa" not in line_text.lower():
        return False
    for match in NOQA_COMMENT_PATTERN.finditer(line_text):
        listed_codes = match.group("codes")
        if listed_codes is None:
            return True  # blanket `# noqa`
        if code.upper() in {item.upper() for item in re.split(r"[\s,]+", listed_codes) if item}:
            return True
    return False


def matches_path_pattern(relative_posix_path: str, pattern: str) -> bool:
    """Glob match where `*`/`**` may cross directories and bare names match any path component."""
    normalized_pattern = pattern.replace("\\", "/").removeprefix("./")
    if fnmatch(relative_posix_path, normalized_pattern):
        return True
    if fnmatch(relative_posix_path, normalized_pattern.rstrip("/") + "/*"):
        return True
    if "/" not in normalized_pattern:
        return any(fnmatch(component, normalized_pattern) for component in relative_posix_path.split("/"))
    return False


def codes_ignored_for_path(
    relative_posix_path: str, per_file_ignores: tuple[tuple[str, frozenset[str]], ...]
) -> frozenset[str]:
    ignored: set[str] = set()
    for pattern, codes in per_file_ignores:
        if matches_path_pattern(relative_posix_path=relative_posix_path, pattern=pattern):
            ignored.update(codes)
    return frozenset(ignored)
