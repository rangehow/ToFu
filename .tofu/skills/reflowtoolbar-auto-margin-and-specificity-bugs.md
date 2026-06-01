---
name: reflowToolbar-auto-margin-and-specificity-bugs
description: _reflowToolbar measurement bugs: margin-left:auto resolves to huge px at 9999px; CSS specificity order for flex-shrink override
enabled: true
tags: [css, layout, toolbar, measurement, bug-fix]
created: 2026-04-10T07:36:05Z
updated: 2026-04-10T07:36:05Z
---

# _reflowToolbar Measurement Bugs (fixed 2026-04-10)

## Bug 1: margin-left:auto inflates measurement
`.input-actions-right` has `margin-left:auto`. When `--toolbar-w` is temporarily set to 9999px
for measurement, `getComputedStyle(el).marginLeft` returns the **used value** (thousands of px)
not the string "auto". Adding this to the width sum makes `--toolbar-w` near viewport width.

**Fix**: Skip margins that are `'auto'` (string) OR > 50px (resolved auto). Real toolbar margins
are always < 50px (ig-sep has 2px margins).

## Bug 2: CSS specificity order for ig-model-wrapper
`.ig-model-wrapper{flex-shrink:0}` (line 284) was overridden by `.preset-toggle-wrapper{flex:0 1 auto}`
(line 685) because same specificity (0,1,0) but later source order. The element has both classes.

**Fix**: Use compound selector `.ig-model-wrapper.preset-toggle-wrapper{flex-shrink:0}` 
(specificity 0,2,0) to beat the later rule.

## Bug 3: Hardcoded border width
`w += 3` assumed 1.5px border each side. Tofu theme uses 2.5px border.

**Fix**: Read `getComputedStyle(inputBox).borderLeftWidth/borderRightWidth` dynamically.

## Key files
- `static/js/main.js` → `_reflowToolbar()`
- `static/styles.css` line ~284 (ig-model-wrapper)

