"""Resolve dotted names, class hierarchies and constructor signatures across the project index."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Literal

from kaszanas_lint.module_summarization import last_segment
from kaszanas_lint.symbols import ClassSymbol, FunctionSymbol, ModuleSummary

TRANSPARENT_BASE_CLASS_SEGMENTS = frozenset({"object", "Generic", "Protocol", "ABC"})
MAXIMUM_RESOLUTION_DEPTH = 12
BUILTIN_NAMES = frozenset(dir(builtins))

ParameterBinding = Literal["unbound", "drops_first", "first_is_positional_only"]
AccessMode = Literal["direct", "via_class", "via_instance"]


@dataclass(frozen=True, slots=True)
class ExternalSymbol:
    """Something outside the project (stdlib, third-party, builtins), by its expanded dotted name."""

    dotted_name: str


ResolvedSymbol = FunctionSymbol | ClassSymbol | ModuleSummary | ExternalSymbol | None


@dataclass(frozen=True, slots=True)
class _ExternalBaseMarker:
    dotted_name: str


MethodResolutionEntry = ClassSymbol | _ExternalBaseMarker


def binding_for_access(function: FunctionSymbol, access_mode: AccessMode) -> ParameterBinding:
    if function.method_kind in ("function", "static") or access_mode == "direct":
        return "unbound"
    if function.method_kind == "class":
        return "drops_first"
    # Instance method: bound through an instance, explicit `self` when reached via the class.
    if access_mode == "via_instance":
        return "drops_first"
    return "first_is_positional_only"


def effective_positional_parameters(
    function: FunctionSymbol, binding: ParameterBinding
) -> tuple[int, tuple[str, ...]]:
    """(count of leading slots that must stay positional, names of all positional slots)."""
    names = function.positional_only_parameters + function.positional_or_keyword_parameters
    positional_only_count = len(function.positional_only_parameters)
    if binding == "drops_first" and names:
        names = names[1:]
        positional_only_count = max(0, positional_only_count - 1)
    elif binding == "first_is_positional_only":
        positional_only_count = max(positional_only_count, 1)
    return positional_only_count, names


class SymbolResolver:
    def __init__(self, module_summaries: dict[str, ModuleSummary]) -> None:
        self.module_summaries = module_summaries
        self.classes_by_qualified_name: dict[str, ClassSymbol] = {}
        for summary in module_summaries.values():
            for class_symbol in summary.classes.values():
                self._register_class(class_symbol=class_symbol)
        self._dotted_cache: dict[str, ResolvedSymbol] = {}
        self._linearization_cache: dict[str, tuple[MethodResolutionEntry, ...]] = {}
        self._constructor_cache: dict[str, FunctionSymbol | None] = {}

    def _register_class(self, class_symbol: ClassSymbol) -> None:
        self.classes_by_qualified_name[class_symbol.qualified_name] = class_symbol
        for nested_class in class_symbol.nested_classes.values():
            self._register_class(class_symbol=nested_class)

    # ----------------------------------------------------------------- dotted names

    def resolve_dotted_name(self, dotted_name: str, depth: int = 0) -> ResolvedSymbol:
        if dotted_name in self._dotted_cache:
            return self._dotted_cache[dotted_name]
        if depth > MAXIMUM_RESOLUTION_DEPTH:
            return None
        self._dotted_cache[dotted_name] = None  # cycle guard for import loops
        resolved = self._resolve_dotted_name_uncached(dotted_name=dotted_name, depth=depth)
        self._dotted_cache[dotted_name] = resolved
        return resolved

    def _resolve_dotted_name_uncached(self, dotted_name: str, depth: int) -> ResolvedSymbol:
        parts = dotted_name.split(".")
        for prefix_length in range(len(parts), 0, -1):
            module_name = ".".join(parts[:prefix_length])
            if module_name in self.module_summaries:
                current: ResolvedSymbol = self.module_summaries[module_name]
                for attribute_name in parts[prefix_length:]:
                    current = self.member_of(
                        container=current, attribute_name=attribute_name, depth=depth + 1
                    )
                return current
        return ExternalSymbol(dotted_name=dotted_name)

    def member_of(self, container: ResolvedSymbol, attribute_name: str, depth: int = 0) -> ResolvedSymbol:
        if isinstance(container, ExternalSymbol):
            return ExternalSymbol(dotted_name=f"{container.dotted_name}.{attribute_name}")
        if isinstance(container, ModuleSummary):
            resolved = self.name_in_module(
                module_summary=container,
                name=attribute_name,
                depth=depth,
                include_builtins=False,
            )
            submodule_name = f"{container.module_name}.{attribute_name}"
            if resolved is None and submodule_name in self.module_summaries:
                return self.module_summaries[submodule_name]
            return resolved
        if isinstance(container, ClassSymbol):
            return self.find_attribute_in_class_hierarchy(
                class_symbol=container, attribute_name=attribute_name
            )
        return None

    def name_in_module(
        self,
        module_summary: ModuleSummary,
        name: str,
        depth: int = 0,
        include_builtins: bool = True,
    ) -> ResolvedSymbol:
        """Look up a module-global name the way code inside that module would see it."""
        if name in module_summary.untrustworthy_names:
            return None
        if name in module_summary.functions:
            return module_summary.functions[name]
        if name in module_summary.classes:
            return module_summary.classes[name]
        if name in module_summary.imported_names:
            return self.resolve_dotted_name(dotted_name=module_summary.imported_names[name], depth=depth + 1)
        if name in module_summary.module_level_bound_names:
            return None
        for star_module_name in module_summary.star_imported_modules:
            star_module = self.module_summaries.get(star_module_name)
            if star_module is None:
                return None  # names from an external star import are unknowable
            resolved = self.name_in_module(
                module_summary=star_module,
                name=name,
                depth=depth + 1,
                include_builtins=False,
            )
            if resolved is not None:
                return resolved
        if include_builtins and name in BUILTIN_NAMES:
            return ExternalSymbol(dotted_name=f"builtins.{name}")
        return None

    def resolve_name_in_module_by_name(self, module_name: str, dotted_name: str) -> ResolvedSymbol:
        module_summary = self.module_summaries.get(module_name)
        if module_summary is None:
            return None
        first_name, *attribute_names = dotted_name.split(".")
        current = self.name_in_module(module_summary=module_summary, name=first_name)
        for attribute_name in attribute_names:
            current = self.member_of(container=current, attribute_name=attribute_name)
        return current

    # ----------------------------------------------------------------- classes

    def linearize(self, class_symbol: ClassSymbol) -> tuple[MethodResolutionEntry, ...]:
        cache_key = class_symbol.qualified_name
        if cache_key in self._linearization_cache:
            return self._linearization_cache[cache_key]
        recursion_marker = _ExternalBaseMarker(dotted_name="<recursive>")
        self._linearization_cache[cache_key] = (class_symbol, recursion_marker)

        direct_bases: list[MethodResolutionEntry] = []
        base_linearizations: list[list[MethodResolutionEntry]] = []
        for base_expression in class_symbol.base_class_expressions:
            if base_expression is None:
                dynamic_marker = _ExternalBaseMarker(dotted_name="<dynamic>")
                direct_bases.append(dynamic_marker)
                base_linearizations.append([dynamic_marker])
                continue
            resolved_base = self.resolve_name_in_module_by_name(
                module_name=class_symbol.module_name, dotted_name=base_expression
            )
            if isinstance(resolved_base, ClassSymbol):
                direct_bases.append(resolved_base)
                base_linearizations.append(list(self.linearize(class_symbol=resolved_base)))
                continue
            external_name = (
                resolved_base.dotted_name if isinstance(resolved_base, ExternalSymbol) else base_expression
            )
            if last_segment(dotted_name=external_name) in TRANSPARENT_BASE_CLASS_SEGMENTS:
                continue
            external_marker = _ExternalBaseMarker(dotted_name=external_name)
            direct_bases.append(external_marker)
            base_linearizations.append([external_marker])

        merged = _c3_merge(sequences=[*base_linearizations, direct_bases])
        if merged is None:  # inconsistent hierarchy: fall back to depth-first, de-duplicated
            merged = []
            seen_keys: set[str] = set()
            for linearization in base_linearizations:
                for entry in linearization:
                    if _entry_key(entry=entry) not in seen_keys:
                        seen_keys.add(_entry_key(entry=entry))
                        merged.append(entry)
        result = (class_symbol, *merged)
        self._linearization_cache[cache_key] = result
        return result

    def find_attribute_in_class_hierarchy(
        self, class_symbol: ClassSymbol, attribute_name: str, skip_first: bool = False
    ) -> FunctionSymbol | ClassSymbol | None:
        entries = self.linearize(class_symbol=class_symbol)
        for entry in entries[1:] if skip_first else entries:
            if isinstance(entry, _ExternalBaseMarker):
                return None  # an external base could define it first
            if attribute_name in entry.untrustworthy_member_names:
                return None
            if attribute_name in entry.methods:
                return entry.methods[attribute_name]
            if attribute_name in entry.nested_classes:
                return entry.nested_classes[attribute_name]
        return None

    def constructor_signature(self, class_symbol: ClassSymbol) -> FunctionSymbol | None:
        """Signature of `ClassName(...)`, or None when it can't be known statically."""
        cache_key = class_symbol.qualified_name
        if cache_key not in self._constructor_cache:
            self._constructor_cache[cache_key] = self._constructor_signature_uncached(
                class_symbol=class_symbol
            )
        return self._constructor_cache[cache_key]

    def _constructor_signature_uncached(self, class_symbol: ClassSymbol) -> FunctionSymbol | None:
        if class_symbol.has_custom_metaclass or class_symbol.has_untrusted_class_decorator:
            return None
        entries = self.linearize(class_symbol=class_symbol)
        for entry in entries:
            if isinstance(entry, _ExternalBaseMarker):
                is_direct_named_tuple = (
                    last_segment(dotted_name=entry.dotted_name) == "NamedTuple" and entry is entries[1]
                )
                if is_direct_named_tuple:
                    return _synthesized_constructor(
                        qualified_name=class_symbol.qualified_name,
                        positional_names=tuple(field.name for field in class_symbol.annotated_fields),
                        keyword_only_names=(),
                    )
                return None
            if entry.has_custom_metaclass or "__new__" in entry.methods:
                return None
            if "__init__" in entry.untrustworthy_member_names:
                return None
            if "__init__" in entry.methods:
                initializer = entry.methods["__init__"]
                return initializer if initializer.is_signature_trustworthy else None
            if entry.dataclass_options is not None and entry.dataclass_options.generates_init:
                return self._dataclass_constructor(class_symbol=class_symbol, entries=entries)
        return _synthesized_constructor(
            qualified_name=class_symbol.qualified_name, positional_names=(), keyword_only_names=()
        )

    def _dataclass_constructor(
        self, class_symbol: ClassSymbol, entries: tuple[MethodResolutionEntry, ...]
    ) -> FunctionSymbol | None:
        ordered_fields: dict[str, tuple[bool, bool]] = {}
        for entry in reversed(entries):
            if isinstance(entry, _ExternalBaseMarker):
                return None
            if entry.dataclass_options is None:
                continue
            for field in entry.annotated_fields:
                ordered_fields[field.name] = (field.is_init_parameter, field.is_keyword_only)
        return _synthesized_constructor(
            qualified_name=class_symbol.qualified_name,
            positional_names=tuple(
                name
                for name, (is_init_parameter, is_keyword_only) in ordered_fields.items()
                if is_init_parameter and not is_keyword_only
            ),
            keyword_only_names=tuple(
                name
                for name, (is_init_parameter, is_keyword_only) in ordered_fields.items()
                if is_init_parameter and is_keyword_only
            ),
        )


def _synthesized_constructor(
    qualified_name: str, positional_names: tuple[str, ...], keyword_only_names: tuple[str, ...]
) -> FunctionSymbol:
    return FunctionSymbol(
        qualified_name=qualified_name,
        positional_only_parameters=(),
        positional_or_keyword_parameters=positional_names,
        keyword_only_parameters=keyword_only_names,
        accepts_variadic_positional=False,
        method_kind="static",
        is_signature_trustworthy=True,
    )


def _entry_key(entry: MethodResolutionEntry) -> str:
    if isinstance(entry, _ExternalBaseMarker):
        return f"external:{entry.dotted_name}"
    return f"project:{entry.qualified_name}"


def _c3_merge(
    sequences: list[list[MethodResolutionEntry]],
) -> list[MethodResolutionEntry] | None:
    pending = [list(sequence) for sequence in sequences if sequence]
    result: list[MethodResolutionEntry] = []
    while pending:
        candidate: MethodResolutionEntry | None = None
        for sequence in pending:
            head_key = _entry_key(entry=sequence[0])
            appears_in_a_tail = any(
                head_key == _entry_key(entry=entry) for other in pending for entry in other[1:]
            )
            if not appears_in_a_tail:
                candidate = sequence[0]
                break
        if candidate is None:
            return None
        candidate_key = _entry_key(entry=candidate)
        result.append(candidate)
        pending = [
            [entry for entry in sequence if _entry_key(entry=entry) != candidate_key] for sequence in pending
        ]
        pending = [sequence for sequence in pending if sequence]
    return result
