---
name: tofu-context-health-bar
description: Per-conv context gauge: liquid-bubble vessel (post-2026-05-12 redesign #5)
enabled: true
tags: [frontend, ui, compaction, convention]
created: 2026-05-12T04:39:28Z
updated: 2026-05-12T04:39:28Z
---

# Context Health Bar — "Liquid Bubble" (2026-05-12, redesign #5)

Quiet status chip on the left flank of `.chat-wrapper`, vertically
centered, showing prompt-token usage as a % of the active model's
context window.

## Geometry (single design across all themes)

- 28×28 SOLID circular vessel ("bubble") — the gauge IS the vessel,
  no donut/track + fill split.
- Inline SVG layers (back→front):
  1. `<circle .ctx-bubble-bg>` — empty vessel base (theme cream/dark)
  2. `<g clip-path>` clipping the liquid to a perfect circle:
     - `<g .ctx-bubble-wave-group>` (THE ONLY MOVING ELEMENT — set
       `transform="translate(0 Y)"` per percentage)
       - `<path .ctx-bubble-wave>` — liquid body (filled by zone color)
       - `<path .ctx-bubble-meniscus>` — bright stroke on wave top
  3. `<circle .ctx-bubble-rim>` — glass rim stroke (always on top)
  4. `<path .ctx-bubble-shine>` — static upper-left highlight arc
- Counter badge: top-right of bubble, hidden when 0.
- "34%" + "·" + "1M" text to the right (cap dimmed until hover).

## How the liquid "rises"

- Wave path is built ONCE in `_buildWavePath()` — series of cubic
  Béziers approximating a sine wave at amplitude `_WAVE_AMP=1.1`,
  width `_WAVE_VIEW*2=64`, closed downward as a polygon.
- Wave path local y=0 = wave midline (NOT surface).
- JS computes `liquidTopY` to translate the wave group:
    `bottomAnchor = cy + r + _WAVE_AMP`  (pct=0 → crests at bubble bottom)
    `topAnchor    = cy - r - _WAVE_AMP`  (pct=1 → troughs at bubble top)
    `liquidTopY = lerp(bottomAnchor, topAnchor, pct)`
- The +/- `_WAVE_AMP` offsets are critical: without them, the wave's
  trough at pct=1 would leave a gap at the bubble top, and its crest
  at pct=0 would peek into the bubble bottom.
- CSS `.ctx-bubble-wave-group { transition: transform .55s }`
  smooths the level rise/fall.

## ⚠ NO MOTION RULE

- Only motion: wave group `transform` translates on actual value
  change.
- Wave geometry NEVER moves (no horizontal drift, no amplitude
  pulse).
- Counter "land" cue: one-shot 1.4 s `ctxBadgeLand` keyframe scaling
  the badge — no idle loop.
- No ambient pulses, blinks, shakes.

## Compaction binding (simplified post-redesign)

- Click chip → `openCompactionViewer(activeConvId)` (auto-selects
  most recent archive).
- Counter badge displays compaction count for the active conv.
- `compaction_done` SSE → `flashGaugeForArchive` adds `is-landing`
  class to the BADGE (a one-shot scale pulse) — earlier per-tick
  flash is gone.
- The earlier in-gauge timeline (radial ticks / sesame seeds /
  capsule dots / per-tick hover↔marker cross-highlight) is
  intentionally retired. Past liquid-level marks inside the bubble
  competed with the surface read; the dots-above-capsule layer
  added clutter. Now the badge alone communicates "there are N
  compactions in this conv — click to inspect."

## Design history (lessons)

The chip went through 5 visual concepts. Failure modes:

1. **Donut + tofu glyph**: every AI tool uses a ring → generic.
   Center icon collapsed into noise at 14 px.
2. **Tofu plank** (slab, marinade fill, knife cuts): green marinade
   read as a separate object inside the cream bar.
3. **Bento tray** (10 segmented cells, sesame seeds): cells too
   small to read; chip felt like a battery indicator.
4. **Halo capsule** (thin pill, halo trailing leading edge): too
   restrained. Read as anonymous, not Tofu.
5. **Liquid bubble** (current): solid circular vessel filled with a
   wavy liquid that rises with context. Solid (not hollow), one
   shape = one meaning, liquid texture without motion.

If user wants any of #1–#4 back: each had a documented failure
mode. **Do NOT re-attempt them without solving the failure mode
first.**

## Update hooks (must trigger `updateContextBar()`)

1. `static/js/ui.js`:
   - Every `assistantMsg.usage = …` write (4 sites).
   - **`phase` SSE event with `phase === 'llm_thinking'`** (each
     LLM round, orchestrator.py:_emit_tool_round_phase).
   - **`compaction` and `compaction_done` SSE events** — both call
     `updateContextBar()`; `compaction_done` also calls
     `flashGaugeForArchive(ev.archiveId)`.
2. `static/js/main.js`:
   - `_applyModelUI` (model swap), `_restoreConvToolState` (conv
     switch), `newChat` (reset).
   - **POST `/api/chat/send` and `/api/chat/regenerate`** — fired
     before request goes out.
3. `static/js/compaction-viewer.js`:
   - `attachCompactionMarkersToConversation` calls
     `updateContextBar()` after re-hydrating markers.

## Public API (stable across redesigns)

- `window.updateContextBar()` — recompute + repaint (rAF-coalesced).
- `window.flashGaugeForArchive(id)` — one-shot cue when a new
  compaction lands. Currently flashes the counter badge; previously
  flashed an in-gauge tick. Callers don't need to change.
- `window._resolveContextLimit(modelId)` — exported for debug.

## Files

- `static/js/context-bar.js` (~462 lines). `_collectCompactions(conv)`
  walks `conv.messages[]._compactions[]`. `_buildWavePath()` builds
  the static wave path used in the bubble.
- `static/styles.css` — block at EOF marked
  `Context indicator — "Liquid Bubble"`. Per-theme tokens:
  `--ctx-vessel-bg`, `--ctx-rim`, `--ctx-shine`, `--ctx-counter-*`.
  Legacy classes (`.ctx-bar-rail`, `.ctx-bar-icon`, `.ctx-bar-plank`,
  `.ctx-tray-cell`, `.ctx-bar-track`, `.ctx-bar-fill`,
  `.ctx-bar-shell`, `.ctx-bar-dots`, `.ctx-bar-tick`, …) are kept
  in the `display:none !important` safety-net selector — do NOT
  delete those without verifying no cached HTML/JS still references
  them.
- `static/js/ui.js` — phase / compaction SSE hooks.
- `static/js/main.js` — POST hooks.
- `static/js/compaction-viewer.js` — refresh on attach.
- `lib/js_bundler.py:_BUNDLE_FILES` — `context-bar.js` registered
  after `main.js` (already in place).

## Token math (unchanged across all redesigns)

Reads the **last** assistant message in `activeConv.messages` that
has `usage`. Prefers `_liveLastRoundUsage.tokensIn` (live SSE-driven
mid-turn reading) → `apiRounds[-1].usage` → `usage / N` legacy.
Mirrors `ui.js:1853` for the cache-vs-residual normalization.
Zone thresholds: `>=0.95` crit, `>=0.82` hot (matches
`_SUMMARY_TRIGGER_RATIO` in `compaction.py`), `>=0.60` warn, else ok.

## TRAP: JS bundler order

Any new top-level JS file under `static/js/` MUST be added to
`lib/js_bundler.py:_BUNDLE_FILES`. The `_APP_SCRIPTS_RE` in
`routes/common.py` will strip its `<script>` tag from the served
HTML even if it's in `index.html`. Server restart required after
editing `js_bundler.py`. CSS/JS file edits ARE mtime-checked so
edits to existing files don't need restart — but a hard-refresh
in the browser is needed since the bundle filename hash flips.

## Updating the model-limit table

When a new model family ships, add a `[regex, limitOrFunction]` row
to `_CONTEXT_LIMITS` in `context-bar.js`. Keep server-side
`lib/tasks_pkg/compaction.py:_get_context_limit` in sync — that's
the authority.

