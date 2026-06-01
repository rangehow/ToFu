---
name: pptx-translation-integration
description: PPTX file translation feature — backend-only, hidden behind feature flag in Settings, adapted from tristan-mcinnis/PPT-Translator
enabled: true
tags: [feature, pptx, translation, feature-flag, settings]
created: 2026-04-13T12:00:29Z
updated: 2026-04-13T13:31:46Z
---

# PPTX Translation Feature

## Architecture
- **Backend only** — no frontend modal/UI. Feature is accessed via API endpoints.
- **Feature flag**: `pptx_translate_enabled` in `data/config/features.json`, default OFF
  - Toggle in Settings → Feature Modules (alongside Trading module)
  - `lib/__init__.py`: `PPTX_TRANSLATE_ENABLED` flag
  - `routes/common.py`: `/api/features` GET/POST handles it
  - Endpoint guarded: returns 403 if disabled

## Files
| File | Purpose |
|------|---------|
| `lib/pptx_translator.py` | Core engine — walks shapes/tables, extracts formatting, translates, rebuilds with original formatting |
| `routes/translate.py` | API: `/api/translate/pptx` (upload+start), `/api/translate/pptx/download/<file>` |

## Key Details
- Uses existing `_translate_one_chunk()` from routes/translate.py (MT or LLM)
- Preserves: font size/name/bold/italic/underline/color, paragraph alignment/spacing, table cell formatting, grouped shapes
- Translation cache avoids re-translating identical strings
- Async task pattern: upload → poll → download
- `python-pptx>=0.6.21` required (in requirements.txt)
- Adapted from [tristan-mcinnis/PPT-Translator-Formatting-Intact-with-LLMs](https://github.com/tristan-mcinnis/PPT-Translator-Formatting-Intact-with-LLMs)

## Feature Flag Pattern (reusable)
```python
# lib/__init__.py
MY_FLAG = _resolve_feature_flag('MY_FLAG', 'my_flag', False)
# routes/common.py — add to features() GET and save_features() POST
# index.html — add toggle in settings section
# settings.js — add init + save logic
```

