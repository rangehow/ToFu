---
name: machine-translation-provider-architecture
description: Machine translation: multi-provider (NiuTrans default + custom), dedicated tab, real brand logos, LLM fallback
enabled: true
tags: [translation, mt-provider, niutrans, settings, architecture]
created: 2026-04-10T03:43:54Z
updated: 2026-04-10T07:39:52Z
---

# Machine Translation Provider Architecture

## Feature
Dedicated machine translation providers as faster/cheaper alternative to LLM-based translation.
Multi-provider support with NiuTrans as default and "Custom" option for other APIs.

## Key Files
- `lib/mt_provider.py` — MT provider adapters (NiuTrans v1 simple + v2 signed API)
- `lib/__init__.py` — `MT_PROVIDER_CONFIG` module-level variable, hot-reloadable
- `routes/translate.py` — `_translate_one_chunk()` checks `is_mt_configured()` first, falls back to LLM
- `routes/translate.py` — `/api/translate/mt-test` endpoint for Settings UI test button
- `routes/config.py` — `mt_provider` section in GET/POST `/api/server-config`
- `index.html` — **Dedicated "翻译" tab** in settings (between Search and Network)
- `static/styles.css` — `.mt-provider-card`, `.mt-apply-link`, `.mt-provider-select` etc.
- `static/js/settings.js` — `_populateMtProviderSection()`, `_collectMtProviderConfig()`, `_testMtProvider()`, `_switchMtProvider()`

## Icons
- `static/icons/translate.svg` — Tab icon (A↔文 concept, uses `currentColor`)
- `static/icons/niutrans.svg` — NiuTrans brand icon (official favicon embedded as base64 PNG in SVG wrapper, brand color #5589FC)

## Settings UI Design
- Separate "翻译" tab with translate icon (`static/icons/translate.svg`) in settings sidebar
- Enable/disable toggle at top
- **Provider selector dropdown** (`<select>`) with options: NiuTrans (default), Custom
- `_switchMtProvider(provider)` shows/hides the corresponding card
- **NiuTrans card**: real logo, "申请 API Key" button → niutrans.com/cloud/overview, "去获取 →" inline link
- **Custom card**: generic globe icon, same fields but API URL is required
- Each card has its own test button + result span (suffixed with `Custom` for the custom card)
- `_collectMtProviderConfig()` reads from the active card's fields based on selected provider

## Config Structure (in server_config.json)
```json
{
  "mt_provider": {
    "provider": "niutrans",
    "api_url": "",
    "api_key": "your-api-key",
    "app_id": "",
    "enabled": true
  }
}
```

## Behavior
- **Not configured / disabled**: Uses LLM cheap model as before (no change)
- **Configured**: `_translate_one_chunk()` tries MT first → LLM fallback on failure
- MT doesn't need the translation prompt — text is sent directly to the MT API
- Code blocks protected with `[CBLOCK_N]` placeholders during translation

## CSS Design Tokens
Uses settings panel's tofu design system: `--s-white`, `--s-ink`, `--s-border`, `--s-shadow-xs`, `--accent-color`
NiuTrans logo container uses brand blue `rgba(85,137,252,.12)` gradient.

