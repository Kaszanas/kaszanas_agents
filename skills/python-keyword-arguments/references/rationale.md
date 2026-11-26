# Why keyword arguments, and how kaszanas-lint decides

## Why

- **Call sites document themselves.** You can read `send(to=user, retries=3)` without
  opening the definition.
- **Swaps fail loudly.** `transfer(source, target)` with the arguments swapped still
  runs, but `transfer(source=target, target=source)` is visibly wrong in review.
- **Refactors are safer.** Reordering or inserting parameters doesn't silently rebind
  existing calls.
- **Agents make fewer mistakes.** An LLM editing a call it can't fully see is far less
  likely to misplace a keyword argument than a positional one.

## Why not ruff?

Ruff has no plugin API. The call-site rule has been requested in
[astral-sh/ruff#3269](https://github.com/astral-sh/ruff/issues/3269) and
[discussion #8137](https://github.com/astral-sh/ruff/discussions/8137), and is waiting
on multi-file analysis. `PLR0917` only checks *definitions*. kaszanas-lint runs next to
ruff; add `lint.external = ["KWA"]` to ruff's config so ruff keeps `# noqa: KWA001`
comments.

## How calls are resolved

1. **Index pass.** Every project file is parsed once, in parallel and cached by mtime.
   The pass records function signatures, classes (bases, methods, dataclass fields,
   decorators) and imports.
2. **Check pass.** Each requested file is walked with lexical scopes, and each call is
   resolved:
   - local names, nested `def`s and function-local imports;
   - module globals, `from x import y as z`, `import a.b as c`, relative and star
     imports, re-exports through `__init__.py`;
   - `self.m()`, `cls.m()`, `super().m()`, `ClassName.m()` and `ClassName(...).m()`,
     using the C3 MRO;
   - constructors: `__init__`, `@dataclass` (including `KW_ONLY`, `field(init=False)`,
     `kw_only=True` and inherited fields) and `NamedTuple`.

## When a call is *not* treated as project-defined (falls back to KWA002)

These are deliberately conservative choices. A false positive with an autofix is worse
than a miss.

- Names re-bound at module level (`f = wrap(f)`), conditionally defined with different
  signatures, or imported from alternative places in `try/except`.
- Names shadowed in the local scope (parameters, assignments, loop and comprehension
  variables).
- Functions with unknown decorators. A decorator may change the signature.
  Signature-preserving decorators are listed in `safe-decorators`:
  `functools.wraps`/`cache`/`lru_cache`, `contextmanager`, `abstractmethod`, `override`
  and others.
- `@overload`ed functions, properties, classes with a custom metaclass or `__new__`, and
  unknown class decorators.
- Methods that an *external* base class could override first in the MRO (e.g. a Django
  model method inherited from a third-party base).

## KWA002 and the standard library

For builtins and stdlib modules, kaszanas-lint reads the real signature with `inspect`.
It only imports modules listed in `sys.stdlib_module_names`, never project or
third-party code. With the signature it can:

- skip positional-only and `*args` parameters (`isinstance(x, T)`,
  `os.path.join(a, b, c)`);
- name the keywords to use (`re.sub` → `pattern=, repl=, string=`).

## What the autofix does

It inserts `name=` before each positional argument, using the token stream:

- redundant parentheses `f((a))` become `f(name=(a))`;
- a lone generator `f(x for x in y)` becomes `f(name=(x for x in y))`;
- a walrus `f(x := g())` becomes `f(name=(x := g()))`;
- comments and line endings (CRLF, BOM) are preserved.

Every fixed file is re-parsed before it's written. If the fixed file doesn't parse, that
file is left untouched.
