---
name: use-real-brand-icons-not-emoji
description: MANDATORY: brand/product icons must use official SVG logos, never emoji. Brand SVGs in static/icons/. MCP catalog icons: lib/mcp/registry.py 'icon' field renders as innerHTML (grid+modal) — use <img src="static/icons/mcp/<id>.svg"> for brands; stored in static/icons/mcp/.
enabled: true
tags: [html, css, icons, svg, branding, convention, mandatory]
created: 2026-03-29T11:14:03Z
updated: 2026-06-10T06:36:53Z
---

## Rule
When an icon represents a real product/service/brand (Feishu/Lark, Google, GitHub, Claude, OpenAI, Docker, Slack…),
you **MUST** use the official SVG logo. **Never** substitute a generic emoji or random SVG path.

## How to Find Official Icons
1. Search the web for `{brand} logo SVG`.
2. Best sources (in order):
   - **simple-icons** via jsdelivr (works, fast): `https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/{slug}.svg` — monochrome single-path, viewBox 0 0 24 24, fill currentColor-ready. Has GitHub/GitLab/Slack/Notion/Docker/K8s/AWS/Stripe/Figma/Discord/Perplexity/Zapier/Vercel/Supabase/Postgres/Redis/Mongo/Sentry/Cloudflare/Overleaf/Playwright/Puppeteer/BigQuery/Jira/Asana/Todoist/Upstash/Brave/Gmail/Google-Drive etc.
   - dashboard-icons / LobeHub (note: `jsdelivr.net/gh/...` raw-GitHub path can TIME OUT — prefer npm path above).
   - **Favicon fallback** for brands NOT in simple-icons (Tavily, Exa, Firecrawl, Context7): `curl {site}/favicon.ico` → PIL convert → base64-embed PNG inside `<svg><image href="data:image/png;base64,…"/></svg>`.
3. `curl` (not fetch_url) for raw assets.

## Implementation Pattern
- Save to `static/icons/{brand}.svg` (external file). For MCP catalog: `static/icons/mcp/{id}.svg`.
- Reference with `<img src="static/icons/{brand}.svg" width="20" height="20" alt="{Brand}">` — bare relative `static/...` path is the project convention (index.html:1254/1283), no BASE_PATH/apiUrl needed for innerHTML-injected refs.
- Dark theme on DARK bg: `style="filter:brightness(0) invert(1)"` whitens a dark SVG. On LIGHT/cream tiles (e.g. MCP cards use `--s-cream`) DO NOT invert — black simple-icons render correctly.
- Size to context: 15×15 tab buttons, 20×20 section titles, 26px MCP grid tile, 34px MCP modal.

## MCP catalog icons (lib/mcp/registry.py)
- Each CatalogEntry has an `'icon'` field. The frontend (`static/js/settings/mcp.js` `_renderMcpCatalog` + `_mcpOpenInstallModal`) injects it via **innerHTML when the string starts with `<`**, else escapes it as an emoji/text. So an `<img …>` or inline `<svg …>` string Just Works.
- 37 brand entries migrated emoji→`<img src="static/icons/mcp/<id>.svg">` (2026-06). Generic-concept entries kept as emoji: fetch 🌐, memory 🧠, sequential-thinking 💭, filesystem 📂, mcp-compass 🧭, Xuecheng 📖. Hope uses an inline `<svg>` string.
- CSS sizing added in static/styles.css: `.mcp-app-icon img,.mcp-app-icon svg{width:26px;height:26px;object-fit:contain}` and `.mcp-install-modal-icon img,…svg{width:34px;height:34px}`.

## Existing Icons in Project
- static/icons/: claude.svg, openai.svg, lark.svg, niutrans.svg, translate.svg
- static/icons/mcp/: 37 brand logos (github, gitlab, git, linear, postgres, sqlite, redis, mongodb, slack, gmail, brave-search, tavily, exa, firecrawl, notion, todoist, google-drive, docker, kubernetes, sentry, cloudflare, stripe, figma, playwright, puppeteer, context7, supabase, vercel, aws, upstash, jira, asana, discord, perplexity, zapier, bigquery, overleaf)

## OK to keep emoji
Generic concepts: 🔑 credentials, 📂 workspace, 👥 access, ⚠️ warning, 🧠 memory, 💭 thinking.

## NOT OK
💬 for Feishu, 🔍 for Google, 🐙 for GitHub, 🐳 for Docker — always the real logo.
