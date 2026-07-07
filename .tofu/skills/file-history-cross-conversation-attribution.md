---
name: file-history-cross-conversation-attribution
description: fh cross-conv leak: backend serve paths conv+task scoped (Fixes 1-5); frontend fallback cache leaked — now keyed by OWNING MESSAGE (WeakMap _fcResultByMsg), not global fingerprint. Isolation by construction.
enabled: true
tags: [file-history, concurrency, orchestrator]
created: 2026-05-13T04:52:04Z
updated: 2026-07-03T03:57:51Z
---

# File-history / file-change cross-conversation misattribution

Two independent vectors cause "file edits from conv A shown on conv B".
Both must stay closed.

## VECTOR 1 — backend journal/fh side-channel (Fixes 1–5)
Authoritative `modifiedFileList` = this round's OWN journalled writes,
unioned across ALL roots (`project_path` + `projectPaths[1:]`), filtered
by `conv_id` then `taskId`. The fh `diff_name_status` diff is ENRICHMENT
ONLY, gated by `last_writer_task_id` (Fix 2) + `_project_lock` (Fix 3) +
drop-unattributed-drift (Fix 4) + OPAQUE-WRITER probe (Fix 5,
`_TRACKED_EDIT_TOOLS`). A 2026-07-03 audit re-confirmed every backend
serve path (derive_round_modified_files commit_round.py:79, chat_poll,
cold-replay, sync-to-conversation) is conv+task scoped. Backend is NOT
the residual leak.
Fix 3 root cause: `_run_commit_round_async` runs
get_last_snapshot_id→make_snapshot→diff_name_status; make_snapshot walks
project-wide tracked.json, so a concurrent conv's writer between prev_snap
and make_snapshot leaks into our diff. Wrap the region in
`_project_lock` (RLock). Tests: debug/test_file_history.py (34),
debug/test_concurrent_fh_attribution.py (Task-A..G).

## VECTOR 2 — FRONTEND file-changes-bar cache (2026-07-03) ⚠️ the residual one
Only bites messages LACKING a server `modifiedFileList` (the
extract-from-toolRounds FALLBACK path: mid-stream / project-tracking-off).
Derivation LOGIC is already backend-SSOT (`lib/tool_changes.py` via
`POST /api/v1/messages/extract-file-changes`; frontend never re-parses).
The leak was in the RESULT CACHE in static/js/ui/finish_info.js.

### Two-part fix
**Part 1 (faithful key):** `_fcFingerprint` was COARSE
(`roundCount:lastStatus:lastToolName:lastResultCount`) → two same-shape
messages in different convs minted the SAME key. Rewrote it
CONTENT-FAITHFUL: walk ALL write-capable rounds
(`write_file/apply_diff(s)/insert_content(s)/run_command`), project ONLY
the fields the extractor consumes: `toolArgs.path` + `edits[].path` (NOT
full toolArgs — write_file `content` must never bloat the key), plus
`results[0]`'s `fileChanges`/`writeOk`/`badge`/`title`.

**Part 2 (isolation by CONSTRUCTION — the real fix, after owner pushback
"is this still not backend SSOT?"):** the faithful hash CLOSED the leak
but isolation still rested on a collision-free hash in a PROCESS-GLOBAL
Map. Replaced the global `_extractedFileChangesCache` (fp→files) with
`_fcResultByMsg` = WeakMap<msg, {fp, files}> — the cached list lives ON
THE OWNING MESSAGE OBJECT. A message reads only ITS OWN entry (and only
if the stored fp still matches its current toolRounds = staleness check),
so cross-conversation leakage is IMPOSSIBLE BY CONSTRUCTION, not avoided
by a good hash. The fingerprint demotes to: (a) per-message staleness
token, (b) request-dedup key for `_extractedFileChangesPending`
(fp→Promise) which holds ONLY in-flight Promises, never a rendered result
(identical input ⟹ identical output ⟹ safe to coalesce).
Signatures: `_extractFileChangesFromRoundsAsync(rounds, msg)` and the
batch prefetch write per-owning-message (re-check each msg's fp on
landing so a mid-flight mutation never mis-stamps);
`_extractFileChangesFromRoundsCached(msg)` reads owner-scoped. Call
sites pass `msg`: `renderFileChangesBar`, streaming `updateStreamingUI`.

### Test — tests/test_frontend_fc_fingerprint_isolation.py (jsdom)
Asserts faithful-key distinctions AND the structural guarantee: two
DISTINCT msg objects with BYTE-IDENTICAL toolRounds (genuine fp
collision) do NOT share a cache entry (clone reads null, not the
original's list). Double-neuters (both proven to bite, restored
byte-identical):
  - revert to coarse key → `distinct_files_distinct_fp` FAILS.
  - reintroduce global content-keyed lookup →
    `clone_not_leaked_from_original` FAILS.
No test pins `_fcFingerprint` output (all harnesses stub it `() => 0`).

## Guardrail / lesson
"Make the hash faithful" is a PATCH; "scope the cache to its owner" is
the FIX. When a cache holds per-entity data, key it by the ENTITY
(object identity / WeakMap), not a content fingerprint — a hash collision
then degrades to a harmless miss, not a cross-entity leak. Reserve
content-fingerprint keys for caches whose value is a pure function of
that content AND are safe to share (e.g. an in-flight request-dedup map).
When hunting a cross-conversation display leak, audit global FRONTEND
caches too, not just backend serve paths.

## Invariants
- Backend AUTHORITATIVE list = round's own journalled writes across ALL
  roots; fh diff never seeds/replaces it. Empty-`last_writer_task_id`
  paths kept ONLY on opaque-writer rounds. `_project_lock` is an RLock.
- Frontend fallback cache is OWNER-SCOPED (WeakMap keyed by msg). The
  fingerprint is a staleness/dedup token, never a cross-conv cache key.

