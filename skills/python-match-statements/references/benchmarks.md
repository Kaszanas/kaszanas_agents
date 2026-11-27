# Is `match` faster than if/elif? (No.)

Measured with CPython 3.14.3, in nanoseconds per call. Reproduce with
`uv run scripts/benchmark_dispatch.py`.

| Value dispatch, 7 branches | First | Last | No match |
| -------------------------- | ----- | ---- | -------- |
| if/elif `==`               | 17.7  | 49.1 | 52.5     |
| `match` literal patterns   | 19.9  | 54.0 | 52.2     |
| dict `.get`                | 26.4  | 26.2 | 28.2     |

| Class dispatch, 4 classes | First | Last |
| ------------------------- | ----- | ---- |
| `isinstance` chain        | 30.8  | 68.3 |
| `match` class patterns    | 52.9  | 92.1 |

- CPython compiles `match` into the same compare-and-jump sequence as if/elif. There's
  no jump table, so the cost grows with the branch position in both forms.
- Class patterns add pattern-matching bookkeeping on top of the `isinstance` check.
- Only a dict lookup costs the same whatever the key.

So: use `match` because it **reads** better (one subject, alternatives with `|`,
destructuring), and use a dict when a value→constant table is on a hot path.

The full write-up, including regression checks on Django, CPython and pandas, is in the
repository at `docs/benchmarks/match-statements.md`.
