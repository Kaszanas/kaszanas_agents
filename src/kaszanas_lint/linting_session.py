"""Pass 2 orchestration: check (and optionally fix) the requested files against the project index."""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path

from kaszanas_lint.automatic_fixes import apply_fix_groups
from kaszanas_lint.configuration import LintConfiguration
from kaszanas_lint.diagnostics import Diagnostic
from kaszanas_lint.file_discovery import (
    discover_project_python_files,
    is_excluded,
    relative_posix_path,
)
from kaszanas_lint.parallel_execution import map_possibly_in_parallel
from kaszanas_lint.project_index import SourceFile, build_project_index, build_source_files
from kaszanas_lint.rules.rule_framework import FixPlan, RuleContext
from kaszanas_lint.rules.rule_registry import run_all_rules
from kaszanas_lint.source_text import SourceText, read_python_source
from kaszanas_lint.suppression_comments import (
    codes_ignored_for_path,
    is_suppressed_by_noqa_comment,
)
from kaszanas_lint.symbol_resolution import SymbolResolver
from kaszanas_lint.symbols import ModuleSummary


@dataclass(slots=True)
class FileCheckResult:
    relative_path: str
    absolute_path: Path
    diagnostics: list[Diagnostic]
    original_source: str | None = None
    fixed_source: str | None = None
    encoding: str = "utf-8"
    fixed_violation_count: int = 0
    error: str | None = None


@dataclass(slots=True)
class LintRunResult:
    file_results: list[FileCheckResult] = field(default_factory=list)
    indexed_file_count: int = 0
    index_seconds: float = 0.0
    check_seconds: float = 0.0

    @property
    def diagnostics(self) -> list[Diagnostic]:
        collected = [item for result in self.file_results for item in result.diagnostics]
        return sorted(collected, key=Diagnostic.sort_key)

    @property
    def errors(self) -> list[str]:
        return [f"{result.relative_path}: {result.error}" for result in self.file_results if result.error]


def analyze_source(
    source: str,
    relative_path: str,
    module_summary: ModuleSummary,
    resolver: SymbolResolver,
    configuration: LintConfiguration,
) -> tuple[list[Diagnostic], list[tuple[FixPlan, ...]]]:
    """Diagnostics plus one fix group per fixable, unsuppressed finding."""
    tree = ast.parse(source, filename=relative_path)
    source_text = SourceText(text=source)
    findings = run_all_rules(
        tree=tree,
        context=RuleContext(
            module_summary=module_summary,
            resolver=resolver,
            configuration=configuration,
            source_text=source_text,
        ),
    )
    ignored_codes = codes_ignored_for_path(
        relative_posix_path=relative_path, per_file_ignores=configuration.per_file_ignores
    )
    diagnostics: list[Diagnostic] = []
    fix_groups: list[tuple[FixPlan, ...]] = []
    for finding in findings:
        node = finding.node
        if finding.code in ignored_codes:
            continue
        end_line = node.end_lineno or node.lineno
        # Calls accept noqa comments on their first or last line; statements (an if/elif chain) on the
        # line where they start, so a comment deep inside the chain can't silence it.
        noqa_lines = {node.lineno, end_line} if isinstance(node, ast.expr) else {node.lineno}
        if any(
            is_suppressed_by_noqa_comment(line_text=source_text.lines[line_number - 1], code=finding.code)
            for line_number in noqa_lines
        ):
            continue
        diagnostics.append(
            Diagnostic(
                relative_path=relative_path,
                line=node.lineno,
                column=source_text.character_column(line_number=node.lineno, byte_column=node.col_offset) + 1,
                end_line=end_line,
                end_column=source_text.character_column(
                    line_number=end_line, byte_column=node.end_col_offset or 0
                )
                + 1,
                code=finding.code,
                message=finding.message,
                is_fixable=bool(finding.fix_plans),
            )
        )
        if finding.fix_plans:
            fix_groups.append(finding.fix_plans)
    return diagnostics, fix_groups


MAXIMUM_FIX_PASSES = 5


_worker_state: dict[str, object] = {}


def initialize_checking_worker(
    module_summaries: dict[str, ModuleSummary], configuration: LintConfiguration, apply_fixes: bool
) -> None:
    _worker_state["summaries"] = module_summaries
    _worker_state["resolver"] = SymbolResolver(module_summaries=module_summaries)
    _worker_state["configuration"] = configuration
    _worker_state["apply_fixes"] = apply_fixes


def check_source_file(source_file: SourceFile) -> FileCheckResult:
    summaries: dict[str, ModuleSummary] = _worker_state["summaries"]  # type: ignore[assignment]
    resolver: SymbolResolver = _worker_state["resolver"]  # type: ignore[assignment]
    configuration: LintConfiguration = _worker_state["configuration"]  # type: ignore[assignment]
    apply_fixes = bool(_worker_state["apply_fixes"])

    result = FileCheckResult(
        relative_path=source_file.relative_path,
        absolute_path=source_file.absolute_path,
        diagnostics=[],
    )
    module_summary = summaries.get(source_file.module_name)
    if module_summary is None or module_summary.syntax_error:
        result.error = (module_summary.syntax_error if module_summary else None) or "not indexed"
        return result

    def analyze(text: str) -> tuple[list[Diagnostic], list[tuple[FixPlan, ...]]]:
        return analyze_source(
            source=text,
            relative_path=source_file.relative_path,
            module_summary=module_summary,
            resolver=resolver,
            configuration=configuration,
        )

    try:
        decoded = read_python_source(file_path=source_file.absolute_path)
        diagnostics, fix_groups = analyze(text=decoded.text)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError) as analysis_error:
        result.error = str(analysis_error)
        return result

    result.diagnostics = diagnostics
    result.encoding = decoded.encoding
    if not (apply_fixes and fix_groups):
        return result

    # Overlapping fixes (e.g. a KWA001 call inside a chain MAT001 rewrites) land on later passes.
    current_source = decoded.text
    fixable_before = sum(1 for item in diagnostics if item.is_fixable)
    for _ in range(MAXIMUM_FIX_PASSES):
        if not fix_groups:
            break
        fixed_source, applied_groups = apply_fix_groups(source=current_source, fix_groups=fix_groups)
        if fixed_source is None or applied_groups == 0 or fixed_source == current_source:
            break
        current_source = fixed_source
        try:
            diagnostics, fix_groups = analyze(text=current_source)
        except (SyntaxError, ValueError):
            return result  # never write a file we can't re-analyse
    if current_source != decoded.text:
        result.original_source = decoded.text
        result.fixed_source = current_source
        result.fixed_violation_count = max(
            0, fixable_before - sum(1 for item in diagnostics if item.is_fixable)
        )
        result.diagnostics = diagnostics
    return result


def _check_task(source_file: SourceFile) -> FileCheckResult:
    return check_source_file(source_file=source_file)


def select_requested_files(
    requested_paths: list[Path], project_files: list[Path], configuration: LintConfiguration
) -> list[Path]:
    selected: set[Path] = set()
    for requested_path in requested_paths:
        resolved = requested_path.resolve()
        if resolved.is_dir():
            selected.update(file_path for file_path in project_files if file_path.is_relative_to(resolved))
        elif resolved.is_file():
            relative_path = relative_posix_path(file_path=resolved, project_root=configuration.project_root)
            if not is_excluded(relative_path=relative_path, exclude_patterns=configuration.exclude_patterns):
                selected.add(resolved)
    return sorted(selected)


def run_lint(
    configuration: LintConfiguration,
    requested_paths: list[Path],
    apply_fixes: bool,
    jobs: int | None = None,
) -> LintRunResult:
    run_result = LintRunResult()
    index_started = time.perf_counter()
    project_files = discover_project_python_files(
        project_root=configuration.project_root, exclude_patterns=configuration.exclude_patterns
    )
    requested_files = select_requested_files(
        requested_paths=requested_paths, project_files=project_files, configuration=configuration
    )
    all_files = sorted(set(project_files) | set(requested_files))
    source_files = build_source_files(file_paths=all_files, configuration=configuration)
    module_summaries = build_project_index(source_files=source_files, configuration=configuration, jobs=jobs)
    run_result.indexed_file_count = len(source_files)
    run_result.index_seconds = time.perf_counter() - index_started

    check_started = time.perf_counter()
    requested_set = set(requested_files)
    files_to_check = [item for item in source_files if item.absolute_path in requested_set]
    run_result.file_results = map_possibly_in_parallel(
        function=_check_task,
        tasks=files_to_check,
        jobs=jobs,
        initializer=initialize_checking_worker,
        initializer_arguments=(module_summaries, configuration, apply_fixes),
    )
    run_result.check_seconds = time.perf_counter() - check_started
    return run_result
