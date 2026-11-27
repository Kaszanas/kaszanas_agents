# `match` vs if/elif: benchmarks and real-codebase regression checks

This page backs the claims in
[MAT001](../../src/kaszanas_lint/rule_documentation/MAT001.md) and
[MAT002](../../src/kaszanas_lint/rule_documentation/MAT002.md). In short: **`match` is a
readability tool, not a speed-up**. Use a dict when you need constant-time dispatch.

## Micro-benchmark

Reproduce with `uv run scripts/benchmark_dispatch.py`. Add `--python 3.10` (or another
version) to compare interpreters. Measured with CPython 3.14.3 on Windows, in
nanoseconds per call:

| Value dispatch (7 branches) | First branch | Last branch | No match |
| --------------------------- | ------------ | ----------- | -------- |
| if/elif `==`                | 17.7         | 49.1        | 52.5     |
| `match` literal patterns    | 19.9         | 54.0        | 52.2     |
| dict `.get`                 | 26.4         | 26.2        | 28.2     |

| Class dispatch (4 classes) | First branch | Last branch |
| -------------------------- | ------------ | ----------- |
| `isinstance` chain         | 30.8         | 68.3        |
| `match` class patterns     | 52.9         | 92.1        |

Why: CPython compiles `match` into the same sequence of compare-and-jump instructions as
if/elif. There is no jump table (check with `dis.dis`). A class pattern does an
`isinstance` check plus pattern-matching bookkeeping, so it costs more than a bare
`isinstance` call. Only a dict lookup has constant cost.

## Real-codebase regression check

Each project's own test suite was run before and after applying
`kaszanas-lint check --select MAT001 --fix` to its library code. Test directories were
never rewritten, so the tests act as the oracle.

| Codebase                                | MAT001 found (fixable) | MAT002 | Rewrites applied | Tests before                                  | Tests after |
| --------------------------------------- | ---------------------- | ------ | ---------------- | --------------------------------------------- | ----------- |
| Django `f0e7bd9` (`django/`)            | 116 (107)              | 4      | 111 in 72 files  | 20,090 OK, 2,138 skipped, 7 expected failures | identical   |
| CPython 3.14.3 `Lib/` (`v3.14.3` tests) | 174 (159)              | 5      | 162 in ~88 files | 433 modules OK, 3 environment failures¹       | identical   |
| pandas 3.1.1 wheel (`pandas/`)          | 184 (138)              | 3      | 141 in 77 files  | 186,253 passed, 931 failed², 226 errors²      | *pending*   |

"Rewrites applied" can exceed the fixable count, because nested chains become fixable on
a later fix pass, once their enclosing chain has been rewritten.

¹ `test_idle`, `test_shutil` and `test_ssl` fail in the standalone build, before and
after the rewrite. The 492 test modules come from the `v3.14.3` source tag, copied into
a uv-managed CPython 3.14.3.

² These fail because optional dependencies (lxml, pyarrow and others) aren't installed.
The comparison is made test by test, using the JUnit XML reports.

Every rewritten file compiled. About 15 rewrites per codebase were reviewed by hand, for
example Django's admin helpers and GDAL driver, and CPython's `json.encoder`. In all of
them, class patterns resolved to real classes, `x is True` became the identity pattern
`case True:`, and branch order was preserved. Order matters where `bool` and `int`, or
`datetime` and `date`, are both handled.

## Real-world runtime benchmark

*Pending.* `scripts/benchmark_real_world_chains.py` extracts every MAT001 chain from the
unmodified code into a standalone harness:

- the tests are kept verbatim;
- the subject is renamed;
- constants and classes are replaced by local stand-ins;
- each branch body returns its index;
- the `match` version comes from the MAT001 fixer itself.

It checks that both versions return the same result for an input that hits every branch,
plus one input that matches nothing. Then it times them on Python 3.10, 3.12 and 3.14.

Coverage of the first extraction:

- Django: 105 of 119 chains;
- pandas: 156 of 187 chains;
- CPython: 223 of 239 chains.

Chains with guards are skipped, because their guards reference other variables that
can't be stubbed faithfully.

## Reproducing the regression check

1. Clone or install the codebase into a scratch location, with an isolated uv
   environment.
2. Run its test suite and save the results. Use JUnit XML where available.
3. Run `kaszanas-lint check <package dir> --select MAT001 --fix` with test directories
   excluded. For an installed package, point the run at a temporary `pyproject.toml` at
   the `site-packages` root. Remove that file before re-testing, because pytest can pick
   it up as its root directory.
4. Run `python -m compileall` on the rewritten package, then re-run the tests and
   compare the sets of failing tests.
