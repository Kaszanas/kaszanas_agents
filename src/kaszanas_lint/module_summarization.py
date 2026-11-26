"""Pass 1: reduce one module's AST to a picklable ModuleSummary (signatures, classes, imports)."""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterator

from kaszanas_lint.symbols import (
    ClassSymbol,
    DataclassField,
    DataclassOptions,
    FunctionSymbol,
    MethodKind,
    ModuleSummary,
)

COMPOUND_STATEMENT_BODY_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
PROPERTY_DECORATOR_SEGMENTS = frozenset({"property", "cached_property", "setter", "getter", "deleter"})
NESTED_SCOPE_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def dotted_name_of(expression: ast.expr) -> str | None:
    """`a.b.c` for Name/Attribute chains, otherwise None."""
    attribute_names: list[str] = []
    while isinstance(expression, ast.Attribute):
        attribute_names.append(expression.attr)
        expression = expression.value
    if isinstance(expression, ast.Name):
        attribute_names.append(expression.id)
        return ".".join(reversed(attribute_names))
    return None


def decorator_dotted_name(decorator: ast.expr) -> str | None:
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    return dotted_name_of(expression=decorator)


def last_segment(dotted_name: str) -> str:
    return dotted_name.rsplit(".", maxsplit=1)[-1]


def is_safe_decorator(dotted_name: str | None, safe_decorators: frozenset[str]) -> bool:
    if dotted_name is None:
        return False
    return dotted_name in safe_decorators or last_segment(dotted_name=dotted_name) in safe_decorators


def resolve_relative_module(
    current_module_name: str, current_is_package: bool, level: int, imported_module: str | None
) -> str | None:
    if level == 0:
        return imported_module
    package_parts = current_module_name.split(".") if current_module_name else []
    if not current_is_package:
        package_parts = package_parts[:-1]
    if level - 1 > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    if imported_module:
        base_parts = [*base_parts, imported_module]
    return ".".join(base_parts) or None


def build_function_symbol(
    function_node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
    is_defined_in_class_body: bool,
    safe_decorators: frozenset[str],
) -> FunctionSymbol:
    decorator_names = [decorator_dotted_name(decorator=item) for item in function_node.decorator_list]
    decorator_segments = {last_segment(dotted_name=name) for name in decorator_names if name}

    method_kind: MethodKind = "function"
    if is_defined_in_class_body:
        if "staticmethod" in decorator_segments or function_node.name == "__new__":
            method_kind = "static"
        elif "classmethod" in decorator_segments or function_node.name in {
            "__init_subclass__",
            "__class_getitem__",
        }:
            method_kind = "class"
        else:
            method_kind = "instance"

    is_trustworthy = (
        all(is_safe_decorator(dotted_name=name, safe_decorators=safe_decorators) for name in decorator_names)
        and "overload" not in decorator_segments
        and not (is_defined_in_class_body and decorator_segments & PROPERTY_DECORATOR_SEGMENTS)
    )

    arguments = function_node.args
    return FunctionSymbol(
        qualified_name=qualified_name,
        positional_only_parameters=tuple(argument.arg for argument in arguments.posonlyargs),
        positional_or_keyword_parameters=tuple(argument.arg for argument in arguments.args),
        keyword_only_parameters=tuple(argument.arg for argument in arguments.kwonlyargs),
        accepts_variadic_positional=arguments.vararg is not None,
        method_kind=method_kind,
        is_signature_trustworthy=is_trustworthy,
    )


def iterate_statements_in_same_scope(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements, descending into if/try/with/for/while/match bodies but not new scopes."""
    for statement in statements:
        yield statement
        if isinstance(statement, NESTED_SCOPE_NODE_TYPES):
            continue
        for field_name in COMPOUND_STATEMENT_BODY_FIELDS:
            children = getattr(statement, field_name, None)
            if not children:
                continue
            for child in children:
                if isinstance(child, ast.stmt):
                    yield from iterate_statements_in_same_scope(statements=[child])
                else:  # ExceptHandler / match_case carry their own bodies
                    yield from iterate_statements_in_same_scope(statements=child.body)


def stored_names_in_same_scope(node: ast.AST, include_definition_names: bool = True) -> Iterator[str]:
    """Names bound by assignment-like constructs, without entering nested function/class scopes.

    `include_definition_names=False` leaves out names bound by `def`/`class` statements, for
    callers that record those separately.
    """
    if isinstance(node, NESTED_SCOPE_NODE_TYPES):
        if include_definition_names and not isinstance(node, ast.Lambda):
            yield node.name
        return
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store | ast.Del):
            yield current.id
        elif (isinstance(current, ast.ExceptHandler) and current.name) or (
            isinstance(current, ast.MatchAs | ast.MatchStar) and current.name
        ):
            yield current.name
        elif isinstance(current, ast.MatchMapping) and current.rest:
            yield current.rest
        for child in ast.iter_child_nodes(current):
            if isinstance(child, NESTED_SCOPE_NODE_TYPES):
                if include_definition_names and not isinstance(child, ast.Lambda):
                    yield child.name
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                continue
            pending.append(child)


def global_declarations_inside_functions(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _is_annotation_named(annotation: ast.expr, expected_segment: str) -> bool:
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return expected_segment in annotation.value
    dotted_name = dotted_name_of(expression=annotation)
    return dotted_name is not None and last_segment(dotted_name=dotted_name) == expected_segment


def _keyword_constant(call: ast.Call, keyword_name: str) -> object:
    for keyword in call.keywords:
        if keyword.arg == keyword_name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


class _NameBindings:
    """Collects every way a name gets bound inside one scope."""

    def __init__(self) -> None:
        self.function_symbols: dict[str, list[FunctionSymbol]] = defaultdict(list)
        self.class_symbols: dict[str, list[ClassSymbol]] = defaultdict(list)
        self.import_targets: dict[str, list[str]] = defaultdict(list)
        self.assignment_counts: dict[str, int] = defaultdict(int)

    def all_names(self) -> set[str]:
        return (
            set(self.function_symbols)
            | set(self.class_symbols)
            | set(self.import_targets)
            | set(self.assignment_counts)
        )

    def binding_kind_count(self, name: str) -> int:
        return sum(
            1
            for mapping in (
                self.function_symbols,
                self.class_symbols,
                self.import_targets,
                self.assignment_counts,
            )
            if name in mapping
        )


class ModuleSummarizer:
    def __init__(
        self,
        module_name: str,
        relative_path: str,
        is_package: bool,
        safe_decorators: frozenset[str],
    ) -> None:
        self.module_name = module_name
        self.relative_path = relative_path
        self.is_package = is_package
        self.safe_decorators = safe_decorators

    def qualify(self, *name_parts: str) -> str:
        return ".".join(part for part in (self.module_name, *name_parts) if part)

    def summarize(self, tree: ast.Module) -> ModuleSummary:
        bindings = _NameBindings()
        star_imported_modules: list[str] = []

        for statement in iterate_statements_in_same_scope(statements=tree.body):
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                bindings.function_symbols[statement.name].append(
                    build_function_symbol(
                        function_node=statement,
                        qualified_name=self.qualify(statement.name),
                        is_defined_in_class_body=False,
                        safe_decorators=self.safe_decorators,
                    )
                )
            elif isinstance(statement, ast.ClassDef):
                bindings.class_symbols[statement.name].append(
                    self.summarize_class(class_node=statement, qualified_name_parts=(statement.name,))
                )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.asname:
                        bindings.import_targets[alias.asname].append(alias.name)
                    else:
                        top_level_name = alias.name.split(".", maxsplit=1)[0]
                        bindings.import_targets[top_level_name].append(top_level_name)
            elif isinstance(statement, ast.ImportFrom):
                source_module = resolve_relative_module(
                    current_module_name=self.module_name,
                    current_is_package=self.is_package,
                    level=statement.level,
                    imported_module=statement.module,
                )
                for alias in statement.names:
                    if source_module is None:
                        bindings.assignment_counts[alias.asname or alias.name] += 1
                    elif alias.name == "*":
                        star_imported_modules.append(source_module)
                    else:
                        bindings.import_targets[alias.asname or alias.name].append(
                            f"{source_module}.{alias.name}"
                        )

        for statement in tree.body:
            for stored_name in stored_names_in_same_scope(node=statement, include_definition_names=False):
                bindings.assignment_counts[stored_name] += 1
        for global_name in global_declarations_inside_functions(tree=tree):
            bindings.assignment_counts[global_name] += 1

        summary = ModuleSummary(
            module_name=self.module_name,
            relative_path=self.relative_path,
            is_package=self.is_package,
            star_imported_modules=tuple(star_imported_modules),
            module_level_bound_names=frozenset(bindings.all_names()),
        )
        untrustworthy: set[str] = set()
        for name in bindings.all_names():
            if bindings.binding_kind_count(name=name) > 1:
                untrustworthy.add(name)
            elif name in bindings.function_symbols:
                symbols = bindings.function_symbols[name]
                if len(set(symbols)) == 1:
                    summary.functions[name] = symbols[0]
                else:
                    untrustworthy.add(name)
            elif name in bindings.class_symbols:
                if len(bindings.class_symbols[name]) == 1:
                    summary.classes[name] = bindings.class_symbols[name][0]
                else:
                    untrustworthy.add(name)
            elif name in bindings.import_targets:
                if len(set(bindings.import_targets[name])) == 1:
                    summary.imported_names[name] = bindings.import_targets[name][0]
                else:
                    untrustworthy.add(name)
        summary.untrustworthy_names = frozenset(untrustworthy)
        return summary

    def summarize_class(self, class_node: ast.ClassDef, qualified_name_parts: tuple[str, ...]) -> ClassSymbol:
        base_class_expressions: list[str | None] = []
        for base in class_node.bases:
            if isinstance(base, ast.Subscript):
                base = base.value
            base_class_expressions.append(dotted_name_of(expression=base))

        has_custom_metaclass = False
        for keyword in class_node.keywords:
            if keyword.arg == "metaclass":
                metaclass_name = dotted_name_of(expression=keyword.value)
                if metaclass_name is None or last_segment(dotted_name=metaclass_name) != "ABCMeta":
                    has_custom_metaclass = True

        dataclass_options: DataclassOptions | None = None
        has_untrusted_class_decorator = False
        for decorator in class_node.decorator_list:
            decorator_name = decorator_dotted_name(decorator=decorator)
            is_dataclass_decorator = (
                decorator_name is not None and last_segment(dotted_name=decorator_name) == "dataclass"
            )
            if is_dataclass_decorator and isinstance(decorator, ast.Call):
                dataclass_options = DataclassOptions(
                    generates_init=_keyword_constant(call=decorator, keyword_name="init") is not False,
                    all_fields_keyword_only=_keyword_constant(call=decorator, keyword_name="kw_only") is True,
                )
            elif is_dataclass_decorator:
                dataclass_options = DataclassOptions(generates_init=True, all_fields_keyword_only=False)
            elif not is_safe_decorator(dotted_name=decorator_name, safe_decorators=self.safe_decorators):
                has_untrusted_class_decorator = True

        class_symbol = ClassSymbol(
            qualified_name=self.qualify(*qualified_name_parts),
            module_name=self.module_name,
            base_class_expressions=tuple(base_class_expressions),
            dataclass_options=dataclass_options,
            has_untrusted_class_decorator=has_untrusted_class_decorator,
            has_custom_metaclass=has_custom_metaclass,
        )

        bindings = _NameBindings()
        for statement in iterate_statements_in_same_scope(statements=class_node.body):
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                bindings.function_symbols[statement.name].append(
                    build_function_symbol(
                        function_node=statement,
                        qualified_name=self.qualify(*qualified_name_parts, statement.name),
                        is_defined_in_class_body=True,
                        safe_decorators=self.safe_decorators,
                    )
                )
            elif isinstance(statement, ast.ClassDef):
                bindings.class_symbols[statement.name].append(
                    self.summarize_class(
                        class_node=statement,
                        qualified_name_parts=(*qualified_name_parts, statement.name),
                    )
                )
            elif isinstance(statement, ast.Import | ast.ImportFrom):
                for alias in statement.names:
                    bindings.assignment_counts[alias.asname or alias.name.split(".")[0]] += 1
        for statement in class_node.body:
            for stored_name in stored_names_in_same_scope(node=statement, include_definition_names=False):
                bindings.assignment_counts[stored_name] += 1

        untrustworthy: set[str] = set()
        for name, symbols in bindings.function_symbols.items():
            # Property setters reuse the getter's name (`@x.setter def x(...)`): distinct symbols.
            if name in bindings.assignment_counts or name in bindings.class_symbols:
                untrustworthy.add(name)
            elif len(set(symbols)) == 1:
                class_symbol.methods[name] = symbols[0]
            else:
                untrustworthy.add(name)
        for name, symbols in bindings.class_symbols.items():
            if len(symbols) == 1 and name not in bindings.assignment_counts:
                class_symbol.nested_classes[name] = symbols[0]
            else:
                untrustworthy.add(name)
        class_symbol.untrustworthy_member_names = frozenset(untrustworthy)
        class_symbol.annotated_fields = self.collect_annotated_fields(
            class_node=class_node, dataclass_options=dataclass_options
        )
        return class_symbol

    def collect_annotated_fields(
        self, class_node: ast.ClassDef, dataclass_options: DataclassOptions | None
    ) -> tuple[DataclassField, ...]:
        keyword_only_from_here = bool(dataclass_options and dataclass_options.all_fields_keyword_only)
        fields: list[DataclassField] = []
        for statement in class_node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            if _is_annotation_named(annotation=statement.annotation, expected_segment="ClassVar"):
                continue
            if _is_annotation_named(annotation=statement.annotation, expected_segment="KW_ONLY"):
                keyword_only_from_here = True
                continue
            is_init_parameter = True
            is_keyword_only = keyword_only_from_here
            if isinstance(statement.value, ast.Call):
                called_name = dotted_name_of(expression=statement.value.func)
                if called_name is not None and last_segment(dotted_name=called_name) == "field":
                    if _keyword_constant(call=statement.value, keyword_name="init") is False:
                        is_init_parameter = False
                    explicit_keyword_only = _keyword_constant(call=statement.value, keyword_name="kw_only")
                    if isinstance(explicit_keyword_only, bool):
                        is_keyword_only = explicit_keyword_only
            fields.append(
                DataclassField(
                    name=statement.target.id,
                    is_init_parameter=is_init_parameter,
                    is_keyword_only=is_keyword_only,
                )
            )
        return tuple(fields)


def summarize_module_source(
    source: str,
    module_name: str,
    relative_path: str,
    is_package: bool,
    safe_decorators: frozenset[str],
) -> ModuleSummary:
    try:
        tree = ast.parse(source, filename=relative_path)
    except (SyntaxError, ValueError) as parse_error:
        return ModuleSummary(
            module_name=module_name,
            relative_path=relative_path,
            is_package=is_package,
            syntax_error=str(parse_error),
        )
    summarizer = ModuleSummarizer(
        module_name=module_name,
        relative_path=relative_path,
        is_package=is_package,
        safe_decorators=safe_decorators,
    )
    return summarizer.summarize(tree=tree)
