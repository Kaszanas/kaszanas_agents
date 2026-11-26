# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Claude Code PostToolUse hook: lint the Python file Claude just edited with kaszanas-lint.

Exit code 2 sends the findings back to Claude so it fixes them in the same turn; any other
situation (not a .py file, checker unavailable, clean file) exits 0 and stays silent.

Checker lookup order:
  1. `kaszanas-lint` on PATH (e.g. `uv tool install git+https://github.com/Kaszanas/kaszanas_agents`)
  2. `$KASZANAS_LINT_SPEC` run through uvx (e.g. `git+https://github.com/Kaszanas/kaszanas_agents`)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

MAXIMUM_REPORTED_LINES = 25


def checker_command() -> list[str] | None:
    installed = shutil.which("kaszanas-lint")
    if installed:
        return [installed]
    specification = os.environ.get("KASZANAS_LINT_SPEC")
    uvx = shutil.which("uvx")
    if specification and uvx:
        return [uvx, "--quiet", "--from", specification, "kaszanas-lint"]
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not file_path.endswith((".py", ".pyi")) or not os.path.isfile(file_path):
        return 0
    command = checker_command()
    if command is None:
        return 0

    try:
        completed = subprocess.run(
            [*command, "check", "--quiet", file_path],
            cwd=payload.get("cwd") or os.path.dirname(file_path) or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=55,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if completed.returncode != 1:
        return 0

    findings = completed.stdout.strip().splitlines()
    shown = findings[:MAXIMUM_REPORTED_LINES]
    if len(findings) > len(shown):
        shown.append(f"... and {len(findings) - len(shown)} more")
    print(
        "kaszanas-lint: calls to project-defined callables must use keyword arguments.\n"
        + "\n".join(shown)
        + f"\nAuto-fix KWA001 with: kaszanas-lint check --fix {file_path}"
        + "\nKWA002 (external calls) needs keywords added by hand.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
