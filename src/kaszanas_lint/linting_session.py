"""Pass 2 orchestration: check (and optionally fix) the requested files against the project index."""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path

from kaszanas_lint.automatic_fixes import KeywordInsertionPlan, apply_keyword_insertions
from kaszanas_lint.configuration import LintConfiguration
from kaszanas_lint.diagnostics import Diagnostic
from kaszanas_lint.file_discovery import (
    discover_project_python_files,
    is_excluded,
    relative_posix_path,
)
from kaszanas_lint.parallel_execution import map_possibly_in_parallel
from kaszanas_lint.project_index import SourceFile, build_project_index, build_source_files
from kaszanas_lint.rules.keyword_arguments import PROJECT_CALL_CODE, check_module_tree
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
) -> tuple[list[Diagnostic], list[KeywordInsertionPlan]]:
    tree = ast.parse(source, filename=relative_path)
    findings = check_module_tree(
        tree=tree, module_summary=module_summary, resolver=resolver, configuration=configuration
    )
    ignored_codes = codes_ignored_for_path(
        relative_posix_path=relative_path, per_file_ignores=configuration.per_file_ignores
    )
    source_text = SourceText(text=source)
    diagnostics: list[Diagnostic] = []
    plans: list[KeywordInsertionPlan] = []
    for finding in findings:
        call = finding.call
        end_line = call.end_lineno or call.lineno
        if finding.code in ignored_codes:
            continue
        if any(
            is_suppressed_by_noqa_comment(line_text=source_text.lines[line_number - 1], code=finding.code)
            for line_number in {call.lineno, end_line}
        ):
            continue
        diagnostics.append(
            Diagnostic(
                relative_path=relative_path,
                line=call.lineno,
                column=source_text.character_column(line_number=call.lineno, byte_column=call.col_offset) + 1,
                end_line=end_line,
                end_column=source_text.character_column(
                    line_number=end_line, byte_column=call.end_col_offset or 0
                )
                + 1,
                code=finding.code,
                message=finding.message,
                is_fixable=bool(finding.insertion_plans),
            )
        )
        plans.extend(finding.insertion_plans)
    return diagnostics, plans


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
    try:
        decoded = read_python_source(file_path=source_file.absolute_path)
        diagnostics, plans = analyze_source(
            source=decoded.text,
            relative_path=source_file.relative_path,
            module_summary=module_summary,
            resolver=resolver,
            configuration=configuration,
        )
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError) as analysis_error:
        result.error = str(analysis_error)
        return result

    result.diagnostics = diagnostics
    result.encoding = decoded.encoding
    if apply_fixes and plans:
        fixed_source = apply_keyword_insertions(source=decoded.text, plans=plans)
        if fixed_source is not None and fixed_source != decoded.text:
            remaining, _ = analyze_source(
                source=fixed_source,
                relative_path=source_file.relative_path,
                module_summary=module_summary,
                resolver=resolver,
                configuration=configuration,
            )
            count_before = sum(1 for item in diagnostics if item.code == PROJECT_CALL_CODE)
            count_after = sum(1 for item in remaining if item.code == PROJECT_CALL_CODE)
            result.original_source = decoded.text
            result.fixed_source = fixed_source
            result.fixed_violation_count = max(0, count_before - count_after)
            result.diagnostics = remaining
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
