---
name: emit-content-block-styling-and-json-pretty-print
description: emit_to_user inline block uses left-stripe (var(--accent) via color-mix), auto pretty-prints JSON; no outer frame
enabled: true
tags: [ui, css, emit_to_user]
created: 2026-05-01T17:35:30Z
updated: 2026-05-01T17:35:30Z
---

# emit_to_user inline block — styling + JSON pretty-print

## Location
- Render: `static/js/ui.js` — the `if (!isUser && msg._emitContent)` block
  around line 1333 inside the main message render function. Structure:
  ```html
  <div class="emit-content-block">
    <div class="emit-content-label">{tool_name}</div>
    <pre class="emit-content-output"><code>{pretty_text}</code></pre>
  </div>
  ```
- CSS: `static/styles.css` — `.emit-content-block`, `.emit-content-label`,
  `.emit-content-output` near the "emit_to_user — inline tool result in
  assistant bubble" comment (~line 4537).

## Design principles (do not regress)
- NO outer frame / gray filled header bar — previous version had a big
  rounded box with tinted background, which looked like a boxed artifact.
- Just a thin left accent stripe (`border-left: 2px solid
  color-mix(in srgb, var(--accent) 45%, transparent)`) + small inline
  label pill with `background: var(--accent-subtle)`.
- Stripe and label use `var(--accent)` / `var(--accent-subtle)` so they
  theme correctly (default purple, tofu warm-beige, light).
- Transparent `<pre>` and `<code>` — no background — so the payload
  reads as a natural continuation of the assistant text.
- No 📤 or other emoji in the label (CLAUDE.md §3.4: emoji not used to
  represent specific products; here the frame was doing too much work).

## JSON auto-pretty-print
Before rendering, if `trimmed[0] === '{' || '['`, attempt
`JSON.parse` → `JSON.stringify(parsed, null, 2)`. Silently fall back
to the raw string on parse error. This avoids the "one raw line of
{...}" look for tool results like `hope_check_login`.

## Remember
- Rebuild bundle after ui.js edits:
  `python3 -c "from lib.js_bundler import build_bundle; build_bundle()"`

