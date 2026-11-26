"""Signatures of builtins and standard-library callables, via `inspect` on the running interpreter.

Only builtins and modules listed in `sys.stdlib_module_names` are ever imported, never project or
third-party code, so inspection cannot trigger arbitrary side effects.
"""

from __future__ import annotations

import builtins
import importlib
import inspect
import sys
from dataclasses import dataclass
from functools import lru_cache

NEVER_IMPORTED_STANDARD_LIBRARY_MODULES = frozenset(
    {
        "antigravity",
        "this",
        "idlelib",
        "tkinter",
        "turtle",
        "turtledemo",
        "__hello__",
        "__phello__",
        "pydoc",
        "webbrowser",
        "msvcrt",
        "winsound",
        "readline",
        "curses",
    }
)
STANDARD_LIBRARY_MODULE_NAMES: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", ()))


@dataclass(frozen=True, slots=True)
class ExternalPositionalSignature:
    positional_only_count: int
    positional_parameter_names: tuple[str, ...]
    accepts_variadic_positional: bool


def _signature_of(callable_object: object) -> ExternalPositionalSignature | None:
    try:
        signature = inspect.signature(callable_object)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    positional_only_count = 0
    positional_names: list[str] = []
    accepts_variadic_positional = False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional_only_count += 1
            positional_names.append(parameter.name)
        elif parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            positional_names.append(parameter.name)
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            accepts_variadic_positional = True
    return ExternalPositionalSignature(
        positional_only_count=positional_only_count,
        positional_parameter_names=tuple(positional_names),
        accepts_variadic_positional=accepts_variadic_positional,
    )


@lru_cache(maxsize=8192)
def lookup_external_signature(dotted_name: str) -> ExternalPositionalSignature | None:
    parts = dotted_name.split(".")
    if parts[0] == "builtins" and len(parts) >= 2:
        current: object = builtins
        attribute_names = parts[1:]
    else:
        if parts[0] not in STANDARD_LIBRARY_MODULE_NAMES:
            return None
        if parts[0] in NEVER_IMPORTED_STANDARD_LIBRARY_MODULES:
            return None
        current = None
        attribute_names = []
        for prefix_length in range(len(parts), 0, -1):
            module_name = ".".join(parts[:prefix_length])
            try:
                current = importlib.import_module(module_name)
            except Exception:
                continue
            attribute_names = parts[prefix_length:]
            break
        if current is None:
            return None
    for attribute_name in attribute_names:
        try:
            current = getattr(current, attribute_name)
        except AttributeError:
            return None
    if not callable(current) or inspect.ismodule(current):
        return None
    return _signature_of(callable_object=current)
