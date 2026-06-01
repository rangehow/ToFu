---
name: content-visibility-auto-interactive-card-rendering-bug
description: Bug: content-visibility:auto + contain-intrinsic-size:32px collapses tall interactive cards (HG/stdin/approval) when slot innerHTML changes from spinner to card
enabled: true
tags: [css, content-visibility, bug-fix, rendering, interactive-cards, hg-card, performance]
created: 2026-04-13T03:43:25Z
updated: 2026-04-13T03:43:25Z
---

# content-visibility:auto Breaks Interactive Card Rendering

## Bug Pattern
`.ptool-panel-body > [data-prn]` has `content-visibility:auto; contain-intrinsic-size:auto 32px` for performance optimization (skip rendering off-screen tool round slots in long sessions with 50+ rounds).

When a slot's innerHTML changes from a one-line spinner (`.ptool-active`, ~32px) to a tall interactive card (`.hg-card` ~200px, `.ptool-cmd-stdin`, `.ptool-pending`), the browser may:
1. Cache the 32px intrinsic size from the previous content
2. Consider the slot "off-screen" based on the stale size
3. Not render the new tall content, keeping it collapsed at 32px

Symptom: Interactive cards (questions, options, input boxes) appear as plain tool lines. Switching conversations fixes it because `showStreamingUIForConv` rebuilds all slots from scratch.

## Fix (3 layers)
1. **CSS `:has()` override**: Slots containing interactive card classes get `content-visibility:visible` override
2. **JS inline style**: `slot.style.contentVisibility = "visible"` when creating/updating interactive slots
3. **JS fallback re-render**: If round is interactive but slot lacks the expected card DOM, force re-render

## Key Files
- `static/styles.css` lines ~3052-3061
- `static/js/ui.js` `_syncToolRoundsDOM` function — slot update chain

## Related
- `contain: layout style` on `.ptool-panel-body` creates containment context
- `content-visibility:auto` on children skips rendering for off-screen elements
- Same pattern could affect any element that transitions from small to tall within a containment context

