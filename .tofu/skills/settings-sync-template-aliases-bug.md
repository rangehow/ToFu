---
name: settings-sync-template-aliases-bug
description: _syncFromTemplate originally ignored aliases; now does additive merge + warns about user-only aliases not in template
enabled: true
tags: [settings, providers, frontend, convention]
created: 2026-05-11T02:29:26Z
updated: 2026-05-11T02:29:26Z
---

# Settings → "Sync Template" button: alias handling

`_syncFromTemplate` in `static/js/settings.js` (called from the 📋 同步模板 button)
originally only reconciled `capabilities` and `cost` for models that already existed
in the user's provider. **`aliases` were completely ignored** — neither pulled from
the template nor flagged when the user had aliases the template didn't have.

This caused dead user-added aliases (e.g. `deepseek-v4-pro-baidu` on the Meituan
sankuai gateway, where that backend doesn't exist) to silently survive sync,
generating endless HTTP 400 "请求格式有误" errors.

## Current behavior (post-fix)

- Template aliases missing from the user's model list are **added** (additive merge).
- User-only aliases (in the user's config but NOT in the template) are **kept** but
  surfaced in the result alert so the user can review and prune dead ones.
- Counted in the result: `aliasesAdded`, `userOnlyAliases`.

## What "Sync Template" still does NOT do

- Does not push user edits back to `static/provider_templates/*.json`. That's
  `_offerTemplateUpdate`, which is only triggered after `_discoverModels` succeeds.
  If a user manually adds an alias / model in Settings, it stays in
  `data/config/server_config.json` only.
- Does not remove user-only aliases automatically (they may be intentional).

## Files
- `static/js/settings.js:_syncFromTemplate` — sync logic
- `static/provider_templates/*.json` — template source of truth (curated)
- `data/config/server_config.json` — user-edited / runtime config

