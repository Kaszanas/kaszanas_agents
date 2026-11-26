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

The first standard: **in Python, call functions defined in your own codebase with
keyword arguments.**

```python
create_user("ada", "ada@example.com", True)  # before
create_user(name="ada", email="ada@example.com", is_admin=True)  # after `kaszanas-lint check --fix`
```

## What's inside

| Component                                                                    | What it does                                                                                                              |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| [`python-keyword-arguments`](skills/python-keyword-arguments/SKILL.md) skill | Teaches agents the rule, its exceptions, and how to verify with the checker                                               |
| `kaszanas-lint`                                                              | Indexes your whole project, flags positional arguments in calls to project-defined callables, and rewrites them (`--fix`) |
| Claude Code plugin `kaszanas-python`                                         | The skill, plus a hook that lints each `.py` file right after Claude edits it and feeds findings back                     |
| [`rules/`](rules/)                                                           | The same rule as a Cursor `.mdc`, Copilot `.instructions.md` and `AGENTS.md` section, for agents without skill support    |
| pre-commit / prek hooks                                                      | `kaszanas-lint` and `kaszanas-lint-fix`, for any repository                                                               |

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
kaszanas-lint check --fix .                   # rewrite KWA001 findings in place
kaszanas-lint check --diff src/               # preview the fixes as a unified diff
kaszanas-lint check --output-format github    # GitHub Actions annotations (also: json)
kaszanas-lint check --statistics .            # timings
kaszanas-lint rules                           # list the rules
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

- `.cursor/rules/python-keyword-arguments.mdc`;
- `.github/instructions/python-keyword-arguments.instructions.md` (Copilot, applies to
  `**/*.py`);
- a marked section in `AGENTS.md`. Running the command again updates that section in
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

If you also use ruff, declare the external code prefix so that ruff keeps
`# noqa: KWA001` comments:

```toml
[tool.ruff.lint]
external = ["KWA"]
```

## The keyword-argument rule

| Code       | When                                                                                                                | Fix                                                               |
| ---------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| **KWA001** | A call to a function, method or class **defined in your project** passes keyword-capable arguments positionally     | `--fix` rewrites it                                               |
| **KWA002** | A call to external or unresolvable code passes more than `max-positional-external` (default 2) positional arguments | Add keywords by hand. The message names them for stdlib functions |

```text
app.py:12:5: KWA001 [*] Call to `shop.orders.create_order` passes 2 argument(s) positionally; use keywords: customer=, items=
app.py:20:1: KWA002 Call to `re.sub` passes 3 positional arguments (max 2); use keywords: pattern=, repl=, string=
```

**Positional is still allowed wherever keywords are impossible:**

- positional-only parameters (before `/`);
- arguments that land in `*args`;
- calls that unpack `*iterable`;
- the explicit `self` in `ClassName.method(instance, …)`.

For builtins and standard-library functions, the real signatures are used, so
`isinstance(x, T)` and `os.path.join(a, b, c)` aren't flagged. Logging-style calls
(`logger.info("%s", x)`) are ignored by default.

**Suppress a single line** with ruff-style comments: `# noqa: KWA001` or a bare
`# noqa`.

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

Command-line flags override the file: `--select`, `--ignore`, `--allow-positional`,
`--max-positional-external`, `--no-cache` and `--config`.

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

**Does it replace ruff?** No. It adds the one rule ruff can't express. Run both.

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
