"""`kaszanas-lint` command line."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import sys
from pathlib import Path

from kaszanas_lint import __version__
from kaszanas_lint.configuration import ALL_RULE_CODES, find_project_root, load_configuration
from kaszanas_lint.diagnostic_reporting import (
    display_path,
    format_github,
    format_json,
    format_summary,
    format_text,
)
from kaszanas_lint.linting_session import run_lint
from kaszanas_lint.rules.rule_registry import RULE_DESCRIPTIONS, RULE_DOCUMENTATION_URL, rule_documentation
from kaszanas_lint.source_text import write_python_source

SUBCOMMANDS = ("check", "rules", "rule")


def _comma_separated_codes(raw_value: str) -> frozenset[str]:
    codes = frozenset(item.strip().upper() for item in raw_value.split(",") if item.strip())
    unknown = codes - set(ALL_RULE_CODES)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown rule code(s): {', '.join(sorted(unknown))}")
    return codes


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kaszanas-lint",
        description="Opinionated Python lint rules (keyword arguments, match statements) with autofix.",
    )
    parser.add_argument("--version", action="version", version=f"kaszanas-lint {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="lint files (default command)")
    check_parser.add_argument("paths", nargs="*", type=Path, default=[Path(".")])
    check_parser.add_argument("--fix", action="store_true", help="apply fixes in place")
    check_parser.add_argument(
        "--diff", action="store_true", help="print fixes as a unified diff instead of writing"
    )
    check_parser.add_argument("--output-format", choices=("text", "json", "github"), default="text")
    check_parser.add_argument("--select", type=_comma_separated_codes, default=None)
    check_parser.add_argument("--ignore", type=_comma_separated_codes, default=None)
    check_parser.add_argument("--allow-positional", type=int, default=None, metavar="N")
    check_parser.add_argument("--max-positional-external", type=int, default=None, metavar="N")
    check_parser.add_argument(
        "--min-match-branches", type=int, default=None, metavar="N", help="MAT001/MAT002 chain length"
    )
    check_parser.add_argument("--config", type=Path, default=None, help="explicit pyproject.toml")
    check_parser.add_argument("--no-cache", action="store_true")
    check_parser.add_argument("--jobs", type=int, default=None, help="worker processes")
    check_parser.add_argument("--exit-zero", action="store_true")
    check_parser.add_argument("--quiet", action="store_true", help="only print diagnostics")
    check_parser.add_argument("--statistics", action="store_true", help="print timings")

    subparsers.add_parser("rules", help="list the available rules")
    rule_parser = subparsers.add_parser("rule", help="show the documentation page of one rule")
    rule_parser.add_argument("code", type=str.upper, choices=ALL_RULE_CODES, metavar="CODE")
    return parser


def _normalize_arguments(arguments: list[str]) -> list[str]:
    if not arguments:
        return ["check"]
    if arguments[0] in SUBCOMMANDS or arguments[0] in ("-h", "--help", "--version"):
        return arguments
    return ["check", *arguments]


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def run_check(parsed: argparse.Namespace) -> int:
    working_directory = Path.cwd()
    start_directory = parsed.config.parent if parsed.config else working_directory
    project_root = find_project_root(start_directory=start_directory)
    try:
        configuration = load_configuration(project_root=project_root, explicit_config_path=parsed.config)
    except (ValueError, OSError) as configuration_error:
        print(f"kaszanas-lint: configuration error: {configuration_error}", file=sys.stderr)
        return 2

    selected = configuration.selected_rule_codes
    if parsed.select is not None:
        selected = parsed.select
    if parsed.ignore is not None:
        selected = selected - parsed.ignore
    configuration = configuration.with_overrides(
        selected_rule_codes=frozenset(selected),
        allowed_leading_positional_arguments=parsed.allow_positional,
        max_positional_arguments_for_external_calls=parsed.max_positional_external,
        minimum_match_branches=parsed.min_match_branches,
        cache_enabled=False if parsed.no_cache else None,
    )

    run_result = run_lint(
        configuration=configuration,
        requested_paths=parsed.paths,
        apply_fixes=parsed.fix or parsed.diff,
        jobs=parsed.jobs,
    )

    fixed_count = 0
    for file_result in run_result.file_results:
        if file_result.fixed_source is None or file_result.original_source is None:
            continue
        fixed_count += file_result.fixed_violation_count
        if parsed.diff:
            shown_path = display_path(
                relative_path=file_result.relative_path,
                project_root=project_root,
                working_directory=working_directory,
            )
            sys.stdout.writelines(
                difflib.unified_diff(
                    file_result.original_source.splitlines(keepends=True),
                    file_result.fixed_source.splitlines(keepends=True),
                    fromfile=f"a/{shown_path}",
                    tofile=f"b/{shown_path}",
                )
            )
        else:
            write_python_source(
                file_path=file_result.absolute_path,
                text=file_result.fixed_source,
                encoding=file_result.encoding,
            )

    remaining = run_result.diagnostics
    match parsed.output_format:
        case "json":
            print(format_json(diagnostics=remaining, project_root=project_root))
        case "github":
            rendered = format_github(
                diagnostics=remaining, project_root=project_root, working_directory=working_directory
            )
            if rendered:
                print(rendered)
        case _:
            rendered = format_text(
                diagnostics=remaining, project_root=project_root, working_directory=working_directory
            )
            if rendered:
                print(rendered)
            if not parsed.quiet:
                print(format_summary(remaining=remaining, fixed_count=fixed_count, would_fix=parsed.diff))

    for error in run_result.errors:
        print(f"kaszanas-lint: skipped {error}", file=sys.stderr)
    if parsed.statistics:
        print(
            f"indexed {run_result.indexed_file_count} files in {run_result.index_seconds:.3f}s, "
            f"checked {len(run_result.file_results)} files in {run_result.check_seconds:.3f}s",
            file=sys.stderr,
        )

    if parsed.exit_zero:
        return 0
    if remaining or (parsed.diff and fixed_count):
        return 1
    return 0


def run_rules() -> int:
    for code, description in RULE_DESCRIPTIONS.items():
        print(f"{code}  {description}")
    print(
        "\nDetails: kaszanas-lint rule <CODE>  (online: " + RULE_DOCUMENTATION_URL.format(code="<CODE>") + ")"
    )
    return 0


def run_rule(code: str) -> int:
    print(rule_documentation(code=code))
    return 0


def main(arguments: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_argument_parser()
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    parsed = parser.parse_args(_normalize_arguments(arguments=raw_arguments))
    if parsed.command == "rules":
        return run_rules()
    if parsed.command == "rule":
        return run_rule(code=parsed.code)
    return run_check(parsed=parsed)


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
