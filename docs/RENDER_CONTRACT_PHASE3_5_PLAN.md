# RENDER_CONTRACT Phase 3.5 — make the DOM-apply layer single-seamed too

> **Status: DESIGN + tests-first RED skeleton only. NO production code touched in this commit.**
> Companion: [`RENDER_CONTRACT.md`](RENDER_CONTRACT.md) §1 Invariant 1 (`DOM = render(messages, rev)`),
> [`RENDER_CONTRACT_PHASE3_PLAN.md`](RENDER_CONTRACT_PHASE3_PLAN.md) (the message-document reducer).
> This plan closes the gap Phase 3 did NOT close: **Phase 3 unified the message-document
> projection (live/warm/cold/poll → `{content, thinking, toolRounds}`). It did NOT unify
> the DOM-apply layer.** `conv.messages` is the SSOT, but ~111 raw DOM writes still reach
> `#chatInner` without going through ConvView, so the rendered DOM is NOT yet a pure
> function of the message document.
>
> **Measurement note (2026-07-24):** raw-op counts come from the single-pass
> tokenizer in `tests/test_frontend_dom_seam_convergence.py::_scan_raw_dom_ops`
> (regex-stripping undercounts — it swallowed 22 of 23 ops in
> `main_send_pipeline.js` before the tokenizer replaced it).
>
> **Owner sign-off (2026-07-24):** Phase 3 is the long-term plan, not a quick win. This
> commit delivers the design + the per-site reclassification table + the failing-first
> byte-parity test skeleton. Boundary: this write-set (`docs/RENDER_CONTRACT_PHASE3_5_PLAN.md`,
> `tests/test_frontend_dom_seam_convergence.py`) is disjoint from CAS-baton / autopilot
> / conv_state_ssot — those three epics are explicitly OUT of scope here.

---

## 0. The verdict, in one sentence

ConvView is today the single seam for the **streaming-bubble lifecycle only**; the rest of
the DOM-apply layer is a second tier (`renderChat()` + a large fleet of raw DOM ops) that
still owns ~149 writes. Phase 3.5 makes the DOM apply layer single-seamed: every write to
`#chatInner` either (a) derives from a message-document change via `ConvView`, or (b) is
explicitly classified STRUCT-ONLY (scroll/window management) and allowed to stay, or (c) is
a PENDING placeholder that must NOT read message fields and therefore cannot diverge.

## 1. The three classes (the ONLY taxonomy this plan uses)

Every raw write lands in exactly one of:

| Class | Meaning | Rule |
|---|---|---|
| **CONTENT-DERIVED** | The DOM node *reads a message field* (content / thinking / error / images / igResult / translatedContent / _translatePartial / modifiedFiles / cost / _ctx). | MUST route through ConvView so the DOM is a pure projection of the message. Today raw → the byte-parity test (§3) catches every case where the live write and the cold re-projection diverge. |
| **STRUCT-ONLY** | Scroll / lazy-window / sentinel / placeholder management. The node reads NO message field — it only moves/evicts/inserts DOM slots for performance. | MAY stay raw — it cannot diverge from the SSOT because it never projects content. Explicitly allowlisted so the ratchet test (§4) doesn't flag it. |
| **PENDING-PLACEHOLDER** | A transient "we are waiting" bubble (translating / VLM-wait / image-gen loading). Reads NO message field; its content is a fixed status string. | MAY stay raw for now, BUT it must never read `msg.*`. The moment it shows any message-derived text it becomes CONTENT-DERIVED. Listed so the ratchet covers it. |

The confusion today is that these three classes are **interleaved inside the same functions**
(e.g. `_surgicalRerenderMsg` does BOTH a STRUCT-ONLY re-anchor AND a CONTENT-DERIVED
`el.outerHTML = renderMessage(...)`). Phase 3.5 splits them at the call site.

## 2. The 111-site reclassification table

`N = current raw count` measured 2026-07-24 by the §4 audit (single-pass tokenizer strips
comments + strings, then counts `innerHTML=` / `outerHTML=` / `insertAdjacentHTML(` /
`appendChild(` / `.remove()`).
ConvView itself has 8 raw ops (it IS the seam — those are the ALLOWED writes). Every other
file's raw ops are listed below with a class + a Phase-3.5 target.

### 2.1 `static/js/conv_view.js` — N=8 → THE SEAM (keep)

| Line cluster | Op | Class | Target |
|---|---|---|---|
| ~137 | `existing.outerHTML = html` (upsert) | SEAM | keep — this IS `upsertMessage` |
| ~164 | `el.remove()` (removeMessage) | SEAM | keep |
| ~226 | `inner.insertAdjacentHTML` (startStreaming) | SEAM | keep |
| ~272 | `sm.outerHTML = html` (finalizeStreaming) | SEAM | keep |
| +4 helper ops | evict / restore | SEAM | keep |

### 2.2 `static/js/ui/chat_render.js` — N=10 → the reconcile engine (CONVVERGE to ConvView.apply/replaceAll)

| Line | Op | Class | Target |
|---|---|---|---|
| 465 | `inner.innerHTML = html` (renderChat full) | SEAM-2 | fold into `ConvView.replaceAll` |
| 661 | `wrapper.innerHTML = renderMessage(msg, i)` (surgical) | SEAM-2 | fold into `ConvView.apply` |
| 674 | `upd.el.outerHTML = upd.html` (surgical) | SEAM-2 | fold into `ConvView.apply` |
| 686, 697 | `el.remove()` / `leftoverStreaming.remove()` | STRUCT-ONLY | keep — stale-node eviction |
| 755, 759, 787 | `inner.innerHTML = …` (skeleton / welcome / placeholder) | PENDING-PLACEHOLDER | keep — no message field read |

`renderChat`/`_surgicalTruncateDOM` are the SECOND seam (the render engine ConvView already
delegates to). Phase 3.5 does NOT rewrite them — it moves their call boundary INTO
`ConvView.apply` / `ConvView.replaceAll` so there is ONE public DOM entry point.

### 2.3 `static/js/ui/streaming_render.js` — N=21 → the live-write engine (CONVERGE to ConvView)

| Line | Op | Class | Target |
|---|---|---|---|
| 65, 189, 302 | `sm.remove()` (streaming bubble) | STRUCT-ONLY | keep — bubble lifecycle |
| 92 | `el.outerHTML = html` (`_surgicalRerenderMsg`) | CONTENT-DERIVED | `ConvView.apply(convId, idx, msg)` |
| 101, 103 | `insertAdjacentHTML` (VU / after streaming) | CONTENT-DERIVED | `ConvView.apply` (insert-after becomes an apply with an anchor) |
| 203 | `insertAdjacentHTML` (autopilot VU) | CONTENT-DERIVED | `ConvView.apply` |
| 660–662 | `_streamingBubbleHTML` data-msg-id | SEAM-2 | keep — bubble template |
| 714–715 | `el.remove()` (truncate) | STRUCT-ONLY | keep — `_surgicalTruncateDOM` |
| 862–873 | sentinel `s.remove()` / `appendChild` | STRUCT-ONLY | keep — lazy window |
| 891–903 | evict + sentinel `innerHTML`/`insertBefore` | STRUCT-ONLY | keep |
| 948–952, 1065–1069 | lazy-load fragment `innerHTML`/`after`/`before` | CONTENT-DERIVED | these render real messages → route the *message html* through `ConvView.apply`; the fragment insertion itself stays STRUCT-ONLY |
| 1006–1007 | `el.remove()` (evict above window) | STRUCT-ONLY | keep |

### 2.4 `static/js/ui/sse_pipeline.js` — N=17 → the live event apply (CONVERGE)

| Line | Op | Class | Target |
|---|---|---|---|
| 434, 884, 1333, 1416, 1561, 1619 | `insertAdjacentHTML(_streamingBubbleHTML…)` (ConvView fallback) | CONTENT-DERIVED | delete the raw fallback — ConvView is always present after Phase 3.5; keep only the `ConvView.startStreaming` call |
| 456 | `_body.innerHTML = _html` (resume partial) | CONTENT-DERIVED | `ConvView.apply` on the resumed assistant msg |
| 741, 1279, 1281 | planner avatar/body `innerHTML` | CONTENT-DERIVED | `ConvView.apply` on the planner msg |
| 872 | `staleRenderedEl.remove()` | STRUCT-ONLY | keep |
| 1379, 1649 | `existingSm.remove()` / `danglingSm.remove()` | STRUCT-ONLY | keep |
| 1474, 1480 | `sm.outerHTML = renderMessage(...)` (dangling planner) | CONTENT-DERIVED | `ConvView.apply` |
| 2041 | `_sm.remove()` (ConvView fallback) | STRUCT-ONLY | delete the raw fallback |

### 2.5 `static/js/main/main_send_pipeline.js` — N=23 → CONVERGE (user/error bubbles + VLM + queue bar)

| Line | Op | Class | Target |
|---|---|---|---|
| 446 | `chatInnerEl.insertAdjacentHTML(renderMessage(userMsg,…))` | CONTENT-DERIVED | `ConvView.apply` |
| 583, 705, 1040 | `msgEl.outerHTML = renderMessage(userMsg,…)` (server-translated user msg / VLM done) | CONTENT-DERIVED | `ConvView.apply` |
| 611, 639 | `msgEl.remove()` (steer/queue splice) | CONTENT-DERIVED | `ConvView.removeMessage` (the model-side splice already happened; DOM must follow) |
| 756 | `insertAdjacentHTML(renderMessage(errAssistant,…))` | CONTENT-DERIVED | `ConvView.apply` |
| 1011–1045 | VLM-wait indicator `appendChild` + `innerHTML` + `remove()` | PENDING-PLACEHOLDER | keep — fixed status string, no msg field |
| 230, 923, 1380, 1402 | `oldSm/_ghost.remove()` | STRUCT-ONLY | keep |

### 2.6 `static/js/main/main_regen_continue.js` — N=4 → CONVERGE

| Line | Op | Class | Target |
|---|---|---|---|
| 149 | `msgEl.outerHTML = renderMessage(msg, idx)` | CONTENT-DERIVED | `ConvView.apply` |
| 215 | `chatInnerEl.insertAdjacentHTML(renderMessage(errAssistant,…))` | CONTENT-DERIVED | `ConvView.apply` |
| 411 | `hdr.appendChild(tmEl)` (Continue elapsed timer) | STRUCT-ONLY | keep — chrome, no msg field |
| 417 | `bodyEl.innerHTML = …` (Continue zones) | CONTENT-DERIVED | `ConvView.apply` (the zone template is derived from the assistant msg) |

### 2.7 `static/js/main/main_translating_bubble.js` — N=6 → PENDING-PLACEHOLDER (keep, guarded)

| Line | Op | Class | Target |
|---|---|---|---|
| 37, 47 | `el.innerHTML` / `inner.appendChild(el)` (translating bubble) | PENDING-PLACEHOLDER | keep — fixed '翻译中…'/'连接中…' string |
| 55 | `el.remove()` | PENDING-PLACEHOLDER | keep |
| 74 | `insertAdjacentHTML` (streaming fallback) | CONTENT-DERIVED | delete raw fallback (ConvView always present) |

### 2.8 `static/js/ui/edit_message.js` — N=7 → CONVERGE

| Line | Op | Class | Target |
|---|---|---|---|
| 52 | `bodyEl.innerHTML = …edit form…` | CONTENT-DERIVED | `ConvView.apply` — render the *editor* as a projection of `msg` (content + attachments) |
| 143, 263, 279, 384, 484 | `msgEl.outerHTML = renderMessage(msg, idx)` | CONTENT-DERIVED | `ConvView.apply` |
| 550 | `insertAdjacentHTML(renderMessage(errAssistant,…))` | CONTENT-DERIVED | `ConvView.apply` |

### 2.9 `static/js/ui/translation_render.js` — N=18 → CONVERGE (the "translation lands but UI doesn't refresh" bug home)

| Line | Op | Class | Target |
|---|---|---|---|
| 64 | `el.outerHTML = renderMessage(msg, idx)` (`_renderMsgInPlace`) | CONTENT-DERIVED | `ConvView.apply` |
| 95, 100, 107, 112–113, 131, 137 | translate-loading patch (status / preview into `#translate-loading-N`) | CONTENT-DERIVED | these read `msg._translateStatus` / `_translatePartial` → the indicator becomes a projection of the msg; route via `ConvView.apply` (or keep as a msg-scoped sub-projection ConvView owns) |
| 184, 192 | streaming narration `innerHTML` | CONTENT-DERIVED | `_renderStreamingTranslatePreview` reads `msg._translatePartialByRound` → ConvView streaming-body seam |
| 212, 217, 232, 240, 277, 285 | settled per-round narration `innerHTML` | CONTENT-DERIVED | `_applyPartialByRoundToSettled` reads the msg → ConvView.apply |

This file is the single biggest CONTENT-DERIVED cluster and the direct cause of the
"translation lands but the UI doesn't refresh" bug family — because it writes DOM from
`msg._translate*` WITHOUT going through the same projection as a cold reload. Phase 3.5
makes its emit path (`emitMessageChanged`) call `ConvView.apply`, so live translation and
cold reload produce byte-identical DOM.

### 2.10 `static/js/image-gen.js` — N=18 + `image-gen-batch.js` — N=5 → mixed

| Line | Op | Class | Target |
|---|---|---|---|
| image-gen.js:311, batch:127 | `chatDiv.insertAdjacentHTML(loadingHtml/gridHtml)` | PENDING-PLACEHOLDER | keep — fixed loading markup |
| image-gen.js:364, 392, 418, batch:262 | `loadingEl.remove()` / `footerEl.remove()` | PENDING-PLACEHOLDER | keep |
| batch:173, 499, 526, 552, 569, 202, 241 | `slotEl.innerHTML = ig-result / ig-error` | CONTENT-DERIVED | reads `msg._igResult(s)` / `_igError` → the result card becomes a projection of the msg via `ConvView.apply` |
| image-gen.js:172, 188, 629, 672, 685 | model-picker dropdown `innerHTML` | NOT-#chatInner | out of scope (this is the picker UI, not a chat bubble) — exclude from the ratchet |

### 2.11 `static/js/ui/stream_lifecycle.js` — N=6 → mostly STRUCT

| Line | Op | Class | Target |
|---|---|---|---|
| 116 | `inner.innerHTML = html` (welcome / reset) | PENDING-PLACEHOLDER | keep |
| 250, 254, 366, 384 | `sm/_sm.remove()` (streaming bubble) | STRUCT-ONLY | keep |
| 608 | `insertAdjacentHTML` (queued-dispatch placeholder) | PENDING-PLACEHOLDER | keep |

### 2.12 `static/js/main/main_conv_lifecycle.js` — N=10 → mostly NOT-#chatInner / placeholder

| Line | Op | Class | Target |
|---|---|---|---|
| 53 | `topbarEl.innerHTML` | NOT-#chatInner | exclude (topbar, not a bubble) |
| 63 | `chatInner.innerHTML = welcome` | PENDING-PLACEHOLDER | keep |
| 548, 562, 568, 571 | toast `innerHTML`/`appendChild`/`remove` | NOT-#chatInner | exclude (toast overlay) |
| 696, 701, 715, 721 | modal overlay `innerHTML`/`appendChild`/`remove` | NOT-#chatInner | exclude (modal) |

### 2.13 `static/js/core/conversations.js` — N=4 → PENDING-PLACEHOLDER (keep)

| Line | Op | Class | Target |
|---|---|---|---|
| 1747, 1767, 2300 | `inner.innerHTML = …error welcome…` | PENDING-PLACEHOLDER | keep — fixed error string, no msg field |

### 2.14 Tally

| Class | Count | Phase-3.5 action |
|---|---|---|
| SEAM (conv_view.js) | 8 | keep — this IS the seam |
| SEAM-2 (chat_render reconcile) | ~7 | fold call boundary into `ConvView.apply`/`replaceAll` |
| CONTENT-DERIVED | ~85 | route through `ConvView.apply` — the byte-parity test (§3) is the guard |
| STRUCT-ONLY | ~28 | keep, explicitly allowlisted |
| PENDING-PLACEHOLDER | ~18 | keep, must never read `msg.*` |
| NOT-#chatInner | ~11 | excluded from class rules, still under the ratchet |
| **Total raw ops (tokenizer)** | **157 = 8 seam + 149 non-seam** | ~55 stay (seam+struct+placeholder) + **~85 converge to ConvView** |

## 3. The failing-first byte-parity test (the Phase-3 acceptance anchor, generalized to DOM)

`tests/test_frontend_dom_seam_convergence.py` — JSDOM harness.

**The claim under test:** for ONE logical turn, the DOM produced by folding the LIVE event
stream (delta → tool rounds → translation preview → done) MUST be byte-identical to the DOM
produced by the COLD reload of the same settled message.

```
LIVE  : events  → reduceStreamState → (today: raw DOM writes)      → liveHTML
COLD  : settled msg → renderMessage(msg, idx)                       → coldHTML
ASSERT: liveHTML === coldHTML        // byte-identical #chatInner subtree
```

**RED today** because the ~58 CONTENT-DERIVED raw writes project message fields through a
DIFFERENT code path than `renderMessage` — so e.g. the live translation-preview DOM
(`translation_render.js:184` `_renderStreamingTranslatePreview`) does NOT equal what a cold
`renderMessage` produces for the same `msg._translatePartialByRound`. The test fails on
exactly those divergences, one assertion per site-class.

**GREEN** when every CONTENT-DERIVED write routes through `ConvView.apply` (which calls
`renderMessage`), so live and cold share the single projection.

**NEUTER (committed, proves the test is load-bearing):** `test_NEUTER_raw_translation_preview_diverges`
— inside the harness, simulate the *current* behaviour: take the settled cold message, apply
a `_translatePartialByRound` via the *raw* `narr.innerHTML = …` path (the translation_render
idiom) instead of through `renderMessage`, and assert the two DOM strings DIFFER. This proves
the parity assertion is actually measuring the projection path, not vacuously passing. When
the fix lands (translation goes through ConvView.apply → renderMessage), the NEUTER's raw
path still diverges (by construction) but the REAL path now matches — so the main test flips
GREEN while the NEUTER stays as the standing "this is what divergence looks like" reference.

## 4. The ratchet guard (no NEW raw writes)

A second test in the same file statically audits the 14 files: strip comments + strings,
count the 5 raw-DOM op patterns, and assert each file's count is **≤ its §2 baseline**. This
is the monotonic-decrease ratchet (same pattern as `test_frontend_api_isolation.py`): the
baseline only ever goes DOWN as sites converge to ConvView; any NEW raw write fails CI.
Baselines (2026-07-24, tokenizer-measured): main_send_pipeline 23, streaming_render 21,
translation_render 18, image-gen 18, sse_pipeline 17, main_conv_lifecycle 10, chat_render 10,
conv_view 8 (seam — pinned, not ratcheted down), edit_message 7, stream_lifecycle 6,
main_translating_bubble 6, image-gen-batch 5, main_regen_continue 4, core/conversations 4.

## 5. Landing order (each step committable + tested; mirrors Phase 3 §7)

1. **This plan + failing-first test skeleton (RED).** [THIS COMMIT]
2. Add `ConvView.apply(convId, idx, msg)` as the single public entry that wraps
   `renderMessage` + `_evictByMsgId` + fingerprint update; re-route the highest-bug-density
   cluster first — `translation_render.js` (§2.9) — through it. Byte-parity test: translation
   assertion flips GREEN; ratchet drops ~18.
3. Route the send/regen/edit single-message swaps (§2.5/2.6/2.8) through `ConvView.apply`.
   Ratchet drops; more parity assertions flip GREEN.
4. Fold the `renderChat`/`_surgicalTruncateDOM` call boundary into `ConvView.replaceAll` /
   `ConvView.apply`; delete the per-call `window.ConvView` raw fallbacks (§2.4). Ratchet
   approaches the STRUCT/PENDING floor.
5. Declare the remaining STRUCT-ONLY + PENDING-PLACEHOLDER sites as the permanent allowlist;
   the ratchet baseline becomes the floor and the byte-parity test is fully GREEN.

## 6. Boundary

- **In scope:** `static/js/conv_view.js`, `static/js/ui/{chat_render,streaming_render,sse_pipeline,sse_handlers_*,stream_lifecycle,edit_message,translation_render}.js`, `static/js/main/main_{send_pipeline,regen_continue,translating_bubble}.js`, `static/js/{image-gen,image-gen-batch}.js`, `static/js/core/conversations.js`, `lib/js_bundler.py`.
- **Out of scope (explicitly):** CAS-baton / autopilot (`lib/tasks_pkg/autopilot*`),
  conv_state_ssot (`lib/agent_core/push.py`, `routes/push.py`, `conv_state_reducer.js`),
  `lib/agent_core/events.py` (Phase 3's wire contract — separate sign-off), and the DB
  (`_sync_*` writers — Phase 4). The VU render in `streaming_render.js:428-491` is listed
  here ONLY for its DOM-write classification; its baton semantics are untouched.
