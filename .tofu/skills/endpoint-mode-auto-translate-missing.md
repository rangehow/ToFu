---
name: endpoint-mode-auto-translate-missing
description: Auto-translate safety net misses messages persisted OFF the main assistant finalize (endpoint-critic + autopilot VU turns): wire _maybe_auto_translate_* at each separate persist site (fwd) AND one-shot backfill (vu_translate_backfill.py + _migrate_backfill_vu_translations.py) that delegates to the same wire; writes translatedContent not originalContent
enabled: true
tags: [javascript, python, translation, endpoint, bug-fix, auto-translate, race-condition, cas, parallel-writes, finishStream, safety-net, critic, save_conv, overwrite]
created: 2026-04-21T04:42:42Z
updated: 2026-07-10T00:44:53Z
---

## Symptom
An assistant-like reply is never auto-translated even though `autoTranslate` is ON — only translated if a viewer happens to fire a manual `/api/translate`. Happens for message classes persisted on a code path SEPARATE from `manager._sync_result_to_conversation` (which owns the assistant safety net).

## The pattern (recurring)
`manager._sync_result_to_conversation → _maybe_auto_translate_assistant` (lib/tasks_pkg/auto_translate.py) is the server-side safety net, but it ONLY fires for the normal assistant finalize. Any turn persisted elsewhere needs an EXPLICIT wire at its own persist site. Two instances found + fixed:

### 1. Endpoint-mode critic (role=user + `_isEndpointReview`)
Fixed via `endpoint._trigger_endpoint_auto_translate` → `_maybe_auto_translate_critic` (delegates to `_maybe_auto_translate_assistant`, role-agnostic at commit layer).

### 2. Autopilot VU turns (role=user + `_isVirtualUser`) — fixed 2026-07-10
`autopilot._append_vu_message_to_conv` persists VU turns on a path with ZERO `_maybe_auto_translate_*` calls → every VU turn untranslated. Confirmed on conv mre58lxth33ncr (msg2 only via manual translate because on-screen; msg4 nothing).

**Forward fix:** new `_maybe_auto_translate_vu(conv_id, vu_msg_id, content)` in autopilot.py, called at the VU-append success site in `maybe_run_autopilot`. It re-reads persisted messages, resolves the row INDEX by `m.get('_msgId') == vu_msg_id` (authoritative — NEVER positional), delegates to `_maybe_auto_translate_assistant(conv_id, content, vu_idx, db=db)`, passes **NO `task`** (parent task's `_assistantMsgId`+accumulator belong to the ASSISTANT turn), best-effort. `_run_autopilot_kick` needs NO separate wire (calls `maybe_run_autopilot`).

## Forward-wire is only HALF — you MUST also backfill already-persisted rows
Owner pushback: the forward wire fixes NEW turns; a conversation named in the objective already has its VU turn on disk untranslated → still broken. Backfill (mirrors `segments_backfill` REUSE pattern):
- **Shared pure collector** `lib/conversations/vu_translate_backfill.py::collect_untranslated_vu_turns(messages)` → `[{idx,msgId,content}]` for `role=user`+`_isVirtualUser` rows with non-empty `content` but no `translatedContent`. Pre-filter ONLY (no language/settings gate — those belong to the safety net).
- **Migration** `tests/_migrate_backfill_vu_translations.py` (dry-run default; `--id`/`--limit`/`--apply`) uses the collector then DELEGATES each hit to the SAME `_maybe_auto_translate_vu` → the safety net's gates run verbatim; NO gate/raw-write of its own. The safety net spawns a daemon thread, so `--apply` POLLS the row until `translatedContent` lands (`--settle-timeout` default 120s) to report a VERIFIED result. Idempotent (collector skips rows with `translatedContent`).
- **Ran on mre58lxth33ncr:** dry-run flagged idx=4; `--apply` committed 1227 chars; re-run = 0 (idempotent).

## Idempotency vs frontend manual translate
Safe to double-fire: safety-net gates — `resolve_auto_translate(settings)` off, `is_predominantly_chinese`, existing `translatedContent`, `claim_inflight(conv_id, msgId, idx)` dedup keyed by stable `_msgId`.

## VU/critic bilingual wiring is INVERTED — writes translatedContent NOT originalContent
DISPLAY-translated: `content` = model-language original (原文 toggle), `translatedContent` = UI-language outer 译文 bubble. Opposite of a normal user msg (`originalContent`=源文, `content`=English-for-model). `_do_translate(..., field='translatedContent', targetLang='Chinese')` → `_commit_translation_to_db` sets `msg['translatedContent']`. ACCEPTANCE CHECK: after backfill, msg4 had translatedContent=1227(zh), `originalContent` ABSENT — proves it did NOT corrupt into the user→English path.

## Tests
`tests/test_autopilot_vu_auto_translate.py` (4): real append+`_maybe_auto_translate_vu` vs fake DB; enqueued once at resolved index (not guessed), db threaded, `task=None`; missing-`_msgId` fires nothing; source-order guard; in-memory NEUTER (`tests._nc_harness.neutered_source`, read-only → no `_NC_GUARDED_SOURCES` entry) breaking the resolver makes it MISS. `tests/test_vu_translate_backfill.py` (6): collector selection + skips + reuse-guard + NEUTER dropping the `translatedContent` skip → translated row re-flags.

## Deploy-gap sibling lesson (symptom 2 in the same report)
"Translation/render still broken" can be a DEPLOY gap not a code bug: an uncommitted frontend fix with NO `static/js/bundle-*.js` on disk means the custom no-hot-reload bundler never shipped it. `build_bundle()` + `get_styles_filename()`; VERIFY by curl-ing the SERVED bytes on :15000 (`GET /` tag + `curl static/js/bundle-<hash>.js | grep <symbol>`), not just the file on disk.

## Guardrail
Any message class persisted OUTSIDE `_sync_result_to_conversation` (autopilot VU, endpoint critic, future) is INVISIBLE to the safety net → (a) wire `_maybe_auto_translate_assistant` at that path's persist site (resolve index by `_msgId`, `task=None` unless the accumulator is that same turn's), AND (b) ship a one-shot backfill that REUSES the same wire for already-persisted rows. Forward-wire + backfill are the two halves.

