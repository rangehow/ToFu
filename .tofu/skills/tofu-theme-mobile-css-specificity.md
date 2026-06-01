---
name: tofu-theme-mobile-css-specificity
description: Tofu theme mobile CSS: specificity 0,2,0 overrides, input toolbar refactored to compact layout + bottom sheet for toggles
enabled: true
tags: [css, mobile, tofu-theme, specificity, bug-pattern]
created: 2026-04-07T08:33:07Z
updated: 2026-04-07T08:54:14Z
---

# Tofu Theme vs Mobile CSS — Complete Guide

## CSS Specificity War
The tofu theme uses `[data-theme="tofu"] .class` (specificity 0,2,0) which **beats** mobile `@media` rules using plain `.class` (0,1,0). EVERY mobile override must either:
1. Use `[data-theme="tofu"]` prefix to match specificity
2. Use `!important` as insurance
3. Cover ALL elements the tofu theme touches

### Elements the tofu theme overrides with overflow:visible
- `.sidebar` → `position:relative; overflow:visible`
- `.chat-wrapper` → `overflow:visible`
- `.input-area`, `.input-inner`, `.input-actions`, `.input-actions-scroll`, `.input-group`, `.toolbar-submenu`, `.preset-toggle-wrapper` → `overflow:visible`

### Elements with decorative pseudo-elements (hide on mobile)
- `.input-box::before` — pixel mascot
- `.input-box::after` — controller grip wings
- `.input-area::before/::after` — sparkles
- `.input-row::before/::after` — D-pad + button dots
- `.input-inner::before` — speech bubble tail

## Mobile Input Toolbar Architecture
On 360px screens, the desktop toolbar (model picker + 3 submenus + 2 action btns + search + send = ~8 items) doesn't fit.

### Solution: Compact toolbar + "more" bottom sheet
- **Visible in toolbar**: Model picker, thinking depth, "···" more button, search toggle, send button
- **Hidden behind "···"**: All submenus (AI Enhance, Tools, Mode) and standalone action buttons (imageGen, project)
- **Bottom sheet**: `#mobileSheet` with `toggleMobileSheet()`/`closeMobileSheet()`/`updateMobileSheet()` in main.js
- Sheet items mirror desktop toggles — each calls the same toggle function + `updateMobileSheet()`

### Mobile topbar: only 3 items
hamburger | title (flex:1) | model badge (right-aligned, compact)
All feature badges and action buttons hidden via `display:none!important`.

## Server-Side HTML Cache
`routes/common.py` caches the bundled HTML keyed on bundle_tag + `index.html` mtime. Changing CSS version tags is auto-detected.
