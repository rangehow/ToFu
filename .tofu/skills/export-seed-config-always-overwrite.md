---
name: export-seed-config-always-overwrite
description: Internal export _seed_internal_config must overwrite dest server_config.json every time, not skip if exists
enabled: true
tags: [export, internal, config, bug-fix]
created: 2026-04-22T11:25:53Z
updated: 2026-04-22T11:25:53Z
---

# Internal Export: Always Re-Seed server_config.json

## Problem
`_seed_internal_config()` in `export.py` used to skip writing if `dest/data/config/server_config.json` already existed. Since re-exports reuse the same destination directory (e.g. `tofu-meituan`), a stale config from the first export would persist forever — new models/providers added to source (e.g. `aws.claude-opus-4.7` in the Meituan provider) were silently dropped on re-export.

Users would then click "Sync Template" in Settings UI, which merges `static/provider_templates/meituan.json` into their provider — giving the illusion that the template file was the source of truth, when in fact the provider list is seeded from `data/config/server_config.json`.

## Fix
`_seed_internal_config()` now always overwrites. The seeded config is a derived artifact of source, not a user-edited file — stale persistence is never desired.

## Key insight
Don't conflate "user-edited config files" with "derived config files". The export destination's `server_config.json` is derived (from source + filters), so it must be regenerated every run. `_purge_runtime_artifacts()` wipes DBs/logs but intentionally leaves config — so the seeder itself must handle re-seeding.

