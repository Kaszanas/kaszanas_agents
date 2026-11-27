"""KWA rules: prefer keyword arguments at call sites.

KWA001  A call to a callable defined in this project passes arguments positionally even though
        the parameters accept keywords. Fixable: `f(1, 2)` -> `f(a=1, b=2)`.
KWA002  A call to something outside the project (or not statically resolvable) passes more than
        `max-positional-external` positional arguments.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Literal

from kaszanas_lint.automatic_fixes import KeywordInsertionPlan
from kaszanas_lint.configuration import LintConfiguration
from kaszanas_lint.external_signatures import lookup_external_signature
from kaszanas_lint.module_summarization import (
    build_function_symbol,
    dotted_name_of,
    is_safe_decorator,
    resolve_relative_module,
    stored_names_in_same_scope,
)
from kaszanas_lint.rules.rule_framework import RuleContext, RuleFinding
from kaszanas_lint.symbol_resolution import (
    AccessMode,
    ExternalSymbol,
    ResolvedSymbol,
    SymbolResolver,
    binding_for_access,
    effective_positional_parameters,
)
from kaszanas_lint.symbols import ClassSymbol, FunctionSymbol, ModuleSummary

PROJECT_CALL_CODE = "KWA001"
EXTERNAL_CALL_CODE = "KWA002"

RULE_DESCRIPTIONS = {
    PROJECT_CALL_CODE: "Call to a project-defined callable passes keyword-capable arguments "
    "positionally (fixable).",
    EXTERNAL_CALL_CODE: "Call to an external or unresolvable callable passes more positional "
    "arguments than `max-positional-external`.",
}


@dataclass(frozen=True, slots=True)
class InstanceReference:
    """The `self`/`cls` parameter of a method, or `super()` inside one."""

    class_symbol: ClassSymbol
    mode: Literal["instance", "class"]
    skip_own_class: bool = False


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    pass


NameReference = ResolvedSymbol | InstanceReference | UnresolvedReference
UNRESOLVED = UnresolvedReference()


@dataclass(slots=True)
class LexicalScope:
    kind: Literal["module", "class", "function", "comprehension"]
    bound_names: set[str] = field(default_factory=set)
    global_names: set[str] = field(default_factory=set)
    local_functions: dict[str, FunctionSymbol] = field(default_factory=dict)
    local_imports: dict[str, str] = field(default_factory=dict)
    instance_parameter_name: str | None = None
    instance_reference: InstanceReference | None = None
    class_symbol: ClassSymbol | None = None


def _names_bound_in_function(
    function_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> tuple[set[str], set[str], list[ast.stmt]]:
    """(bound names, global/nonlocal names, nested def/class/import statements)."""
    arguments = function_node.args
    bound_names = {
        argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    if arguments.vararg:
        bound_names.add(arguments.vararg.arg)
    if arguments.kwarg:
        bound_names.add(arguments.kwarg.arg)
    global_names: set[str] = set()
    binding_statements: list[ast.stmt] = []
    body = function_node.body if isinstance(function_node.body, list) else [function_node.body]
    pending: list[ast.AST] = list(body)
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Global | ast.Nonlocal):
            global_names.update(current.names)
            continue
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            binding_statements.append(current)
            continue
        if isinstance(current, ast.Lambda):
            continue
        if isinstance(current, ast.Import | ast.ImportFrom):
            binding_statements.append(current)
            continue
        if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store | ast.Del):
            bound_names.add(current.id)
        elif (isinstance(current, ast.ExceptHandler) and current.name) or (
            isinstance(current, ast.MatchAs | ast.MatchStar) and current.name
        ):
            bound_names.add(current.name)
        pending.extend(ast.iter_child_nodes(current))
    return bound_names - global_names, global_names, binding_statements


class KeywordArgumentChecker(ast.NodeVisitor):
    def __init__(
        self,
        module_summary: ModuleSummary,
        resolver: SymbolResolver,
        configuration: LintConfiguration,
    ) -> None:
        self.module_summary = module_summary
        self.resolver = resolver
        self.configuration = configuration
        self.check_project_calls = PROJECT_CALL_CODE in configuration.selected_rule_codes
        self.check_external_calls = EXTERNAL_CALL_CODE in configuration.selected_rule_codes
        self.ignored_call_regexes = [re.compile(pattern) for pattern in configuration.ignored_call_patterns]
        self.scopes: list[LexicalScope] = [LexicalScope(kind="module")]
        self.class_name_stack: list[str | None] = []
        self.findings: list[RuleFinding] = []

    # ------------------------------------------------------------------ scopes

    def _current_class_symbol(self, class_name: str) -> ClassSymbol | None:
        if any(scope.kind not in ("module", "class") for scope in self.scopes):
            return None  # classes defined inside functions are not indexed
        enclosing_names = [name for name in self.class_name_stack if name is not None]
        if len(enclosing_names) != len(self.class_name_stack):
            return None
        qualified_name = ".".join(
            part for part in (self.module_summary.module_name, *enclosing_names, class_name) if part
        )
        return self.resolver.classes_by_qualified_name.get(qualified_name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(expression)
        class_symbol = self._current_class_symbol(class_name=node.name)
        scope = LexicalScope(kind="class", class_symbol=class_symbol)
        for statement in node.body:
            scope.bound_names.update(stored_names_in_same_scope(node=statement))
        self.scopes.append(scope)
        self.class_name_stack.append(node.name if class_symbol is not None else None)
        for statement in node.body:
            self.visit(statement)
        self.class_name_stack.pop()
        self.scopes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                self.visit(decorator)
            if node.returns is not None:
                self.visit(node.returns)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

        bound_names, global_names, binding_statements = _names_bound_in_function(function_node=node)
        scope = LexicalScope(kind="function", bound_names=bound_names, global_names=global_names)
        self._register_local_bindings(scope=scope, binding_statements=binding_statements)
        self._attach_instance_binding(scope=scope, function_node=node)

        self.scopes.append(scope)
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            for statement in node.body:
                self.visit(statement)
        self.scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def _register_local_bindings(self, scope: LexicalScope, binding_statements: list[ast.stmt]) -> None:
        binding_counts: dict[str, int] = {}
        candidate_functions: dict[str, FunctionSymbol] = {}
        candidate_imports: dict[str, str] = {}
        for statement in binding_statements:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                binding_counts[statement.name] = binding_counts.get(statement.name, 0) + 1
                candidate_functions[statement.name] = build_function_symbol(
                    function_node=statement,
                    qualified_name=statement.name,
                    is_defined_in_class_body=False,
                    safe_decorators=self.configuration.safe_decorators,
                )
            elif isinstance(statement, ast.ClassDef):
                binding_counts[statement.name] = binding_counts.get(statement.name, 0) + 1
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    binding_counts[local_name] = binding_counts.get(local_name, 0) + 1
                    candidate_imports[local_name] = alias.name if alias.asname else local_name
            elif isinstance(statement, ast.ImportFrom):
                source_module = resolve_relative_module(
                    current_module_name=self.module_summary.module_name,
                    current_is_package=self.module_summary.is_package,
                    level=statement.level,
                    imported_module=statement.module,
                )
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    local_name = alias.asname or alias.name
                    binding_counts[local_name] = binding_counts.get(local_name, 0) + 1
                    if source_module is not None:
                        candidate_imports[local_name] = f"{source_module}.{alias.name}"
        for name, count in binding_counts.items():
            if count == 1 and name not in scope.bound_names:
                if name in candidate_functions:
                    scope.local_functions[name] = candidate_functions[name]
                    continue
                if name in candidate_imports:
                    scope.local_imports[name] = candidate_imports[name]
                    continue
            scope.bound_names.add(name)

    def _attach_instance_binding(
        self,
        scope: LexicalScope,
        function_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> None:
        enclosing_scope = self.scopes[-1]
        if (
            isinstance(function_node, ast.Lambda)
            or enclosing_scope.kind != "class"
            or enclosing_scope.class_symbol is None
        ):
            return
        method_symbol = enclosing_scope.class_symbol.methods.get(function_node.name)
        decorator_names = {
            dotted_name_of(expression=item.func if isinstance(item, ast.Call) else item)
            for item in function_node.decorator_list
        }
        is_static = (
            method_symbol.method_kind == "static"
            if method_symbol is not None
            else bool({"staticmethod"} & {str(name).rsplit(".")[-1] for name in decorator_names})
        )
        positional_parameters = [*function_node.args.posonlyargs, *function_node.args.args]
        if is_static or not positional_parameters:
            return
        is_class_method = method_symbol is not None and method_symbol.method_kind == "class"
        if method_symbol is None and any(
            str(name).rsplit(".")[-1] == "classmethod" for name in decorator_names
        ):
            is_class_method = True
        scope.instance_parameter_name = positional_parameters[0].arg
        scope.instance_reference = InstanceReference(
            class_symbol=enclosing_scope.class_symbol,
            mode="class" if is_class_method else "instance",
        )

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        if node.generators:
            self.visit(node.generators[0].iter)  # evaluated in the enclosing scope
        scope = LexicalScope(kind="comprehension")
        for generator in node.generators:
            scope.bound_names.update(stored_names_in_same_scope(node=generator.target))
        self.scopes.append(scope)
        for index, generator in enumerate(node.generators):
            if index > 0:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scopes.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    # ------------------------------------------------------------------ name resolution

    def resolve_name(self, name: str) -> NameReference:
        innermost_index = len(self.scopes) - 1
        for scope_index in range(innermost_index, 0, -1):
            scope = self.scopes[scope_index]
            if scope.kind == "class" and scope_index != innermost_index:
                continue  # class bodies are not enclosing scopes for nested functions
            if name in scope.global_names:
                break
            if scope.instance_parameter_name == name and scope.instance_reference is not None:
                return scope.instance_reference
            if name in scope.local_functions:
                return scope.local_functions[name]
            if name in scope.local_imports:
                return self.resolver.resolve_dotted_name(dotted_name=scope.local_imports[name])
            if name in scope.bound_names:
                return UNRESOLVED
        resolved = self.resolver.name_in_module(module_summary=self.module_summary, name=name)
        return UNRESOLVED if resolved is None else resolved

    def _enclosing_method_reference(self) -> InstanceReference | None:
        for scope in reversed(self.scopes):
            if scope.instance_reference is not None:
                return scope.instance_reference
        return None

    def _member(self, reference: NameReference, attribute_name: str) -> tuple[NameReference, AccessMode]:
        if isinstance(reference, InstanceReference):
            member = self.resolver.find_attribute_in_class_hierarchy(
                class_symbol=reference.class_symbol,
                attribute_name=attribute_name,
                skip_first=reference.skip_own_class,
            )
            access_mode: AccessMode = "via_instance" if reference.mode == "instance" else "via_class"
            return (UNRESOLVED if member is None else member), access_mode
        if isinstance(reference, UnresolvedReference) or reference is None:
            return UNRESOLVED, "direct"
        if isinstance(reference, ClassSymbol):
            member = self.resolver.member_of(container=reference, attribute_name=attribute_name)
            return (UNRESOLVED if member is None else member), "via_class"
        member = self.resolver.member_of(container=reference, attribute_name=attribute_name)
        return (UNRESOLVED if member is None else member), "direct"

    def resolve_callee(self, callee: ast.expr) -> tuple[NameReference, AccessMode]:
        if isinstance(callee, ast.Name):
            return self.resolve_name(name=callee.id), "direct"
        if not isinstance(callee, ast.Attribute):
            return UNRESOLVED, "direct"
        attribute_names: list[str] = []
        base: ast.expr = callee
        while isinstance(base, ast.Attribute):
            attribute_names.append(base.attr)
            base = base.value
        attribute_names.reverse()

        reference: NameReference
        match base:
            case ast.Name():
                reference = self.resolve_name(name=base.id)
            case ast.Call() if (
                isinstance(base.func, ast.Name)
                and base.func.id == "super"
                and not base.args
                and isinstance(self.resolve_name(name="super"), ExternalSymbol)
            ):
                method_reference = self._enclosing_method_reference()
                if method_reference is None:
                    return UNRESOLVED, "direct"
                reference = InstanceReference(
                    class_symbol=method_reference.class_symbol,
                    mode=method_reference.mode,
                    skip_own_class=True,
                )
            case ast.Call():
                # `ProjectClass(...).method()`: a known constructor yields an instance of that class.
                constructed, _ = self.resolve_callee(callee=base.func)
                if (
                    not isinstance(constructed, ClassSymbol)
                    or self.resolver.constructor_signature(class_symbol=constructed) is None
                ):
                    return UNRESOLVED, "direct"
                reference = InstanceReference(class_symbol=constructed, mode="instance")
            case _:
                return UNRESOLVED, "direct"

        access_mode: AccessMode = "direct"
        for index, attribute_name in enumerate(attribute_names):
            if isinstance(reference, InstanceReference) and index > 0:
                return UNRESOLVED, "direct"
            reference, access_mode = self._member(reference=reference, attribute_name=attribute_name)
        return reference, access_mode

    # ------------------------------------------------------------------ calls

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        if any(isinstance(argument, ast.Starred) for argument in node.args) or not node.args:
            return
        reference, access_mode = self.resolve_callee(callee=node.func)

        function: FunctionSymbol | None = None
        if isinstance(reference, FunctionSymbol):
            function = reference if reference.is_signature_trustworthy else None
        elif isinstance(reference, ClassSymbol):
            function = self.resolver.constructor_signature(class_symbol=reference)
            access_mode = "via_instance"  # __init__'s `self` is supplied implicitly

        if function is not None:
            if self.check_project_calls:
                self._check_project_call(call=node, function=function, access_mode=access_mode)
            return
        if self.check_external_calls:
            external_name = reference.dotted_name if isinstance(reference, ExternalSymbol) else None
            self._check_external_call(call=node, external_name=external_name)

    def _check_project_call(self, call: ast.Call, function: FunctionSymbol, access_mode: AccessMode) -> None:
        binding = binding_for_access(function=function, access_mode=access_mode)
        positional_only_count, positional_names = effective_positional_parameters(
            function=function, binding=binding
        )
        if len(call.args) > len(positional_names):
            return  # spills into *args (or is a TypeError): keywords are not possible here
        keywords_already_passed = {keyword.arg for keyword in call.keywords if keyword.arg}
        first_convertible_index = max(
            positional_only_count, self.configuration.allowed_leading_positional_arguments
        )
        plans: list[KeywordInsertionPlan] = []
        for argument_index in range(first_convertible_index, len(call.args)):
            keyword_name = positional_names[argument_index]
            if keyword_name in keywords_already_passed:
                return  # already a TypeError; leave it to the type checker
            plans.append(
                KeywordInsertionPlan(call=call, argument_index=argument_index, keyword_name=keyword_name)
            )
        if not plans:
            return
        suggestion = ", ".join(f"{plan.keyword_name}=" for plan in plans)
        self.findings.append(
            RuleFinding(
                code=PROJECT_CALL_CODE,
                node=call,
                message=(
                    f"Call to `{function.qualified_name}` passes {len(plans)} argument(s) "
                    f"positionally; use keywords: {suggestion}"
                ),
                fix_plans=tuple(plans),
            )
        )

    def _is_ignored_call(self, names: list[str]) -> bool:
        return any(regex.fullmatch(name) for regex in self.ignored_call_regexes for name in names if name)

    def _check_external_call(self, call: ast.Call, external_name: str | None) -> None:
        maximum = self.configuration.max_positional_arguments_for_external_calls
        if len(call.args) <= maximum:
            return
        written_name = dotted_name_of(expression=call.func)
        display_name = written_name or external_name
        if display_name is None:
            display_name = ast.unparse(call.func)
            if len(display_name) > 60:
                display_name = display_name[:57] + "..."
        names_to_match = [name for name in (written_name, external_name) if name]
        if not names_to_match and isinstance(call.func, ast.Attribute):
            names_to_match = [f"<expr>.{call.func.attr}"]
        if self._is_ignored_call(names=names_to_match):
            return

        suggestion = ""
        if external_name is not None and self.configuration.inspect_standard_library:
            signature = lookup_external_signature(dotted_name=external_name)
            if signature is None and external_name.startswith("builtins."):
                return  # C builtins without a text signature (getattr, max, ...) are positional-only
            if signature is not None:
                if len(call.args) > len(signature.positional_parameter_names):
                    return  # spills into *args: keywords are not possible
                keyword_capable_names = signature.positional_parameter_names[
                    signature.positional_only_count : len(call.args)
                ]
                if not keyword_capable_names:
                    return  # every positional slot is positional-only
                suggestion = "; use keywords: " + ", ".join(f"{name}=" for name in keyword_capable_names)
        self.findings.append(
            RuleFinding(
                code=EXTERNAL_CALL_CODE,
                node=call,
                message=(
                    f"Call to `{display_name}` passes {len(call.args)} positional arguments "
                    f"(max {maximum}){suggestion}"
                ),
            )
        )


def check_module_tree(tree: ast.Module, context: RuleContext) -> list[RuleFinding]:
    checker = KeywordArgumentChecker(
        module_summary=context.module_summary,
        resolver=context.resolver,
        configuration=context.configuration,
    )
    checker.visit(tree)
    return checker.findings


__all__ = [
    "EXTERNAL_CALL_CODE",
    "PROJECT_CALL_CODE",
    "RULE_DESCRIPTIONS",
    "KeywordArgumentChecker",
    "check_module_tree",
    "is_safe_decorator",
]
