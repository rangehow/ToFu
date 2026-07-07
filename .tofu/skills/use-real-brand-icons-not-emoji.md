---
name: use-real-brand-icons-not-emoji
description: MANDATORY (CLAUDE.md §3.4): UI icons SVG-only, NO emoji. THREE emoji sources for a tool row: (1) round.query label builders (tool_display.py, browser/display.py); (2) generic meta icon/badge/title via _build_simple_meta/simple_call (misc.py + executor_image.py); (3) MCP meta in handlers/mcp.py (🔌 badge/title). All de-emoji'd; frontend _getToolSvg renders the SVG.
enabled: true
tags: [html, css, icons, svg, branding, convention, mandatory]
created: 2026-03-29T11:14:03Z
updated: 2026-06-26T05:01:35Z
---

## Rule (CLAUDE.md §3.4)
UI **icons** must be SVG, NEVER emoji — brand AND generic-concept. Scope = rendered UI
affordances (incl. debug drawer). OUT of scope (left as emoji): console/`debugLog`/`_orchRunLog`
strings, code comments (`★`), `showToast(icon,…)` (auto-stripped by `_stripEmoji` core/toast.js),
info-banner PROSE tips, typographic dingbats (✓ ✕ ✗ ✦ ✎ ± ∅ ▶ ▾ ▸ ⏸ ↻ ⬇ ⬆ ↔ ⏱ ⬡ ⊞ ⟳ ✚ ⚠ · →),
folder `⭐ 置顶`, `✅`/`❌` endpoint-critic protocol tokens, AND error-detection SENTINELS that match
emoji in tool RESULT CONTENT (e.g. `tool_content.startswith('❌')` in mcp.py — content, not UI).

## THE FRONTEND HELPER
`static/js/core/icons.js` → `window.Icon(name,size?,extraStyle?)` (Lucide, stroke=currentColor)
+ `window.IconDot(color,size?)`. ~44 glyphs in `_PATHS`. Bundled right after i18n.js.

## ★ TOOL-CALL ROW has THREE emoji sources — fix ALL
A tool round renders ONE SVG via `static/js/ui/tool_rounds.js::_getToolSvg(round)`
(project→_projToolSvg, browser→_browserToolSvg, imagegen→own, `mcp__*`→_webToolSvg.mcp plug,
else `_webToolSvg[icon]||_webToolSvg[toolName]||.generic` wrench). Emoji anywhere else = DUPLICATE.
1. **The label** = `round.query` text beside the SVG. Built by:
   `lib/tasks_pkg/tool_display.py` `_tool_display_*` (conv_ref/fetch_url/scheduler/desktop/
   swarm/compact/image_gen/human_guidance/mcp/generic + except-fallback in
   `_build_tool_round_entry`); `lib/browser/display.py::_DISPLAY_HANDLERS` (19 lambdas);
   `lib/project_mod/tools.py::project_tool_display` (already clean). → de-emoji'd 2026-06-26.
2. **Generic result meta** = `meta.title`/`meta.badge` (badge pill + preview), built by
   `_build_simple_meta`/`simple_call` (`lib/tasks_pkg/executor.py`, `handlers/_adapter.py`).
   The `icon=` kwarg ONLY feeds the default fallback (frontend ignores it for the row icon) →
   pass NO icon, keep badge/title text-only. De-emoji'd:
   - `lib/tasks_pkg/handlers/misc.py`: swarm (`_SWARM_BADGE_VERB`, was `_SWARM_ICON_MAP` 🐝⏳📥📦),
     `_build_await_post_build` (badge `'timed out · N/M done'` + `meta.awaitTimedOut` amber),
     ask_human (🙋/✅/⛔/❌→plain), scheduler/desktop (dropped `icon=⏰`/`🖥️`), conv_ref (💬/📋).
   - `lib/tasks_pkg/executor_image.py`: image badges (`⏳ editing…`/`❌ failed`→plain); kept
     `✓ {model}`/`⚠ svg failed` dingbats + `✓`/`✗` in log strings.
3. **MCP result meta** = its OWN handler `lib/tasks_pkg/handlers/mcp.py::handle_mcp_tool`
   (NOT misc.py — dynamic `mcp__*` fallback). `_post_build` set `meta.badge='🔌 {server}'`,
   `meta.title='🔌 {server}/{tool}'`, `❌ {server}` on error, `icon='🔌'`. → de-emoji'd: badge=
   bare `server_name`, error=`'{server} (error)'`, title=`'{server}/{tool} — {suffix}'`, dropped
   `icon=`. KEEP the `❌` in the `is_error` startswith() sentinel (matches result content).
- When de-emoji'ing a tool with NO specific frontend icon, ADD an SVG to `_webToolSvg` keyed by
  toolName. Done: get_conversation/list_conversations/await_agents/get_agent_result/
  context_compact/mcp (+ `_getToolSvg` maps `mcp__*`→plug).

## CRITICAL constraints
- i18n `data-i18n`→textContent (no SVG) → SVG sibling + inner `<span data-i18n>`, or strip emoji.
  `data-i18n-html`→innerHTML (SVG OK). `el.textContent='🔧…'`→`el.innerHTML=Icon(...)+escapeHtml(rest)`.

## Verify
- `node --check` edited JS; rebuild: `python3 -c "from lib.js_bundler import build_bundle; print(build_bundle())"`.
- Backend emoji sweep (ALL meta+label sources):
  `grep -nP "[\x{1F000}-\x{1FAFF}\x{2600}-\x{27BF}\x{2300}-\x{23FF}]" lib/tasks_pkg/tool_display.py lib/browser/display.py lib/tasks_pkg/handlers/misc.py lib/tasks_pkg/handlers/mcp.py lib/tasks_pkg/executor_image.py`
  → only `★` comments + ✓/⚠/✗ log/dingbats + mcp.py line-96 content-sentinel `❌` OK.
- Runtime: build a round per tool via `_build_tool_round_entry`; run `_build_await_post_build()` +
  the mcp `_post_build` — assert no emoji (ord>0x2500) in `query`/`badge`/`title`.
- MCP catalog: `python3 -c "from lib.mcp.registry import CATALOG; print([e['id'] for e in CATALOG if not e['icon'].startswith('<')] or 'ALL SVG')"`.

## Existing icon files
static/icons/ (claude/openai/lark/niutrans/translate.svg) + static/icons/mcp/ (42); core/icons.js ~44 glyphs.
