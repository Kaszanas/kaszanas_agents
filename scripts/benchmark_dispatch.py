# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Micro-benchmark: if/elif vs match vs dict lookup for value dispatch, and isinstance vs class patterns.

    uv run scripts/benchmark_dispatch.py
    uv run --python 3.10 scripts/benchmark_dispatch.py      # compare interpreters

Prints nanoseconds per call for the first branch, the last branch and a miss.
"""

from __future__ import annotations

import platform
import sys
import timeit

BRANCH_COUNT = 7
CALLS_PER_MEASUREMENT = 1_000_000
REPEATS = 5


def value_dispatch_sources() -> dict[str, str]:
    letters = "abcdefg"
    if_lines = ["def dispatch(value):"]
    match_lines = ["def dispatch(value):", "    match value:"]
    for index in range(BRANCH_COUNT):
        keyword = "if" if index == 0 else "elif"
        if_lines.append(f"    {keyword} value == {index + 1}: return {letters[index]!r}")
        match_lines.append(f"        case {index + 1}: return {letters[index]!r}")
    if_lines.append("    else: return 'z'")
    match_lines.append("        case _: return 'z'")
    table = ", ".join(f"{index + 1}: {letters[index]!r}" for index in range(BRANCH_COUNT))
    dict_source = f"TABLE = {{{table}}}\ndef dispatch(value):\n    return TABLE.get(value, 'z')"
    return {"if/elif": "\n".join(if_lines), "match": "\n".join(match_lines), "dict.get": dict_source}


def class_dispatch_sources() -> dict[str, str]:
    classes = "".join(f"class K{index}: pass\n" for index in range(4))
    instances = "INSTANCES = [" + ", ".join(f"K{index}()" for index in range(4)) + "]\n"
    if_body = "".join(
        f"    {'if' if index == 0 else 'elif'} isinstance(value, K{index}): return {index}\n"
        for index in range(4)
    )
    match_body = "    match value:\n" + "".join(
        f"        case K{index}(): return {index}\n" for index in range(4)
    )
    return {
        "isinstance chain": classes + instances + "def dispatch(value):\n" + if_body + "    return -1",
        "match class patterns": classes + instances + "def dispatch(value):\n" + match_body + "    return -1",
    }


def nanoseconds_per_call(setup: str, statement: str) -> float:
    best = min(timeit.repeat(statement, setup=setup, number=CALLS_PER_MEASUREMENT, repeat=REPEATS))
    return best * 1e9 / CALLS_PER_MEASUREMENT


def main() -> int:
    print(f"Python {platform.python_version()} ({platform.python_implementation()}) on {platform.system()}")
    print(f"\n{'value dispatch':22} {'first':>8} {'last':>8} {'miss':>8}   (ns/call)")
    for name, source in value_dispatch_sources().items():
        timings = [
            nanoseconds_per_call(setup=source, statement=f"dispatch({value})")
            for value in (1, BRANCH_COUNT, 99)
        ]
        print(f"{name:22} " + " ".join(f"{timing:8.1f}" for timing in timings))
    print(f"\n{'class dispatch':22} {'first':>8} {'last':>8}")
    for name, source in class_dispatch_sources().items():
        timings = [
            nanoseconds_per_call(setup=source, statement=f"dispatch(INSTANCES[{index}])") for index in (0, 3)
        ]
        print(f"{name:22} " + " ".join(f"{timing:8.1f}" for timing in timings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
