---
name: endpoint-planner-multimodal-content-typeerror
description: Endpoint planner crashed on multimodal user content (list vs str) — fix with isinstance branching like attachments.py
enabled: true
tags: [python, bug-fix, endpoint, planner, multimodal, images, type-error]
created: 2026-04-24T08:52:14Z
updated: 2026-04-24T08:52:14Z
---

# Endpoint planner multimodal user-content TypeError

## Symptom
`TypeError: can only concatenate str (not "list") to str` in
`lib/tasks_pkg/endpoint_review.py:_run_planner_turn` when user sends a
message with image/file attachments in endpoint mode. Crashes the whole
run_endpoint_task thread; `_store_endpoint_turns_on_task` never fires so
the auto-translate helper also logs "No endpoint_turns".

## Root cause
`_run_planner_turn` wrapped the last user message's content by doing
`wrapper_prefix + original_content`. When attachments are present the
message content is a **list of blocks** (`[{type:'text',text:...},
{type:'image_url',...}]`), not a string.

## Fix (lib/tasks_pkg/endpoint_review.py ~lines 60-125)
1. Hoist the wrapper prefix into `_PLANNER_WRAPPER_PREFIX` constant.
2. Branch on `isinstance(raw_content, list)`:
   - **list**: prepend `{'type':'text','text': _PLANNER_WRAPPER_PREFIX}`
     to a list(original_blocks); preserves image blocks so the planner
     can still see attached images.
   - **empty list**: fall back to string path with `''`.
   - **str (default)**: `_PLANNER_WRAPPER_PREFIX + original_content` —
     byte-identical to the pre-fix output so the prefix cache stays hot.
3. Add `logger.info('[Planner] multimodal user content detected (%d blocks)')`.

## Canonical references for the str/list branching idiom
- `lib/tasks_pkg/attachments.py:inject_attachments` (lines 210-220)
- `lib/tasks_pkg/model_config.py` lines 64-70
- `lib/tasks_pkg/cache_tracking.py` line 123
- `lib/tasks_pkg/conv_message_builder.py` line 639

## Regression test
`debug/test_endpoint_multimodal_planner.py` — monkey-patches
`_run_single_turn`, verifies str, list-with-image, and empty-list paths
all succeed and preserve expected content shape.

## Related safe sites (no fix needed)
- `_build_worker_directive` / `_reset_worker_messages_with_plan` in
  endpoint.py — take planner output (always str) and rebuild the user
  message from scratch, so no list concat happens there.
- `_run_critic_turn` — appends a fresh new user message; never mutates
  existing user content.

