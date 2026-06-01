---
name: compaction-tolerates-malformed-tool-call-args
description: LLMs emit bare-string read_files specs; compaction scans must tolerate it
enabled: true
tags: [compaction, tool-calls, defensive, bug-pattern]
created: 2026-04-19T16:06:32Z
updated: 2026-04-19T16:06:32Z
---

# Compaction must tolerate malformed tool_call args

## Pattern
LLMs (esp. Claude Opus) sometimes emit `read_files` with bare strings:
```json
{"reads": ["path/to/file.py"]}        // wrong but happens
{"reads": [{"path": "path/to/file.py"}]}  // correct shape
```

`lib/project_mod/tools.py::execute_tool` (~line 1560) already
normalises bare strings → dicts before calling `tool_read_files`, so
**the tool call itself succeeds**. The trap is any OTHER code that later
re-scans `message['tool_calls']` expecting dict specs.

## Crash site (fixed 2026-04-19)
`lib/tasks_pkg/compaction.py::_extract_recently_accessed_files`
called `spec.get('path', '')` on a bare string → AttributeError →
propagated out of `compute_turn_attachments` → `orchestrator.py` line
~701 → killed task at round 50 with `status=error`.

Conv `mo5adw4vyfm6qf` task `419ea938` hit this after 49 successful rounds.

## Fix
Both layers:
1. `_extract_recently_accessed_files` handles `isinstance(spec, str)`
   + uses `args.get(...) or []` for edit lists.
2. Wrap `compute_turn_attachments` in orchestrator with try/except —
   attachment building is advisory, must not abort a healthy task.

## Rule of thumb
Anywhere we walk `msg['tool_calls'][i]['function']['arguments']` after
it's been JSON-parsed, assume the model produced ANY shape. Check
`isinstance(..., dict)` before `.get()`. Never let advisory
post-processing (attachments, stats, metadata) crash the main loop.

