---
name: i18n-frontend-language-system
description: Frontend i18n system: 500+ dict entries, myday.js fully i18n'd with 70+ keys, t() function for JS dynamic strings
enabled: true
tags: [i18n, frontend, settings, language]
created: 2026-04-10T03:42:10Z
updated: 2026-05-04T13:56:57Z
---

# Frontend i18n System

## Architecture
- `static/js/i18n.js` — loaded FIRST, before all other scripts. Contains:
  - `_i18nLang` — current language ('zh' or 'en'), persisted in localStorage key `tofu_ui_lang`
  - `_i18n` — dictionary object with ~600+ entries, each `{ zh: '...', en: '...' }`
  - `t(key, params?)` — translation function; **falls back to the KEY string if the key is missing**. This silent fallback is the #1 cause of "English-looking" strings in the UI.
  - `setLanguage(lang)` / `_onLanguageChange(lang)` — language switch + re-render hook
  - `_applyI18n()` — scans DOM for `data-i18n`, `data-i18n-html`, `data-i18n-placeholder`, `data-i18n-title` attrs

## HTML Usage
- `data-i18n="key"` → sets textContent (element's text becomes t(key))
- `data-i18n-html="key"` → sets innerHTML (use for entries with `<strong>`, `<code>`, `<br>` etc.)
- `data-i18n-placeholder="key"` → sets placeholder on inputs/textareas
- `data-i18n-title="key"` → sets title tooltip (use on ALL title= buttons)
- For mixed content (SVG + text), wrap just the text in `<span data-i18n="key">...</span>`

## JS Usage
- Use `t('key')` for every user-facing string in JS-generated HTML, `prompt()`, `confirm()`, `button.textContent`, `innerHTML` literals.
- For dynamic panels (like `optimizer.js`), rebuild the panel on every render using `t()` — not static constants.

## Keeping Language Switch Live
`_onLanguageChange` already calls:
- `renderConversationList()` and `renderMessages()` when present
- `_refreshOptimizerPanel()` when present (added 2026-05-04 so the optimizer panel updates on language switch)

**When you add a new dynamic panel** (a badge popup, modal, injected HTML list), remember to hook its re-render in `_onLanguageChange` so it updates immediately when the user flips language.

## Debugging Untranslated Strings
Audit script that caught a huge class of bugs on 2026-05-04:
```bash
python3 -c "
import re
with open('index.html','r',encoding='utf-8') as f: content=f.read()
for i,line in enumerate(content.split('\n'),1):
    if re.search(r'[\u4e00-\u9fff]', line) and 'data-i18n' not in line and '<!--' not in line:
        print(f'{i}: {line.strip()[:120]}')
"
```
List of symptoms → cause:
- **"Still Chinese on English UI" / "Still English on Chinese UI"** → the string has no `data-i18n` attribute, so the i18n pass skips it.
- **"Shows literal key like `settings.optimizerModule`"** → the HTML references a key that doesn't exist in `_i18n`. `t()` falls back to the key. Add the entry.
- **"Mixed SVG icon + translated text"** → the text sits next to `</svg>` in the same tag; wrap in `<span data-i18n="...">`.
- **"Tooltip never translates"** → forgot `data-i18n-title` on the `title="..."` attribute.
- **"Dynamic panel stays English"** → panel renders JS constants; convert every string to `t(...)` and add panel to `_onLanguageChange` re-render list.

## File Coverage Snapshot (2026-05-04)
Fully i18n'd: Settings → General / Providers / Display / Search / Translation / Network / Feishu / OAuth / MCP / Advanced, Browser Bridge modal, Daily Optimizer panel + badge, myday.js, toolbar tooltips.

Intentionally not translated: Tofu brand `豆腐` (logo), language picker options `中文` / `English` (self-naming), tool badges like `译` / `think` (one-char indicators — translation in `msg.thinking` / `toolbar.translateBadge`).

Still has raw strings: `bundle-*.js` build artifacts (never update those by hand), some deep provider-rendering branches in settings.js.

## Adding New Strings Checklist
1. Add entry to `_i18n` dictionary in i18n.js (group with similar keys via section comment).
2. For static HTML: add `data-i18n="key"` (or `-html` / `-placeholder` / `-title`).
3. For JS dynamic content: use `t('key')` in every template literal.
4. If it's a panel that renders on demand, call its refresh function from `_onLanguageChange`.
5. Test by flipping language in Settings → General.

