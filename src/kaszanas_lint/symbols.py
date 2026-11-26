"""Picklable data model produced by the indexing pass and consumed by the checking pass."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MethodKind = Literal["function", "instance", "class", "static"]


@dataclass(frozen=True, slots=True)
class FunctionSymbol:
    """Signature of a function or method defined in the project."""

    qualified_name: str
    positional_only_parameters: tuple[str, ...]
    positional_or_keyword_parameters: tuple[str, ...]
    keyword_only_parameters: tuple[str, ...]
    accepts_variadic_positional: bool
    method_kind: MethodKind
    is_signature_trustworthy: bool


@dataclass(frozen=True, slots=True)
class DataclassField:
    """An annotated class-body field, used to synthesize dataclass / NamedTuple constructors."""

    name: str
    is_init_parameter: bool
    is_keyword_only: bool


@dataclass(frozen=True, slots=True)
class DataclassOptions:
    generates_init: bool
    all_fields_keyword_only: bool


@dataclass(slots=True)
class ClassSymbol:
    """A class defined in the project, with enough detail to resolve methods and constructors."""

    qualified_name: str
    module_name: str
    base_class_expressions: tuple[str | None, ...]
    methods: dict[str, FunctionSymbol] = field(default_factory=dict)
    nested_classes: dict[str, ClassSymbol] = field(default_factory=dict)
    untrustworthy_member_names: frozenset[str] = frozenset()
    annotated_fields: tuple[DataclassField, ...] = ()
    dataclass_options: DataclassOptions | None = None
    has_untrusted_class_decorator: bool = False
    has_custom_metaclass: bool = False


@dataclass(slots=True)
class ModuleSummary:
    """Everything the checker needs to know about one module, without keeping its AST."""

    module_name: str
    relative_path: str
    is_package: bool
    functions: dict[str, FunctionSymbol] = field(default_factory=dict)
    classes: dict[str, ClassSymbol] = field(default_factory=dict)
    imported_names: dict[str, str] = field(default_factory=dict)
    star_imported_modules: tuple[str, ...] = ()
    module_level_bound_names: frozenset[str] = frozenset()
    untrustworthy_names: frozenset[str] = frozenset()
    syntax_error: str | None = None
