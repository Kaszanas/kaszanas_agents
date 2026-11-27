# kaszanas_agents

[![CI](https://github.com/Kaszanas/kaszanas_agents/actions/workflows/ci.yml/badge.svg)](https://github.com/Kaszanas/kaszanas_agents/actions/workflows/ci.yml)
[![License: AGPL v3+](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Coding standards that every agent follows, enforced by a checker so that nothing slips
through.**

Coding agents (Claude Code, GitHub Copilot, Cursor, Codex and others) write code faster
than anyone can review it, so house rules have to be both *taught* and *checked*. This
repository packages each standard twice:

- as an **Agent Skill** that agents load when relevant, so they write compliant code
  from the start;
- as a **fast checker with autofix** that runs after every agent edit, in pre-commit and
  in CI, so violations never land.

The standards so far:

**1. In Python, call functions defined in your own codebase with keyword arguments.**

```python
create_user("ada", "ada@example.com", True)  # before
create_user(name="ada", email="ada@example.com", is_admin=True)  # after `kaszanas-lint check --fix`
```

**2. In Python 3.10+, dispatch on one value with `match`, and on value→constant tables
with a dict.**

```python
if command == "start":
    ...  # before
elif command in ("stop", "halt"):
    ...
elif command is None:
    ...

match command:  # after `kaszanas-lint check --fix`
    case "start":
        ...
    case "stop" | "halt":
        ...
    case None:
        ...
```

**3. In source code (any language), write section headings as one plain comment, never
as ASCII-art banners or divider lines.** Makefiles, shell and config files are exempt.

```python
# ------------------------------------------------------------------ helpers  (before)
# Helpers:                                                       (after `--fix`)
```

## What's inside

| Component                                                                    | What it does                                                                                                           |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| [`python-keyword-arguments`](skills/python-keyword-arguments/SKILL.md) skill | Teaches agents the keyword-argument rule, its exceptions, and how to verify with the checker                           |
| [`python-match-statements`](skills/python-match-statements/SKILL.md) skill   | Teaches when `match` helps, when a dict is better, and the capture-pattern pitfall                                     |
| [`source-section-comments`](skills/source-section-comments/SKILL.md) skill   | Any language: plain `# Title:` section comments instead of banners; scoped to source files only                        |
| `kaszanas-lint`                                                              | Indexes your whole project, reports [the rules below](#rules), and rewrites what it can prove safe (`--fix`)           |
| Claude Code plugin `kaszanas-python`                                         | All skills, plus a hook that lints each `.py` file right after Claude edits it and feeds findings back                 |
| [`rules/`](rules/)                                                           | The same rules as Cursor `.mdc`, Copilot `.instructions.md` and `AGENTS.md` sections, for agents without skill support |
| pre-commit / prek hooks                                                      | `kaszanas-lint` and `kaszanas-lint-fix`, for any repository                                                            |

## Quick start

First install the checker (once per machine):

```bash
uv tool install git+https://github.com/Kaszanas/kaszanas_agents
```

Then add the standard to your agent:

| I use…                                                   | Run                                                                        |
| -------------------------------------------------------- | -------------------------------------------------------------------------- |
| **Claude Code**                                          | `/plugin install kaszanas-python --marketplace Kaszanas/kaszanas_agents`   |
| **Cursor, Copilot, Codex, Gemini CLI, …** (Agent Skills) | `npx skills add Kaszanas/kaszanas_agents`                                  |
| **An agent without skill support**                       | Copy the files from [`rules/`](rules/), or see [Rules files](#rules-files) |
| **pre-commit / prek**                                    | Add the hook from [pre-commit and prek](#pre-commit-and-prek)              |
| **Just the linter**                                      | `kaszanas-lint check --fix .`                                              |

## Installation in detail

### The checker

```bash
uv tool install git+https://github.com/Kaszanas/kaszanas_agents    # `kaszanas-lint` on PATH
uvx --from git+https://github.com/Kaszanas/kaszanas_agents kaszanas-lint check .   # no install
```

```bash
kaszanas-lint check .                         # report
kaszanas-lint check --fix .                   # rewrite fixable findings ([*]) in place
kaszanas-lint check --diff src/               # preview the fixes as a unified diff
kaszanas-lint check --select MAT001 .         # run only some rules
kaszanas-lint check --output-format github    # GitHub Actions annotations (also: json)
kaszanas-lint check --statistics .            # timings
kaszanas-lint rules                           # list the rules
kaszanas-lint rule MAT001                     # full documentation of one rule
```

Exit codes: `0` means clean, `1` means findings remain (or `--diff` would change files),
and `2` means a configuration error.

### Claude Code plugin

```text
/plugin install kaszanas-python --marketplace Kaszanas/kaszanas_agents
```

On older Claude Code versions, run `/plugin marketplace add Kaszanas/kaszanas_agents`,
then `/plugin install kaszanas-python@kaszanas-agents`.

The plugin contains the skill and a `PostToolUse` hook. After every `Edit`/`Write` to a
`.py` file, the hook runs `kaszanas-lint` on that file. Findings go back to Claude,
which fixes them in the same turn.

- The hook uses `kaszanas-lint` from PATH. Failing that, it uses
  `uvx --from "$KASZANAS_LINT_SPEC"`.
- If neither is available, it stays silent. It never blocks your session.

### Agent Skills (`npx skills`)

```bash
npx skills add Kaszanas/kaszanas_agents          # interactive: choose agents and scope
npx skills add Kaszanas/kaszanas_agents -g -y    # every detected agent, user-wide
```

For Claude Code, use **either** the plugin **or** `npx skills`. Installing both loads
the skill twice.

### Rules files

For agents that read rule files instead of skills, clone this repository and run:

```bash
uv run scripts/build_agent_files.py --install-into ../your-project
uv run scripts/build_agent_files.py --install-into ../your-project --agents cursor,copilot
```

This writes:

- `.cursor/rules/<skill>.mdc` for each skill;
- `.github/instructions/<skill>.instructions.md` (Copilot, applies to `**/*.py`);
- marked sections in `AGENTS.md`. Running the command again updates those sections in
  place and leaves the rest of the file alone.

### pre-commit and prek

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/Kaszanas/kaszanas_agents
    rev: v0.1.0 # a tag or commit SHA
    hooks:
      - id: kaszanas-lint # report only; use kaszanas-lint-fix to rewrite on commit
```

If you also use ruff, declare the external code prefixes so that ruff keeps
`# noqa: KWA001` / `# noqa: MAT001` comments:

```toml
[tool.ruff.lint]
external = ["KWA", "MAT"]
```

## Rules

Each rule has its own page with rationale, examples, fix safety, exceptions and
configuration. Read them here, or offline with `kaszanas-lint rule <CODE>`.

| Code                                                     | Standard          | Reports                                                                                   | Fix                     |
| -------------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------- | ----------------------- |
| [KWA001](src/kaszanas_lint/rule_documentation/KWA001.md) | keyword arguments | Positional arguments in a call to a function, method or class **defined in your project** | Yes                     |
| [KWA002](src/kaszanas_lint/rule_documentation/KWA002.md) | keyword arguments | More than `max-positional-external` (2) positional arguments in a call to external code   | No (names the keywords) |
| [MAT001](src/kaszanas_lint/rule_documentation/MAT001.md) | match statements  | An if/elif chain of 3+ branches testing one subject (Python 3.10+)                        | When provably equal     |
| [MAT002](src/kaszanas_lint/rule_documentation/MAT002.md) | match statements  | An if/elif chain that maps literal values to constants: use a dict lookup                 | No                      |
| [CMT001](src/kaszanas_lint/rule_documentation/CMT001.md) | section comments  | A decorative banner around a section title, such as `# ---- helpers ----`                 | Yes (`# Helpers:`)      |
| [CMT002](src/kaszanas_lint/rule_documentation/CMT002.md) | section comments  | A bare divider comment line, such as `# ----------`                                       | Yes (deletes it)        |

```text
app.py:12:5: KWA001 [*] Call to `shop.orders.create_order` passes 2 argument(s) positionally; use keywords: customer=, items=
app.py:20:1: KWA002 Call to `re.sub` passes 3 positional arguments (max 2); use keywords: pattern=, repl=, string=
app.py:31:5: MAT001 [*] Use a `match` statement on `command` instead of this 4-branch if/elif chain
app.py:48:5: MAT002 This 4-branch if/elif chain maps `code` to constants; use a dict lookup instead (constant time, unlike if/elif or match)
```

Suppress a single finding with a ruff-style comment, such as `# noqa: KWA001` on the
call or `# noqa: MAT001` on the `if` line, or with a bare `# noqa`.

### Configuration

All settings go in `pyproject.toml` under `[tool.kaszanas-lint]`, and all are optional.

| Key                                                    | Default                                         | Meaning                                                        |
| ------------------------------------------------------ | ----------------------------------------------- | -------------------------------------------------------------- |
| `src`                                                  | `["src", "."]` if `src/` exists, else `["."]`   | Import roots used to name modules                              |
| `exclude` / `extend-exclude`                           | `[]`                                            | Glob patterns to skip (`.venv`, `build`, … are always skipped) |
| `select` / `ignore`                                    | all rules                                       | Rule codes to enable or disable                                |
| `per-file-ignores`                                     | `{}`                                            | e.g. `{ "tests/**" = ["KWA002"] }`                             |
| `allow-positional`                                     | `0`                                             | Leading positional arguments tolerated in project calls        |
| `max-positional-external`                              | `2`                                             | KWA002 threshold                                               |
| `ignore-call-patterns` / `extend-ignore-call-patterns` | logging and string helpers                      | Regexes, full-matched against the dotted call name             |
| `safe-decorators` / `extend-safe-decorators`           | `functools.wraps`, `cache`, `contextmanager`, … | Decorators known to keep the signature                         |
| `inspect-stdlib`                                       | `true`                                          | Read builtin/stdlib signatures for KWA002                      |
| `min-match-branches`                                   | `3`                                             | Shortest if/elif chain (counting `else`) for MAT001/MAT002     |
| `target-version`                                       | from `requires-python`, then ruff               | Lowest supported Python, e.g. `"py310"`; MAT001 needs 3.10+    |

Command-line flags override the file: `--select`, `--ignore`, `--allow-positional`,
`--max-positional-external`, `--min-match-branches`, `--no-cache` and `--config`.

## How it works

Ruff can't express this rule. It has no plugin API, and the rule needs signatures from
other files ([ruff#3269](https://github.com/astral-sh/ruff/issues/3269),
[discussion #8137](https://github.com/astral-sh/ruff/discussions/8137)). So
`kaszanas-lint` runs alongside ruff:

1. **Index.** Every Python file in the project is parsed, in parallel and cached by
   modification time. Each one is reduced to its function signatures, classes and
   imports.
2. **Resolve.** Each call in the files you check is traced to its definition through:
   - imports and re-exports;
   - `self`, `cls` and `super()`;
   - class hierarchies (C3 MRO);
   - `__init__`, dataclasses and `NamedTuple`.
3. **Stay conservative.** Unknown decorators, re-bound names, custom metaclasses and
   external base classes make a call "not certainly project-defined". Such calls only
   fall under the report-only KWA002 rule, never under the autofix.

Your code is **never imported or executed**. Only builtins and standard-library modules
are inspected, for their signatures. See
[rationale.md](skills/python-keyword-arguments/references/rationale.md) for the full
resolution rules.

## Performance

Measured on Django (2,943 files, 532k lines), on Windows:

| Scenario                                                               | Time           |
| ---------------------------------------------------------------------- | -------------- |
| Whole repository, warm cache (wall clock, including `uv run` start-up) | 2.8 s          |
| One file, warm cache (typical hook / pre-commit run)                   | 0.58 s         |
| Indexing, cold / warm cache                                            | 2.3 s / 0.33 s |

`--fix` rewrote 34,590 call sites in 1,575 Django files. Every rewritten file is
re-parsed before it's written.

## FAQ

**Is `--fix` safe?** It only rewrites calls whose target and parameter names are known
for certain. It handles nested calls, comments, parenthesized arguments, generators and
walrus expressions. It keeps CRLF line endings and BOMs. If a fixed file doesn't parse,
that file is left untouched.

**Does it replace ruff?** No. It adds rules ruff doesn't have. Run both.

**Is `match` faster than if/elif?** No. CPython compiles both to the same sequence of
compare-and-jump instructions, and class patterns are slower than `isinstance` chains.
MAT001 is about readability. For speed on value→constant tables, use a dict (MAT002).
See [the benchmarks on real codebases](docs/benchmarks/match-statements.md).

**Can I adopt it gradually?** Yes:

- start with `select = ["KWA001"]` and run `kaszanas-lint check --fix .` once;
- add `per-file-ignores` for legacy directories;
- tighten the settings later.

**Does it work on Windows?** Yes. It's developed and benchmarked on Windows, and CI
covers Linux, macOS and Windows on Python 3.10 and 3.14.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development
setup (uv, prek, commitizen), the repository layout, and how to add a new standard.
Every contributor signs the [Contributor License Agreement](CLA.md); a bot guides you
through it on your first pull request.

## License

[GNU Affero General Public License v3.0 or later](LICENSE).

- If you modify this project, or build it into your own software, and distribute the
  result, you must release your source under the same licence.
- The same applies if you let others use a modified version over a network, for example
  in a hosted agent platform.
