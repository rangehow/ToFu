---
name: tofu-tool-row-no-opacity-rule
description: Tool-panel rows must NEVER use opacity/transparency to convey state. Use solid-color labels.
enabled: true
tags: [frontend, ui, ptool, compaction, rule]
created: 2026-05-12T05:03:50Z
updated: 2026-05-12T05:03:50Z
---

# Tool-panel: NO transparency rule (2026-05-12)

## Hard rule (do not regress)
**Never use opacity, alpha-channel backgrounds, or fade gradients to
communicate state on `.ptool-line` rows.** Earlier attempts that
violated this rule:
- `_isHot` green dot per row — too quiet, looked like decoration
- `--ptool-fresh` opacity gradient — too subtle, "broken UI" feel
- `_ageBand` text dimming + age tag — better but still ambiguous
- `.ptool-line-compacted` chrome (purple stripe + dotted underline +
  opacity .78) — small chip easy to miss in a long panel

All of those have been removed.  The user explicitly demanded:
> "Don't just rely on fading away — this is too obscure"
> "any content compressed in multiple layers must be clearly labeled"

## Current implementation
`static/js/ui.js:_renderUnifiedToolLine` renders a SOLID-color pill
**inline before the tool name** when `round.compactionLayer` is set:

```
[icon] [COMPACTED L1 280k→2k] grep_search ...
```

Pill structure:
- `.ptool-compaction-label` — outer pill, solid background per layer
- `.ptool-compaction-text` — "COMPACTED L1" portion
- `.ptool-compaction-delta` — inner darker pill with token reduction

Per-layer palette (all SOLID, no transparency):
- L0 (born too big): `#ec4899` outer / `#be185d` inner — pink
- L1 (aged out of hot tail): `#8b5cf6` outer / `#6d28d9` inner — purple
- L3 (LLM summary): `#f59e0b` outer / `#b45309` inner — amber

Tooltip carries a one-sentence explanation of what each layer means.

## Where the data comes from
- `compactionLayer`, `compactedFromChars`, `compactedToChars` are
  stamped on `round` by the `tool_compacted` SSE branch in `ui.js`
  (around line 6556).  See also `lib/tasks_pkg/compaction.py`
  `micro_compact()` and `tool_dispatch.py` L0 budget pass.
- Token estimate = `chars / 4` (rough).
- Re-render trigger: `_msgFingerprint` includes `compactedCount` +
  `compactedToSum`; `_syncToolRoundsDOM` fingerprint includes
  `compactionLayer.length` + `compactedToChars` + `compactedFromChars`.

## Stub function `_stampFreshness(_conv)`
Now a no-op. Kept callable from `renderChat` and `updateStreamingUI`
so we don't have to thread the removal through every render path.
Safe to delete in a future cleanup pass once we confirm no other code
relies on `r._freshness` / `r._ageBand` / `r._ageDistance`.

## Format helper
`_formatTok(n)` in `static/js/ui.js` — compact "12k" / "1.5M" output.
Used by the COMPACTED pill's delta block.

## CSS file location
`static/styles.css`, search for the comment header `COMPACTED tool-row label`.
The CSS bundler (`lib/css_bundler.py`) auto-busts on edit, so you
don't need to bump any version string.

