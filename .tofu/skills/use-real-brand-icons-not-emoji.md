---
name: use-real-brand-icons-not-emoji
description: MANDATORY: When adding icons for real products/brands (Feishu, Google, GitHub etc), must search for and use official SVG logos — never substitute with emoji; official Lark SVG stored at static/icons/lark.svg
enabled: true
tags: [html, css, icons, svg, branding, convention, mandatory]
created: 2026-03-29T11:14:03Z
updated: 2026-04-10T07:40:25Z
---

# Use Real Brand Icons, NOT Emoji

## Rule
When an icon represents a real-world product, service, or brand (e.g. Feishu/Lark, Google, GitHub, Claude, OpenAI),
you **MUST** use the actual official SVG logo. **Never substitute with a generic emoji or random SVG path.**

## How to Find Official Icons
1. **Search the web** for `{brand} logo SVG` or `{brand} icon SVG`
2. Check these sources:
   - [simple-icons](https://github.com/simple-icons/simple-icons) — `https://raw.githubusercontent.com/simple-icons/simple-icons/develop/icons/{slug}.svg`
   - [homarr-labs/dashboard-icons](https://github.com/homarr-labs/dashboard-icons)
   - [LobeHub Icons](https://unpkg.com/@lobehub/icons-static-svg@latest/icons/{slug}.svg)
   - Brand's own design/press page
3. Use `curl` to download since `fetch_url` may fail on raw GitHub
4. **For Chinese products** (NiuTrans, etc.): check the site's favicon and JS bundles for asset paths
   - Vue SPA: look in `js/app.*.js` for `img/*.hash.png` patterns
   - Download favicon: `curl -sL "https://site.com/favicon.ico" -o /tmp/favicon.ico`
   - Extract from ICO: `PIL Image.open('favicon.ico').save('favicon.png')`

## Implementation Pattern
- **Save to `static/icons/{brand}.svg`** — external file, not inline SVG
- For bitmap-only brands: embed the favicon PNG as base64 inside an SVG wrapper
- Reference with `<img src="static/icons/{brand}.svg" width="20" height="20" alt="{Brand}">`
- For dark theme: `style="filter:brightness(0) invert(1)"` makes any dark SVG white
- Size to context: 15×15 for tab buttons, 20×20 for section titles
- Use `style="vertical-align:-3px"` for alignment in text

## Existing Icons in Project
- `static/icons/claude.svg` — Official Claude logo (Simple Icons)
- `static/icons/openai.svg` — Official OpenAI logo (LobeHub Icons)
- `static/icons/lark.svg` — Official Lark/Feishu logo (dashboard-icons)
- `static/icons/niutrans.svg` — NiuTrans favicon (base64 PNG in SVG wrapper, brand blue #5589FC)
- `static/icons/translate.svg` — Generic translation icon (A↔文 concept, uses currentColor)

## What's OK
- Emoji for **generic concepts**: 🔑 (credentials), 📂 (workspace), 👥 (access control), ⚠️ (warning)

## What's NOT OK
- 💬 for Feishu/Lark — use the official Lark logo
- 🔍 for Google — use the Google logo  
- 🐙 for GitHub — use the GitHub mark
- Random inline SVG path for Claude/OpenAI — use the official saved icon file
- Generic globe SVG for NiuTrans — use the real `static/icons/niutrans.svg`

