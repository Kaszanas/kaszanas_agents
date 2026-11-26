"""Source decoding and conversions between ast byte columns, character columns and offsets."""

from __future__ import annotations

import io
import tokenize
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DecodedSource:
    text: str
    encoding: str


def read_python_source(file_path: Path) -> DecodedSource:
    """Decode honoring PEP 263 cookies and BOMs, keeping the original line endings intact."""
    raw_bytes = file_path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw_bytes).readline)
    return DecodedSource(text=raw_bytes.decode(encoding), encoding=encoding)


def write_python_source(file_path: Path, text: str, encoding: str) -> None:
    file_path.write_bytes(text.encode(encoding))


class SourceText:
    """Line table for one source string. Lines are split on "\\n" only, like the tokenizer."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.split("\n")
        self.line_start_offsets: list[int] = []
        running_offset = 0
        for line in self.lines:
            self.line_start_offsets.append(running_offset)
            running_offset += len(line) + 1

    def character_column(self, line_number: int, byte_column: int) -> int:
        """Convert an ast `col_offset` (UTF-8 bytes) into a character index on that line."""
        line = self.lines[line_number - 1]
        if line.isascii():
            return byte_column
        return len(line.encode("utf-8")[:byte_column].decode("utf-8", errors="replace"))

    def offset_from_character_position(self, line_number: int, character_column: int) -> int:
        return self.line_start_offsets[line_number - 1] + character_column

    def offset_from_ast_position(self, line_number: int, byte_column: int) -> int:
        return self.offset_from_character_position(
            line_number=line_number,
            character_column=self.character_column(line_number=line_number, byte_column=byte_column),
        )
