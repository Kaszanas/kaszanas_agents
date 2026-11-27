"""Time real-world if/elif chains against the `match` statements kaszanas-lint would write.

    uv run scripts/benchmark_real_world_chains.py PATH [PATH ...] --python 3.10,3.14 --output out.json

For every MAT001 chain found under the given paths, a standalone harness is generated:

- the tests are kept verbatim (so `x in (1, 2)` stays a tuple membership test), with the subject
  renamed to `subject`, dotted constants replaced by sentinel attributes `V.c<N>`, and
  `isinstance` classes replaced by local classes `C<N>`;
- each body becomes `return <branch index>`;
- the `match` version is produced by kaszanas-lint's own MAT001 fixer.

Chains whose tests reference anything else (guards on other variables, function calls) are
skipped and counted. Each harness gets one input that hits every branch plus one that matches
nothing. Both versions are checked for equal results on every input, then timed under each
requested Python version with `uv run --python X`. Only the generated harness code is executed,
never the analysed project.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from kaszanas_lint.automatic_fixes import apply_fix_groups
from kaszanas_lint.configuration import DEFAULT_SAFE_DECORATORS, LintConfiguration
from kaszanas_lint.linting_session import analyze_source
from kaszanas_lint.module_summarization import dotted_name_of, summarize_module_source
from kaszanas_lint.rules.match_statements import MatchStatementChecker
from kaszanas_lint.rules.rule_framework import RuleContext
from kaszanas_lint.source_text import SourceText, read_python_source
from kaszanas_lint.symbol_resolution import SymbolResolver

BUILTIN_CLASS_SAMPLES = {
    "int": "7",
    "float": "1.5",
    "complex": "2j",
    "str": "'s'",
    "bytes": "b's'",
    "bool": "True",
    "list": "[]",
    "tuple": "()",
    "dict": "{}",
    "set": "set()",
    "frozenset": "frozenset()",
    "bytearray": "bytearray()",
    "type": "int",
    "object": "object()",
}
ALLOWED_FREE_NAMES = {"subject", "isinstance", "V", "True", "False", "None", *BUILTIN_CLASS_SAMPLES}
CALLS_PER_INPUT = 20_000
REPEATS = 3


@dataclass
class Harness:
    identifier: str
    kinds: list[str]
    branch_count: int
    if_source: str
    inputs: list[str]
    match_source: str = ""


@dataclass
class ExtractionReport:
    harnesses: list[Harness] = field(default_factory=list)
    chains_found: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class TestRewriter(ast.NodeTransformer):
    """Rename the subject and replace constants/classes with harness-local stand-ins."""

    def __init__(self, subject_key: str, value_names: dict[str, str], class_names: dict[str, str]) -> None:
        self.subject_key = subject_key
        self.value_names = value_names
        self.class_names = class_names

    def visit(self, node: ast.AST) -> ast.AST:
        if isinstance(node, ast.expr) and ast.dump(node) == self.subject_key:
            return ast.Name(id="subject", ctx=ast.Load())
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
        ):
            subject = self.visit(node=node.args[0])
            classes = node.args[1].elts if isinstance(node.args[1], ast.Tuple) else [node.args[1]]
            replaced = [self.class_stand_in(node=item) for item in classes]
            class_argument = replaced[0] if len(replaced) == 1 else ast.Tuple(elts=replaced, ctx=ast.Load())
            return ast.Call(func=node.func, args=[subject, class_argument], keywords=[])
        if isinstance(node, ast.Attribute) and dotted_name_of(expression=node) is not None:
            dotted = dotted_name_of(expression=node)
            stand_in = self.value_names.setdefault(dotted, f"c{len(self.value_names)}")
            return ast.Attribute(value=ast.Name(id="V", ctx=ast.Load()), attr=stand_in, ctx=ast.Load())
        return self.generic_visit(node)

    def class_stand_in(self, node: ast.expr) -> ast.expr:
        dotted = dotted_name_of(expression=node)
        if dotted in BUILTIN_CLASS_SAMPLES:
            return ast.Name(id=dotted, ctx=ast.Load())
        stand_in = self.class_names.setdefault(str(dotted), f"C{len(self.class_names)}")
        return ast.Name(id=stand_in, ctx=ast.Load())


def hitting_input(test: ast.expr) -> str | None:
    """Source text of a value that satisfies the (rewritten) test."""
    if isinstance(test, ast.BoolOp):
        return hitting_input(test=test.values[0])
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        operator, left, right = test.ops[0], test.left, test.comparators[0]
        other = right if isinstance(left, ast.Name) and left.id == "subject" else left
        if isinstance(operator, ast.In) and isinstance(right, ast.Tuple | ast.List) and right.elts:
            return ast.unparse(right.elts[0])
        if isinstance(operator, ast.Eq | ast.Is):
            return ast.unparse(other)
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.func.id == "isinstance":
        class_argument = test.args[1]
        first = class_argument.elts[0] if isinstance(class_argument, ast.Tuple) else class_argument
        name = ast.unparse(first)
        return BUILTIN_CLASS_SAMPLES.get(name, f"{name}()")
    return None


def free_names(node: ast.AST) -> set[str]:
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}


def extract_harnesses(paths: list[Path], excluded_directories: frozenset[str]) -> ExtractionReport:
    report = ExtractionReport()
    configuration = LintConfiguration(
        project_root=Path.cwd(),
        source_roots=(Path.cwd(),),
        selected_rule_codes=frozenset({"MAT001"}),
        target_python_version=(3, 10),
    )
    for root in paths:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        files = [item for item in files if not excluded_directories & set(item.parts[:-1])]
        for file_path in files:
            try:
                source = read_python_source(file_path=file_path).text
                tree = ast.parse(source)
            except (SyntaxError, ValueError, UnicodeDecodeError, OSError):
                continue
            summary = summarize_module_source(
                source=source,
                module_name="analysed",
                relative_path=file_path.name,
                is_package=False,
                safe_decorators=DEFAULT_SAFE_DECORATORS,
            )
            checker = MatchStatementChecker(
                tree=tree,
                context=RuleContext(
                    module_summary=summary,
                    resolver=SymbolResolver(module_summaries={"analysed": summary}),
                    configuration=configuration,
                    source_text=SourceText(text=source),
                ),
            )
            continuation_ids: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.If) or id(node) in continuation_ids:
                    continue
                chain = checker.collect_chain(head=node)
                continuation_ids.update(id(branch) for branch in chain.branches[1:])
                if chain.branch_count < configuration.minimum_match_branches:
                    continue
                analysis = checker.analyze_chain(chain=chain)
                if analysis is None:
                    continue
                report.chains_found += 1
                harness = build_harness(
                    identifier=f"{file_path}:{node.lineno}", chain=chain, analysis=analysis, report=report
                )
                if harness is not None:
                    report.harnesses.append(harness)
    return report


def build_harness(identifier, chain, analysis, report: ExtractionReport) -> Harness | None:
    if any(candidate.guard_text for candidate in analysis.branch_patterns):
        report.skip(reason="guard")
        return None
    value_names: dict[str, str] = {}
    class_names: dict[str, str] = {}
    rewriter = TestRewriter(
        subject_key=ast.dump(analysis.subject), value_names=value_names, class_names=class_names
    )
    tests = [
        rewriter.visit(ast.parse(ast.unparse(branch.test), mode="eval").body) for branch in chain.branches
    ]
    allowed = ALLOWED_FREE_NAMES | set(class_names.values())
    if any(free_names(node=test) - allowed for test in tests):
        report.skip(reason="references other names")
        return None
    inputs = [hitting_input(test=test) for test in tests]
    if any(item is None for item in inputs):
        report.skip(reason="no hitting input")
        return None
    lines = [f"class C{index}: pass" for index in range(len(class_names))]
    lines.append("class V:")
    lines += [f"    {stand_in} = object()" for stand_in in value_names.values()] or ["    pass"]
    lines.append("def dispatch(subject):")
    for index, test in enumerate(tests):
        keyword = "if" if index == 0 else "elif"
        lines += [f"    {keyword} {ast.unparse(test)}:", f"        return {index}"]
    if chain.else_body:
        lines += ["    else:", f"        return {len(tests)}"]
    lines.append("    return -1")
    kinds = sorted(set().union(*(candidate.pattern_kinds for candidate in analysis.branch_patterns)))
    return Harness(
        identifier=identifier,
        kinds=kinds,
        branch_count=chain.branch_count,
        if_source="\n".join(lines) + "\n",
        inputs=[*inputs, "object()"],
    )


def rewrite_to_match(source: str) -> str | None:
    summary = summarize_module_source(
        source=source,
        module_name="harness",
        relative_path="harness.py",
        is_package=False,
        safe_decorators=DEFAULT_SAFE_DECORATORS,
    )
    configuration = LintConfiguration(
        project_root=Path.cwd(),
        source_roots=(Path.cwd(),),
        selected_rule_codes=frozenset({"MAT001"}),
        target_python_version=(3, 10),
    )
    _, fix_groups = analyze_source(
        source=source,
        relative_path="harness.py",
        module_summary=summary,
        resolver=SymbolResolver(module_summaries={"harness": summary}),
        configuration=configuration,
    )
    fixed, applied = apply_fix_groups(source=source, fix_groups=fix_groups)
    return fixed if applied else None


TIMING_DRIVER = """
import json, sys, time
results = []
for identifier, if_source, match_source, input_sources in HARNESSES:
    if_namespace, match_namespace = {}, {}
    exec(if_source, if_namespace)
    exec(match_source, match_namespace)
    if_inputs = [eval(text, if_namespace) for text in input_sources]
    match_inputs = [eval(text, match_namespace) for text in input_sources]
    equivalent = all(
        if_namespace["dispatch"](a) == match_namespace["dispatch"](b) for a, b in zip(if_inputs, match_inputs)
    )
    def measure(function, inputs):
        best = float("inf")
        for _ in range(REPEATS):
            started = time.perf_counter()
            for _ in range(CALLS_PER_INPUT):
                for value in inputs:
                    function(value)
            best = min(best, time.perf_counter() - started)
        return best / (CALLS_PER_INPUT * len(inputs)) * 1e9
    results.append({
        "id": identifier,
        "equivalent": equivalent,
        "if_ns": measure(if_namespace["dispatch"], if_inputs),
        "match_ns": measure(match_namespace["dispatch"], match_inputs),
    })
json.dump({"python": sys.version.split()[0], "results": results}, sys.stdout)
"""


def run_timings(harnesses: list[Harness], python_version: str) -> dict:
    payload = [(item.identifier, item.if_source, item.match_source, item.inputs) for item in harnesses]
    driver = (
        f"HARNESSES = {payload!r}\nCALLS_PER_INPUT = {CALLS_PER_INPUT}\nREPEATS = {REPEATS}\n" + TIMING_DRIVER
    )
    with tempfile.TemporaryDirectory() as directory:
        driver_path = Path(directory) / "timing_driver.py"
        driver_path.write_text(driver, encoding="utf-8")
        completed = subprocess.run(
            ["uv", "run", "--no-project", "--python", python_version, "python", "-I", str(driver_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    return json.loads(completed.stdout)


def chain_category(kinds: list[str]) -> str:
    core = set(kinds) - {"alternatives"}
    if core == {"literal"}:
        return "literal"
    if core <= {"literal", "value"}:
        return "value (dotted constants)"
    if core == {"class"}:
        return "class (isinstance)"
    return "mixed"


def summarize(timing: dict, harnesses_by_id: dict[str, Harness]) -> list[str]:
    rows = []
    by_category: dict[str, list[float]] = {}
    for result in timing["results"]:
        ratio = result["match_ns"] / result["if_ns"]
        by_category.setdefault(chain_category(kinds=harnesses_by_id[result["id"]].kinds), []).append(ratio)
        by_category.setdefault("all", []).append(ratio)
    for category, ratios in sorted(by_category.items(), key=lambda item: item[0] != "all"):
        ratios.sort()
        rows.append(
            f"| {timing['python']} | {category} | {len(ratios)} | {statistics.median(ratios):.2f} | "
            f"{ratios[len(ratios) // 10]:.2f} | {ratios[(len(ratios) * 9) // 10]:.2f} | "
            f"{sum(1 for item in ratios if item < 1) / len(ratios):.0%} |"
        )
    return rows


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--python", default="3.14", help="comma-separated interpreter versions")
    parser.add_argument("--output", type=Path, help="write raw results as JSON")
    parser.add_argument(
        "--exclude-directory",
        action="append",
        default=["test", "tests", "idle_test"],
        help="directory names to skip (default: test, tests, idle_test)",
    )
    parsed = parser.parse_args(arguments)

    report = extract_harnesses(paths=parsed.paths, excluded_directories=frozenset(parsed.exclude_directory))
    for harness in report.harnesses:
        harness.match_source = rewrite_to_match(source=harness.if_source) or ""
    usable = [harness for harness in report.harnesses if harness.match_source]
    print(
        f"chains found: {report.chains_found}; benchmarked: {len(usable)}; "
        f"skipped: {dict(report.skipped)}; fixer declined: {len(report.harnesses) - len(usable)}"
    )
    harnesses_by_id = {harness.identifier: harness for harness in usable}
    raw: dict[str, object] = {"chains_found": report.chains_found, "skipped": report.skipped, "timings": []}
    print("\n| Python | Chains | n | median match/if | p10 | p90 | match faster |")
    print("|---|---|---|---|---|---|---|")
    for python_version in (item.strip() for item in parsed.python.split(",") if item.strip()):
        timing = run_timings(harnesses=usable, python_version=python_version)
        mismatches = [item["id"] for item in timing["results"] if not item["equivalent"]]
        if mismatches:
            print(f"BEHAVIOUR MISMATCH on Python {timing['python']}: {mismatches}", file=sys.stderr)
        print("\n".join(summarize(timing=timing, harnesses_by_id=harnesses_by_id)))
        raw["timings"].append(timing)
    if parsed.output:
        parsed.output.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    builtins_reference = builtins  # keep the import for readers checking ALLOWED_FREE_NAMES
    sys.exit(main())
