---
name: paper-mode-per-theme-styling
description: Per-theme Reading Mode (Paper) visual identities: dark aurora, light paper, tofu claymorphic
enabled: true
tags: [css, paper-mode, theme, design, frontend]
created: 2026-04-18T03:13:47Z
updated: 2026-04-18T03:13:47Z
---

# Paper Reading Mode — Per-Theme Styling

Paper Reading Mode (the `.paper-mode-container` full-page split layout at
`index.html` line 218+) has a distinct visual identity per theme.
Styles live in `static/styles.css` starting around the marker block
"★ Paper Reading Mode — Per-Theme Flair" (search this to find it).

## Themes

- **Dark** (`[data-theme="dark"]` + no-attribute fallback via
  `html:not([data-theme="light"]):not([data-theme="tofu"])`) — "Midnight Scholar's Desk":
  aurora gradient backdrop, starfield ::before, glass-morphism toolbar/tabs
  (`backdrop-filter:blur`), PDF pages hover-lift with purple glow, neon-underline
  active tab (indigo→violet→sky gradient).

- **Light** (`[data-theme="light"]`) — "Modern Reading Room":
  warm cream gradient background, Linear-inspired raised PDF cards, animated
  pill tab indicator (top of tab button), **Source Serif Pro** body font on
  the report panel with 64ch max-width for reading comfort.

- **Tofu** (`[data-theme="tofu"]`) — "Cozy Study Nook":
  dashed stitch borders (`var(--stitch)`), claymorphic PDF cards with 3D-style
  border (2.5px solid with DDDC top-left + A89878 bottom-right + 3px push-down
  shadow stack), **JetBrains Mono** tab labels with `▸` prefix, RPG-dialogue
  QA bubbles matching existing `.message` styling for this theme.

## Patterns to reuse

1. **Claymorphic 3D card** (tofu): `border:2.5px solid #C4B89E;
   border-top-color:#DDD6C4; border-left-color:#DDD6C4; border-right-color:#A89878;
   border-bottom-color:#A89878; box-shadow:0 3px 0 #C8BDA6,0 4px 0 #B8AD96,...`
2. **Glass panel** (dark): `background:linear-gradient(180deg,rgba(24,28,44,0.7),
   rgba(18,22,34,0.4)); backdrop-filter:blur(12px);`
3. **Hover-lift PDF card**: `transform:translateY(-2px)` + enhanced shadow on hover
   with `transition:transform .25s cubic-bezier(.2,.9,.3,1.1)`.
4. **Animated tab indicator**: pseudo-element `::after` positioned 2-3px tall,
   gradient fill, slides in with `scaleX` keyframe.

## Key design language sources
- Tofu theme = Sprout Lands pixel UI + Japanese 豆腐 aesthetic (ivory/kinako/matcha)
- Dark theme = aurora + glass (inspired by editors like Zed / Linear dark)
- Light theme = Linear + serif magazine reading

## Gotchas
- The default (no `data-theme` attribute) falls back to dark behavior — use
  `html:not([data-theme="light"]):not([data-theme="tofu"])` selector alongside
  `[data-theme="dark"]` to cover both.
- `.paper-mode-container::before` uses `position:absolute;inset:0;z-index:0`
  for starfield/grain texture — MUST pair with `.paper-body{position:relative;z-index:1}`
  to keep content above the overlay.
- Keep `var(--stitch)` (tofu-only custom property `1.5px dashed #C2BCA4`)
  only under `[data-theme="tofu"]` selectors.

