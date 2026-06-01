---
name: run-command-exit-code-first-match-bug
description: Bug fix: run_command exit code parsed with re.search (first match) picks up nested [exit code: 1] from command stdout instead of the real exit code appended at end — must use end-anchored regex with \s*$
enabled: true
tags: [python, regex, bug-fix, run-command, exit-code, first-match, anchoring]
created: 2026-03-31T15:29:51Z
updated: 2026-03-31T15:29:51Z
---

# run_command Exit Code First-Match Bug

## Problem
`tool_run_command()` appends the real exit code at the **end** of its output:
```
$ python run_tests.py
Test A: [exit code: 1]   ← from command's own stdout
All tests passed.

[exit code: 0]            ← the REAL exit code
```

Three parsing locations used `re.search(r'\[exit code: (\d+)\]', tool_content)` which matches the **first** occurrence — picking up the nested `[exit code: 1]` from the command's own output instead of the actual `0` at the end.

## Affected Files
1. `lib/tools/meta.py` → `_build_run_command()` — UI metadata/badge
2. `lib/tasks_pkg/executor.py` → `_handle_code_exec()` — executor metadata

## Fix
Anchor the regex to end of string and support negative exit codes:
```python
# ❌ OLD — matches first occurrence
m = re.search(r'\[exit code: (\d+)\]', tool_content)

# ✅ NEW — anchored to end, supports negative codes (signal kills)
m = re.search(r'\[exit code: (-?\d+)\]\s*$', tool_content)
```

Also update the cleanup regex in both files:
```python
# ❌ OLD
output_text = re.sub(r'\n?\[exit code: \d+\]$', '', output_text).strip()

# ✅ NEW
output_text = re.sub(r'\n?\[exit code: -?\d+\]\s*$', '', output_text).strip()
```

## Symptoms
- Command shows `✗ exit 1` badge even though the command succeeded (exit 0)
- Happens when the command's stdout/stderr contains `[exit code: N]` text from:
  - Nested subprocess output
  - Test runners logging sub-test exit codes
  - CI/CD pipelines
  - Another chatui tool's run_command output being piped

