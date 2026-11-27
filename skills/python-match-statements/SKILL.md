---
name: python-match-statements
description: Use when writing, editing, refactoring or reviewing Python 3.10+ code that branches on one value (if/elif chains on the same variable, isinstance dispatch, command or status handling). Prefer `match` statements for readability, use dict lookups for value-to-constant tables, avoid the capture-pattern pitfall, and verify with kaszanas-lint (MAT001/MAT002).
license: AGPL-3.0-or-later
---

# Python: `match` statements for dispatch on one subject

## The rule

When a chain of three or more `if`/`elif`/`else` branches tests **the same subject**,
write it as a `match` statement (Python 3.10+):

```python
# Wrong
if command == "start":
    run()
elif command in ("stop", "halt"):
    shutdown()
elif command is None:
    usage()
else:
    error(command)

# Right
match command:
    case "start":
        run()
    case "stop" | "halt":
        shutdown()
    case None:
        usage()
    case _:
        error(command)
```

When every branch only maps a value to a constant, use a **dict** instead. It's the one
form that's genuinely faster (constant time):

```python
STATUS_TEXT = {200: "OK", 404: "Not Found", 500: "Server Error"}
text = STATUS_TEXT.get(code, "Unknown")
```

## Do not claim `match` is faster

CPython compiles `match` into the same sequence of compare-and-jump instructions as
if/elif. Literal patterns run at about the same speed, and class patterns
(`case int():`) are slower than `isinstance` chains. Choose `match` for **readability**,
and a dict for **speed**.

## Pitfalls you must avoid

- **Bare names are captures, not comparisons.** `case RED:` matches *everything* and
  rebinds `RED`. To compare with a constant, use a dotted name (`case Color.RED:`) or a
  literal.
- **`None`/`True`/`False` patterns compare by identity.** `case True:` is `x is True`,
  not `x == True` (`1 == True`). Translate only `x is None` style tests into them.
- **The subject is evaluated once.** Don't convert chains whose "subject" is a call
  (`if read() == 1: … elif read() == 2:`).
- **Class patterns need classes.** `case TYPES():` raises when `TYPES` is a tuple.
  `isinstance(x, (A, B))` becomes `case A() | B():`.
- Use guards for extra conditions: `elif x == 1 and ready:` becomes `case 1 if ready:`.

## Verify with the checker

```bash
kaszanas-lint check --fix path/to/changed_file.py      # rewrites MAT001 where provably safe
kaszanas-lint rule MAT001                              # full rule documentation
```

| Code   | Meaning                                            | Fix                              |
| ------ | -------------------------------------------------- | -------------------------------- |
| MAT001 | if/elif chain on one subject should be `match`     | `--fix` when provably equivalent |
| MAT002 | if/elif chain maps values to constants: use a dict | By hand                          |

MAT001 only applies when the project targets Python 3.10 or later (`requires-python`).
If a MAT001 finding has no `[*]`, rewrite it by hand following the pitfalls above.

For measurements and the bytecode explanation, see
[references/benchmarks.md](references/benchmarks.md).
