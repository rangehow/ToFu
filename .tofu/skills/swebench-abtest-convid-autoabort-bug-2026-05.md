---
name: swebench-abtest-convid-autoabort-bug-2026-05
description: When submitting parallel /api/chat/start tasks without explicit convId, the manager auto-aborts the first one as 'superseded' — use unique convId per arm
enabled: true
tags: [swebench, concurrency, bug-pattern, manager, auto-abort]
created: 2026-05-06T15:28:06Z
updated: 2026-05-06T15:28:06Z
---

# Pattern: empty convId + concurrent submit → AUTO-ABORT

When `/api/chat/start` is called with `messages` but no `convId`, the
task's convId is empty string. If a SECOND submit arrives before the
first finishes, the manager sees both as "conv=" and treats the second
as the replacement, immediately aborting the first:

```
[Task c5a20d9f] conv= ⚠️ AUTO-ABORTED: superseded by new task ? — content=0chars elapsed=0.0s
...
[Chat] Poll c5a20d9f ⚠️ RETURNING EMPTY RESULT — task is done but has no content or thinking! finishReason=aborted
```

Symptom: task returns `status=done, rounds=0, content=0chars,
finishReason=aborted` almost instantly. Looks like a quiet success;
harness sees patch_size=0 and calls it a model failure.

## Fix for A/B / sample-of-N harnesses
Always pass an explicit unique `convId` per submission, e.g.:
```python
conv_id = f'swebench-abtest-{instance_id}-{tool}-{arm}-{int(time.time())}'
body = {'messages': [...], 'convId': conv_id, 'config': {...}}
```

## Affected
- `debug/swebench_abtest_minimal_edit_prompt.py` — fixed.
- `debug/swebench_capture_conversation.py` — works serially so never hit
  this, but should also be updated for safety if run concurrently.
- `debug/swebench_runner.py` — main SWE-bench runner passes no convId but
  it only submits ONE task per workspace via the worker pool, and the
  orchestrator assigns conv to the returned task_id. Verify this before
  relying on it.

