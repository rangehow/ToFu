---
name: delete-turn-popup-clipped-by-overflow-hidden
description: Bug fix: delete-turn confirmation popup invisible because .message-content has overflow:hidden, clipping the absolutely-positioned popup
enabled: true
tags: javascript, css, bug-fix, delete-turn, popup, overflow-hidden, positioning, ui.js
created: 2026-04-21T04:18:46Z
updated: 2026-04-21T04:18:46Z
---

# Delete-turn popup silently clipped by ancestor overflow:hidden

## Symptom
Clicking the 🗑️ trash button on a message's action bar does nothing visible
— no confirmation popup appears, no error in F12 console, no server request
is made.

## Root cause
`deleteTurn(idx)` in `static/js/ui.js` DID create the confirmation popup
(`.delete-turn-popup`) and append it to the message, but it appended into
`.message-content` which has:

```css
/* static/styles.css line 143 */
.message-content{
  flex:1; min-width:0; max-width:100%;
  overflow:hidden;          /* ← clips anything outside its box */
  position:relative;
  contain:layout style;     /* ← also creates a containing block */
}
```

The popup CSS (line 1629):
```css
.delete-turn-popup{
  position:absolute;
  bottom:calc(100% + 4px);  /* ← positioned ABOVE top edge of parent */
  right:0;
  ...
}
```

Because `.message-content` is the nearest `position:relative` ancestor AND
has `overflow:hidden`, the popup renders at negative Y (above the top edge)
and is instantly clipped → invisible → user sees "nothing happens".

On mobile the bug was masked because `@media` overrides with
`position:fixed; top:50%; left:50%` centered it in the viewport instead.

## Fix
Attach popup to `document.body` with `position:fixed`, compute coordinates
from the clicked button's `getBoundingClientRect()`, flip above the button
if it would overflow the viewport bottom. Clamp `left` to `[8, innerWidth - popupWidth - 8]`
to keep it on-screen.

Code: `static/js/ui.js` inside `deleteTurn()` — the block that used to do
`msgEl.querySelector('.message-content').appendChild(_deletePopup)`.

## General lesson
**Beware `position:absolute` popups inside ancestors with `overflow:hidden`**.
If a popup/tooltip isn't visible but the DOM element IS in the tree, check
every ancestor for `overflow:hidden|clip|scroll` and `contain:paint|strict`.
Safest pattern: append floating UI to `document.body` with `position:fixed`
and compute coords from the trigger's bounding rect.

## Verification
After fix: click 🗑️ on any message → popup appears near the button with
"Delete turn / This only / Cancel" options, click outside or wait 5s to
auto-dismiss, "Delete turn" deletes user+assistant pair via
`DELETE /api/conversations/<id>/messages/<idx>?mode=turn`.

Remember: rebuild JS bundle after ui.js edits:
`python3 -c "from lib.js_bundler import build_bundle; build_bundle()"`

