---
name: source-section-comments
description: Use when writing, editing or reviewing source code in any language (Python, TypeScript, JavaScript, Rust, Go, Java, C/C++, C#, SQL and others) and you are about to add a section comment. Never write ASCII-art banners or divider lines such as `# ------ helpers ------` or `// ==========`; write one plain `# Helpers:` style comment instead. Does not apply to Makefiles, shell or config files. Verify Python with kaszanas-lint (CMT001/CMT002).
license: AGPL-3.0-or-later
globs: "**/*.py,**/*.pyi,**/*.js,**/*.jsx,**/*.mjs,**/*.cjs,**/*.ts,**/*.tsx,**/*.rs,**/*.go,**/*.java,**/*.kt,**/*.c,**/*.h,**/*.cpp,**/*.hpp,**/*.cs,**/*.swift,**/*.rb,**/*.php,**/*.lua,**/*.sql"
---

# Source code: plain section comments, no banners

## The rule

In **source code**, never decorate a comment with runs of repeated characters, and never
write divider lines. When a group of definitions needs a heading, write a single plain
comment: the title, capitalized, followed by a colon.

```python
# Wrong
# ------------------------------------------------------------------ helpers
# ==================== Public API ====================
# ------------------------------------------------------------
# Some helpers
# ------------------------------------------------------------

# Right
# Helpers:
# Public API:
# Some helpers:
```

The same applies whatever the comment syntax is:

```typescript
// Wrong
// ===================== validation =====================
/* ************************************************** */

// Right
// Validation:
```

```sql
-- Wrong
-- ---------------------------------------------------------- reporting views

-- Right
-- Reporting views:
```

Often the heading isn't needed at all. Prefer one of these:

- a docstring, or a doc comment on the item itself;
- splitting a module that has grown "sections" into several modules;
- blank lines between groups.

## When banners are fine

Leave decoration alone where it's conventional and not source code:

- Makefiles (`Makefile`, `*.mk`), shell profiles and dotfiles;
- CMake files, Dockerfiles and CI or config files (YAML, TOML, INI, `.gitignore`);
- generated files, licence headers and vendored code;
- ASCII diagrams and tables that explain something (`+-----+-----+`).

Don't rewrite existing banners in code you aren't otherwise touching, unless asked to.

## Verify with the checker (Python)

```bash
kaszanas-lint check --fix path/to/changed_file.py      # rewrites banners, deletes dividers
kaszanas-lint rule CMT001                              # full rule documentation
```

| Code   | Meaning                                                   | Fix                       |
| ------ | --------------------------------------------------------- | ------------------------- |
| CMT001 | Banner around a section title (or a title boxed by lines) | `--fix` writes `# Title:` |
| CMT002 | Bare divider line (`# ----------`)                        | `--fix` deletes the line  |

For other languages, apply the rule by hand: the checker only reads Python.
