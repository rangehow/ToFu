---
name: convview-controller-migration-ledger
description: ConvView (static/js/conv_view.js) unified chatInner controller surface + ledger of migrated vs. unmigrated DOM-mutation sites
enabled: true
tags: [frontend, chatInner, rendering, convention, ledger]
created: 2026-05-15T04:40:31Z
updated: 2026-05-15T04:40:31Z
---

# ConvView controller — surface and migration ledger

Step 3 of the unified chatInner rendering refactor (2026-05-15).
Companion to the `bilateral-msgid-and-data-msg-id` memory.

`static/js/conv_view.js` exposes `window.ConvView` — the only
intended mutator of `#chatInner`. It currently delegates to existing
ui.js primitives; later steps will move keyed diffing and
streaming-merge logic into it.

## Bundler position

`'conv_view.js'` sits **immediately after `ui.js`** in
`lib/js_bundler.py:_BUNDLE_FILES` and has a matching `<script defer>`
tag in `index.html` next to the `ui.js` tag. Depends on:
`renderMessage`, `_surgicalTruncateDOM`, `_convRenderFingerprint`,
`renderChat`, `_lastRenderedFingerprint` (ui.js), `_ensureMsgId`
(core.js).

## API surface

```js
ConvView.upsertMessage(convId, msg, {idx?, append?}) → bool
ConvView.removeMessage(convId, msgOrIdOrIdx)         → bool
ConvView.removeAfter(convId, cutoffIdx)              → bool
ConvView.replaceAll(convId)                          → bool
ConvView.finalizeStreaming(convId, msg, {removeIfTruncated?=true}) → bool
```

All methods are no-ops when `activeConvId !== convId` — caller is
responsible for the model-side mutation; ConvView only handles DOM.

`finalizeStreaming` centralizes:
- chatContainer scrollTop save/restore (the "thinking-block collapse"
  jump fix that was previously inlined in finishStream)
- Auto-removal when `msg` is no longer in `conv.messages` (the
  truncation-aware fallback for Edit/Regen mid-abort races)

`removeAfter` delegates to `_surgicalTruncateDOM` and falls back to
`renderChat` when the surgical path returns false (matches the
pre-existing `if (!_surgicalTruncateDOM(...)) renderChat(...)` idiom).

Identity lookup inside `_findMsgEl` prefers `[data-msg-id="..."]`
(via `CSS.escape`) and falls back to `getElementById('msg-' + idx)`.

## Migration ledger — MIGRATED (7 sites)

### Streaming finalize (5)
| File | Anchor | Was |
|---|---|---|
| `static/js/main.js:1268` | startAssistantResponse error path | `sm.outerHTML = renderMessage(assistantMsg, idx)` |
| `static/js/ui.js:5337` | `finishStream` main path | inline scroll-save + outerHTML |
| `static/js/ui.js:7204` | `endpoint_iteration` entering critic, finalize worker | inline outerHTML |
| `static/js/ui.js:7261` | `endpoint_iteration` stale-planner detection | inline outerHTML |
| `static/js/ui.js:7341` | `endpoint_planner_done` happy path | inline outerHTML |
| `static/js/ui.js:7401` | `endpoint_critic_msg` finalize | inline outerHTML |

### Truncate (2)
| File | Anchor | Was |
|---|---|---|
| `static/js/main.js:2132` | regenerateFromUser | `if (!_surgicalTruncateDOM(...)) renderChat(...)` |
| `static/js/ui.js:4101` | saveEditAndResend | same idiom |

## Migration ledger — STILL BYPASSING ConvView

These sites still mutate `#chatInner` directly. Each entry lists why
it was deferred — useful for prioritising follow-up steps.

### Single-message updates via `outerHTML = renderMessage(msg, idx)`
- `static/js/main.js:1480` — sendMessage initial user-bubble append
  (uses `insertAdjacentHTML('beforeend', ...)`; `ConvView.upsertMessage`
  with `append:true` is the natural target).
- `static/js/main.js:1548, 1632, 1830` — sendMessage pre-translate
  user-bubble re-renders.
- `static/js/main.js:2188` — regenerateFromUser surgical re-render.
- `static/js/ui.js:3886, 3989, 4086, 4157` — saveEditOnly /
  saveEditAndResend / saveEditOnly retry / cancelEditMessage.
- `static/js/ui.js:3973` — async PATCH error-revert handler.
- `static/js/ui.js:3744` — translation pipeline post-finish swap
  (re-export through ConvView would also help `static/js/translation.js:57`).
- `static/js/project.js:610` — project-mode message swap.

### `endpoint_planner_done` dangling-ref recovery (`ui.js:7355-7367`)
**Intentionally left inline.** The branch re-inserts `assistantMsg`
into `conv.messages` before rendering — promoting it into ConvView
would conflate two intents (DOM mutation + model recovery).
Revisit when the controller grows a `_recoverDanglingMsg` primitive.

### Streaming bubble creation — `inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(...))`
- `static/js/main.js:1158, 1201`
- `static/js/ui.js:5171-5175, 5541, 5845, 7224, 7299`

These create the live `#streaming-msg`. A future
`ConvView.startStreaming(convId, msgId, role, status?)` should own
both the HTML emission and the `data-msg-id` threading — currently
`_streamingBubbleHTML` accepts a `msgId` 4th arg but **no caller
passes it yet**.

### `_surgicalTruncateDOM` internals (`ui.js:686`)
Still walks `[id^="msg-"]` by numeric index. Migrating it to
`[data-msg-id]` is the cleanest unlock for stable identity but
requires audit of every consumer of the `msg-${idx}` id (including
turn-nav `data-msg-idx`, `scrollToTurn`, `copyMessage(idx)` button
onclicks emitted by `renderMessage`).

### branch.js / paper-reader.js
Out of scope. Branch mode renders into its own `branch-streaming-…`
regions; paper-reader uses a separate `_paperQAHistory`. Per the
design doc, they get their own controllers later.

## Behavioral parity notes

- `finalizeStreaming` logs `[ConvView]` on outerHTML failure (was
  `[finishStream]`). Adjust any log-grep alerts if needed.
- `ConvView.removeAfter` returns `true` even when it falls back to
  `renderChat` — slightly different from the original
  `if (!_surgicalTruncateDOM(...)) renderChat(...)` which returned
  the result of the surgical attempt. Consumers that ignore the
  return value (which is all of them) are unaffected.

## Verification

```bash
node --check static/js/conv_view.js
python3 -c "from lib.js_bundler import build_bundle; print(build_bundle())"
```

Bundle build at 2026-05-15 produced `bundle-2de93b9b.js` with 8
`ConvView.` call sites. After server restart, hard-refresh the
browser (bundle hash changes invalidate cache).

