"""The diagnostic record shared by rules, the session and reporters (picklable)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Diagnostic:
    relative_path: str
    line: int
    column: int  # 1-based, in characters
    end_line: int
    end_column: int
    code: str
    message: str
    is_fixable: bool

    def sort_key(self) -> tuple[str, int, int, str]:
        return (self.relative_path, self.line, self.column, self.code)
