# Contributing

Contributions are welcome and greatly appreciated! Every little bit helps, and credit
will always be given.

## Types of contributions

### Report bugs

Open an issue and include:

- your operating system, Python version and `kaszanas-lint --version`;
- the smallest code snippet that reproduces the problem;
- what you expected and what happened instead (paste the full diagnostic line).

A **false positive** (a KWA001 finding, or a fix, on a call that isn't really
project-defined) is the most valuable bug report you can file.

### Fix bugs and implement features

Look through the GitHub issues. Anything tagged `bug` or `enhancement` together with
`help wanted` is open to whoever wants to work on it. For larger changes, please open an
issue first so we can agree on the approach.

### Propose a new coding standard

New standards live as skills (see [Adding a standard](#adding-a-standard)). In the
issue, explain:

- the rule, with "wrong" and "right" examples;
- why it helps humans *and* coding agents;
- whether it can be checked mechanically (and how), or is guidance only.

### Write documentation

Improvements to the README, the skills or the rationale documents are always welcome.

## Getting started

The code style standards are defined in
[`.pre-commit-config.yaml`](.pre-commit-config.yaml) and are run with
[prek](https://github.com/j178/prek). prek is a fast, drop-in replacement for
pre-commit, and the same config also works with classic `pre-commit`.

1. Fork and clone the repository.

2. Install the project and the development tools with [uv](https://docs.astral.sh/uv/):

   ```console
   uv sync
   ```

3. Install the git hooks (pre-commit and commit-msg):

   ```console
   uv run prek install
   ```

4. Create a branch for your change:

   ```console
   git checkout -b name-of-your-bugfix-or-feature
   ```

5. Make your changes, then run every check the CI runs:

   ```console
   uv run prek run --all-files
   ```

   The hooks are:

   - `pre-commit-hooks` basics;
   - `ruff check --fix` and `ruff format`;
   - `mdformat` (wrap 88, numbered lists);
   - `actionlint`;
   - **this repository's own `kaszanas-lint`**: the code must follow the rule it
     enforces;
   - regeneration of the agent files from `skills/`;
   - `pytest`.

6. Commit using [Conventional Commits](https://www.conventionalcommits.org/). commitizen
   can write the message for you, and the commit-msg hook checks it:

   ```console
   uv run cz commit
   ```

7. Push your branch and open a pull request.

## Repository layout

```text
skills/<name>/SKILL.md          source of truth for each standard (edit these)
skills/<name>/references/       longer explanations loaded on demand by agents
rules/                          GENERATED: Cursor, Copilot and AGENTS.md versions of each skill
plugins/kaszanas-python/        Claude Code plugin: GENERATED skill copy + PostToolUse hook
.claude-plugin/marketplace.json Claude Code marketplace listing
src/kaszanas_lint/              the checker
scripts/build_agent_files.py    regenerates rules/ and the plugin skill copies
.pre-commit-hooks.yaml          hooks other repositories consume
tests/                          pytest suite (fixture projects are built in tmp_path)
```

Never edit files under `rules/` or `plugins/kaszanas-python/skills/` by hand. The
`agent-files-sync` hook regenerates them, and `tests/test_agent_files_are_in_sync.py`
fails when they're stale.

### How kaszanas-lint works

1. **Index pass**: `file_discovery`, `project_index`, `module_summarization`.

   Every project file is parsed, in parallel and cached in `.kaszanas_lint_cache/`, into
   a small picklable `ModuleSummary`. The summary holds function signatures, classes
   (bases, methods, dataclass fields), imports and names that are re-bound.

2. **Check pass**: `linting_session` runs every family in `rules/rule_registry.py`.

   Each family has a `check_module_tree(tree, context)` function that returns
   `RuleFinding`s (see `rules/rule_framework.py`):

   - `rules/keyword_arguments` (KWA) resolves every call to a project definition,
     through imports, `self`/`cls`/`super()`, the C3 MRO and constructors, or to
     external code;
   - `rules/match_statements` (MAT) finds if/elif chains that test one subject;
   - `rules/banner_comments` (CMT) works on the token stream instead of the AST, because
     comments aren't AST nodes. Such families report a `SourceLocation` (byte columns,
     like `ast`) in place of a node.

3. **Fix**: `automatic_fixes`.

   Each finding carries fix plans that become `TextEdit`s. Non-overlapping findings are
   applied together. Overlapping ones, such as a KWA001 call inside a chain that MAT001
   rewrites, land on the next of up to five passes. Each file is re-parsed before it's
   written.

## Adding a standard

1. Create `skills/<name>/SKILL.md`. It needs YAML frontmatter with `name`, `description`
   (the description is what agents match against, so start it with "Use when …") and
   `license: AGPL-3.0-or-later`. Skills named `python-*` apply to `**/*.py`, and others
   to every file. To narrow that, add a quoted, comma-separated `globs:` value, which
   becomes the Cursor `globs` and the Copilot `applyTo`.

2. Put longer material in `skills/<name>/references/` and link it from the skill.

3. Regenerate the agent files (the hook also does this):

   ```console
   uv run scripts/build_agent_files.py
   ```

4. If the standard can be checked mechanically, add a rule family:

   - a module under `src/kaszanas_lint/rules/` exposing `RULE_DESCRIPTIONS` and
     `check_module_tree(tree, context)`, registered in `rules/rule_registry.py`;
   - its codes in `ALL_RULE_CODES` (`configuration.py`);
   - one documentation page per code in
     `src/kaszanas_lint/rule_documentation/<CODE>.md`, with the sections that
     `tests/test_rule_documentation.py` requires;
   - tests, including behavioural-equivalence checks for any autofix.

## Changing kaszanas-lint

- **Add a test for every behaviour change.** `tests/conftest.py` provides
  `make_project`, which writes a throwaway project, and `lint_project`, which runs the
  linter on it.
- **Stay conservative.** A fix is offered only when it's provably equivalent. Uncertain
  calls fall back to KWA002 (report only), and uncertain chains are reported as MAT001
  without `[*]`. A wrong autofix is worse than a missed finding.
- **Check behaviour, not just syntax.** MAT fixes are tested by running the original and
  the rewritten code over awkward inputs (`assert_equivalent` in
  `tests/test_match_statements_rule.py`). Large changes should also be validated on real
  projects; see
  [docs/benchmarks/match-statements.md](docs/benchmarks/match-statements.md) for the
  regression procedure.
- **Measure performance changes.** Run `kaszanas-lint check --statistics` on a large
  real project (e.g. a shallow clone of Django) and quote the before/after numbers in
  the PR.
- Module and file names are verbose and descriptive (`keyword_arguments.py`, not
  `kwa.py`), and calls to project-defined functions use keyword arguments. The
  `kaszanas-lint` hook enforces the latter.

## Pull request guidelines

Before you submit a pull request, check that:

1. `uv run prek run --all-files` passes locally.
2. New behaviour comes with tests.
3. Documentation (README, skill, rationale) is updated when user-visible behaviour
   changes.
4. The change works on Linux, Windows and macOS, and on Python 3.10 through 3.14. CI
   runs that matrix.

## Contributor License Agreement

This project requires a signed [Contributor License Agreement](CLA.md) from every
contributor. When you open your first pull request, the CLA Assistant bot will ask you
to sign it by commenting:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

By contributing, you agree that your contributions will be licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE), in addition to the rights
granted in the CLA.
