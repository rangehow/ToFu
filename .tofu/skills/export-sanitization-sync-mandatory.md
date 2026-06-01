---
name: export-sanitization-sync-mandatory
description: MANDATORY: Config is per-project (data/config/), NOT global (~/.chatui/); when adding API keys, credentials, endpoints, domains, data dirs, provider IDs, hardcoded paths, or feature flags — MUST also update export.py sanitization rules; exported copies auto-bootstrap fresh PostgreSQL on free port and disable trading via .env
enabled: true
tags: [mandatory, export, sanitization, secrets, privacy, thinking-format, per-provider, brand-assets]
created: 2026-03-30T06:42:46Z
updated: 2026-04-09T09:09:04Z
---

# Per-Project Config Isolation & Export Sanitization

## Config Location (CHANGED 2026-04-01)

All settings are per-project in `data/config/`, NOT global `~/.chatui/`:

| File | Location | Purpose |
|---|---|---|
| `server_config.json` | `data/config/` | Providers, models, presets, search config |
| `features.json` | `data/config/` | Feature flags (trading_enabled etc.) |
| `mcp_servers.json` | `data/config/` | MCP server configs (contains personal tokens!) |
| `daily_reports/` | `data/config/` | Daily task reports |
| `skills/*.md` | `~/.chatui/` | **Global** — intentionally shared across projects |

### Migration
- `lib/config_dir.py` auto-migrates from `~/.chatui/` on first run (copies once)
- Exported copies have empty `data/config/` → start fresh (no key leakage)
- Multiple copies on same machine are fully isolated

### Key Module: `lib/config_dir.py`
```python
from lib.config_dir import config_path
cfg = config_path('server_config.json')  # → <project>/data/config/server_config.json
```

## Internal Mode: Config Seeding (ADDED 2026-04-09)

Bug fix: `internal` mode excluded entire `data/` dir, so colleagues got no API config → "API not found".

**Solution**: `_seed_internal_config()` in `export.py` auto-seeds `server_config.json`:
- **Copied**: providers (with API keys), proxy_config, proxy_bypass_domains, models, presets, search, model_defaults, hidden_models
- **Stripped**: feishu (personal app_id, workspace), model_limits (personal usage), mcp_servers.json (personal tokens like GitHub PAT)
- **Idempotent**: skips if destination already has server_config.json

Config keys are controlled by `_INTERNAL_CONFIG_KEYS` set in `export.py`.

## Export Sanitization Triggers

You **MUST** update `export.py` when any of the following happens:

| Change | What to update |
|---|---|
| New API key/credential | Add to `_SECRETS` dict |
| New internal endpoint/URL | Add to `_ENDPOINTS` dict |
| New internal domain reference | Add to `_INTERNAL_DOMAIN_LITERALS` |
| New data directory | Add to `ALWAYS_EXCLUDE_DIRS` |
| New scratch/temp file | Add to `ALWAYS_EXCLUDE_FILES/GLOBS` |
| New hardcoded secrets in file | Add file-specific rule in `_sanitize_source_opensource()` |
| New provider with internal identity | Add provider ID replacement |
| New security report | Add to `OPENSOURCE_EXTRA_EXCLUDE_FILES` |
| New leak pattern | Add to `_verify_opensource()` `leak_patterns` |
| New shared config key for internal | Add to `_INTERNAL_CONFIG_KEYS` |

## Per-Provider Thinking Format
Each provider can have a `thinking_format` config:
- `auto` (default): auto-detect from model name
- `enable_thinking`: Anthropic/Meituan-style `enable_thinking: true`
- `thinking_type`: Gemini-style `thinkingConfig.thinkingBudget`
- `none`: skip thinking parameter entirely

Configured via Settings UI per-provider or via `provider_templates/*.json`.

