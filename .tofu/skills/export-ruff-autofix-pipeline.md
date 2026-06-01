---
name: export-ruff-autofix-pipeline
description: Export pipeline copies ruff.toml for CI lint config; auto-fix handles F-rules; E-rules suppressed by design
enabled: true
tags: [ci, ruff, export, lint]
created: 2026-04-03T02:07:08Z
updated: 2026-04-04T12:33:55Z
---

# Export Ruff Auto-Fix Pipeline

## What
`export.py` runs `_run_ruff_autofix(dest)` after copying all files but before
`_verify_opensource()` and `_git_push()`. This ensures exported code always
passes CI lint, even if the source has minor style drift.

## ruff.toml (project root)
The project has a proper `ruff.toml` that is copied to exports. It configures:
- **Selected rules**: F (pyflakes) + E (pycodestyle errors)
- **Ignored rules** (deliberate style):
  - E501 (line length), E701/E702 (compact one-liners), E731 (lambda assign),
    E741 (ambiguous var names), E402 (lazy imports in `__init__.py`)
- **Per-file ignores**: `tests/*` ignores F401 and F841

## What auto-fix handles
- **F401**: Unused imports (removed)
- **F841**: Unused local variables (removed with `--unsafe-fixes`)
- **F601**: Duplicate dict keys
- **F811**: Redefinition of unused names

## pyproject.toml
Contains correct Tofu project metadata + pytest markers (unit, api, visual, slow).
No ruff config in pyproject.toml — it's all in `ruff.toml`.

## CI compatibility
CI runs `ruff check lib/ routes/ tests/` which auto-discovers `ruff.toml`.
The export copies `ruff.toml` to dest, so exported code uses the same rules.
`.ruff_cache` is in `ALWAYS_EXCLUDE_DIRS`.

## Adding new ruff rules
1. Fix existing violations in the source (`ruff check --fix --select RULE lib/ routes/ tests/`)
2. The export auto-fix catches future drift automatically
3. If a rule isn't auto-fixable, add it to the ignore list in `ruff.toml`

