---
name: tofu-versioning-system
description: Tofu versioning: VERSION file → lib/version.py → server/health/settings UI; export only tags+version-prefixes commits when --bump is used
enabled: true
tags: [versioning, export, release]
created: 2026-04-03T04:15:38Z
updated: 2026-04-03T04:21:20Z
---

# Tofu Versioning System

## Architecture

1. **`VERSION`** — Single-line semver file at project root (e.g. `0.5.0`)
2. **`lib/version.py`** — Reads VERSION at import, exposes `__version__`
3. **Server banner** — Shows version in startup log (`🫧 Tofu Server v0.5.0`)
4. **`/api/health`** — Returns `version` field in JSON response
5. **Settings UI** — Fetches from `/api/health` and shows in footer

## Export Integration

- `export.py` reads VERSION file and stamps cache-bust params:
  - `?v=YYYYMMDD[a-z]` → `?v=0.5.0` (in HTML/JS files)
  - `_ICON_V` constant in settings.js → version string

## Versioning Rules — IMPORTANT

**Version tags and version-prefixed commit messages are ONLY created when `--bump` is explicitly used.**

| Command | Commit message | Git tag? |
|---|---|---|
| `--push -m 'fix bug'` | `fix bug` | ❌ No |
| `--push` (no -m) | `update 2026-04-03 04:20` | ❌ No |
| `--push --bump patch -m 'fix bug'` | `v0.5.1: fix bug` | ✅ `v0.5.1` |
| `--push --bump minor -m 'new feat'` | `v0.6.0: new feat` | ✅ `v0.6.0` |

This prevents random version tags from appearing on routine pushes.

## CLI Usage

```bash
# Regular push (NO version tag, NO version prefix)
python3 export.py --mode opensource --push -m 'fix some typos'

# Release push (bumps version, tags, prefixes)
python3 export.py --mode opensource --push --bump patch -m 'bug fixes'
python3 export.py --mode opensource --push --bump minor -m 'new feature'
python3 export.py --mode opensource --push --bump major -m 'breaking change'
```

## When to bump

- **patch**: Bug fixes, documentation, minor tweaks
- **minor**: New features, new provider support, UI improvements  
- **major**: Breaking changes, architecture rewrites

