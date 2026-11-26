<!-- kaszanas:python-keyword-arguments:start -->

<!-- Generated from skills/python-keyword-arguments/SKILL.md by scripts/build_agent_files.py -->

## Python: keyword arguments at call sites

### The rule

When code calls a function, method or class **defined in this codebase**, pass every
argument that *can* be a keyword *as* a keyword.

```python
# Wrong
user = create_user("ada", "ada@example.com", True)
report.render(template, context)

# Right
user = create_user(name="ada", email="ada@example.com", is_admin=True)
report.render(template=template, context=context)
```

Leave these positional, because Python doesn't allow keywords there:

- parameters before `/` (positional-only);
- arguments that land in `*args`;
- calls that unpack `*iterable`;
- the explicit `self` in `ClassName.method(instance, ...)`.

For calls to third-party or standard-library code, keep at most **two** positional
arguments, and use keywords beyond that (`re.sub(pattern=p, repl=r, string=s)`).
Logging-style calls (`logger.info("%s", value)`) are exempt.

When you write a new function with several parameters of the same type, consider making
them keyword-only with `*` (`def move(*, x: int, y: int)`) so that callers can't mix
them up.

### Verify with the checker

`kaszanas-lint` indexes every `def` and `class` in the project, resolves each call
through imports, `self`/`cls`/`super()` and inheritance, and knows the real parameter
names. After editing Python files, run it on the files you changed:

```bash
kaszanas-lint check --fix path/to/changed_file.py other_file.py   # rewrite KWA001 in place
kaszanas-lint check path/to/changed_file.py                        # report only
```

If it isn't on PATH:
`uvx --from git+https://github.com/Kaszanas/kaszanas_agents kaszanas-lint check --fix <files>`.

| Code   | Meaning                                                                     | Fix                  |
| ------ | --------------------------------------------------------------------------- | -------------------- |
| KWA001 | Call to a project-defined callable passes keyword-capable args positionally | `--fix` rewrites it  |
| KWA002 | External/unresolvable call passes more than 2 positional args               | Add keywords by hand |

KWA001 output names the exact keywords to use (`use keywords: width=, height=`). Fix
every finding in code you touched. Don't silence findings to get a clean run.

### Suppressing a finding

Only when a keyword call would genuinely hurt readability (e.g. a mathematical helper
`clamp(value, 0, 1)` in hot numeric code), add a targeted comment and say why:

```python
clamp(value, 0, 1)  # noqa: KWA001 -- reads like the formula
```

### Project configuration (`pyproject.toml`)

```toml
[tool.kaszanas-lint]
allow-positional = 0              # leading positional args tolerated for project calls
max-positional-external = 2       # KWA002 threshold
per-file-ignores = { "tests/**" = ["KWA002"] }
extend-ignore-call-patterns = ['(.*\.)?assert\w*']   # regexes on dotted call names
extend-safe-decorators = ["my_project.utils.traced"] # decorators that keep the signature
```

<!-- kaszanas:python-keyword-arguments:end -->
