---
name: mobile-tofu-theme-css-specificity-war
description: Mobile CSS specificity war: tofu theme [data-theme='tofu'] .class (0,2,0) beats mobile .class (0,1,0) — sidebar stays relative, overflow:visible defeats containment
enabled: true
tags: [css, mobile, tofu-theme, bug-pattern]
created: 2026-04-07T09:15:39Z
updated: 2026-04-07T09:15:39Z
---

# Mobile CSS Specificity War with Tofu Theme

## Problem
The tofu theme sets `overflow:visible` and `position:relative` on `.sidebar`, `.chat-wrapper`,
`.input-area`, `.input-inner`, `.input-actions`, `.input-actions-scroll` etc.
with selector `[data-theme="tofu"] .class` (specificity 0,2,0).

Mobile `@media(max-width:768px)` rules using plain `.class` selectors (specificity 0,1,0) LOSE,
causing:
- Sidebar stays `position:relative` (takes 280px, pushes `.main` right)
- `overflow:visible` defeats all overflow containment — badges/buttons overflow right edge
- Topbar items extend beyond 360px viewport

## Fix
In `@media(max-width:768px)`, prefix with `[data-theme="tofu"]` to match specificity:
```css
@media(max-width:768px) {
  [data-theme="tofu"] .sidebar{position:fixed!important;overflow:hidden!important}
  [data-theme="tofu"] .chat-wrapper{overflow:hidden!important}
  [data-theme="tofu"] .input-area,.input-inner,.input-actions{overflow:hidden!important}
}
```

## Mobile Input Toolbar Architecture
- Desktop: full toolbar with submenus (增强/工具/模式), action buttons, depth bar
- Mobile: compact `[Model▾] ――― [⋯] [🔍] [⏎]`
  - All submenus → hidden, accessible via "⋯" bottom sheet
  - Thinking depth → hidden in toolbar, shown in bottom sheet
  - Bottom sheet: Chinese labels, vertical name+desc layout

## Caching Gotchas
1. Server `_bundled_index_cache` — check `index.html` mtime too (not just bundle hash)
2. CSS `Cache-Control: immutable` — change `?v=` tag to bust browser cache
3. HTML `max-age=0, no-cache` for fast mobile iteration

