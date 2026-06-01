---
name: system-prompt-trimmed-by-mode-2026-05-20
description: System prompt now scales by mode: bare chat 6KB, tools 8KB, project+tools 12KB. SWE-shaped content gated on project_enabled via is_code_context.
enabled: true
tags: [system-prompt, privacy, claude-code-port, prompt-cache, is_code_context]
created: 2026-05-20T05:24:21Z
updated: 2026-05-20T05:43:21Z
---

# System Prompt Trimming — 2026-05-20

## Goal
Original Claude Code-shaped prompt was ~14 KB **regardless** of whether the
user was doing code or not. For translation / paper Q&A / daily report /
trading turns this was both wasteful (cache pressure, prompt cost) and
**counterproductive** (SWE framing biased the model toward code-shaped
answers, and the cwd bullet leaked the server's runtime path).

## New Shape (all in `lib/tasks_pkg/system_prompt_cc.py`)

### Three-axis gating
1. `cwd` truthiness — `Primary working directory:` and `Is a git
   repository:` bullets dropped when empty.
2. `has_real_tools` — `Shell:`, `OS Version:`, `# Using your tools`,
   `# Function Result Clearing`, summarize-tool-results all dropped
   when no tools.
3. **NEW** `is_code_context` (= `project_enabled`) — gates SWE-bench
   shaped material:
   - `section_intro()`: "software engineering tasks" → "your tasks"
   - `section_doing_tasks()`: ~12 code-hygiene bullets dropped (OWASP,
     "Don't add features", "no comments unless WHY", file/PR comment
     conventions, code-specific verification language, backwards-compat
     hacks). Universally-useful bullets (capability, judgment, time
     estimates, retry policy, faithful reporting, verify-before-claim)
     always ship.
   - `section_actions()`: git/CI/Slack/PR examples → generic destructive
     / shared-state / third-party-upload examples
   - `section_tone_and_style()`: drops `file_path:line_number` and
     `owner/repo#123` bullets

### Outright deletions (any mode)
- `section_notes()` — every bullet was either duplicated (no-emojis,
  no-colon-before-tool-call) or code-only (absolute paths, code snippets).
- `Powered by model X` bullet — chatui's internal aliases (e.g.
  `aws.claude-opus-4.7`) are misleading when sent to non-Claude vendors.
- `anthropics/claude-code#100` example string.
- Auto-compaction sentence in `section_system_reminders()` (kept in
  `section_system()` only).
- Vendor build suffix in `OS Version:` — `Linux 4.18.0-147.mt20200626.…`
  becomes `Linux 4.18.0` via `_short_os_version()`.
- Permission-mode bullet in `section_system()` — chatui only has a narrow
  write-file approval, not Claude Code's full ask/auto/plan modes.

## Wiring
`lib/tasks_pkg/system_context.py` `_inject_system_contexts` passes
`is_code_context=project_enabled` to `build_static_prompt()`.

## Measured Sizes (from build_static_prompt())
| Scenario | Bytes |
|---|---|
| project OFF, no tools (chat/translation) | ~6500 |
| project OFF, tools ON (paper Q&A, search) | ~8400 |
| project ON, tools ON (code copilot) | ~12500 |

## Tests
`tests/test_cc_alignment.py` (29) + `tests/test_compaction_improvements.py`
(104) + `tests/test_streaming_and_prefetch.py` (33) — all 166 green
with no test-side changes needed. The default `is_code_context=True`
keeps back-compat for any caller that doesn't pass the flag.

## Pitfalls / Gotchas
- `model` parameter is now ignored by `section_environment` but kept in
  the signature so callers don't break. Don't rely on it being rendered.
- `is_code_context` defaults to True in `build_static_prompt()` for
  back-compat. The only real caller (`_inject_system_contexts`) passes
  the actual `project_enabled` value.
- General bullets in `section_doing_tasks()` are split into
  `_DOING_TASKS_GENERAL` (always ships) vs `_DOING_TASKS_CODE_ONLY`
  (project mode only). Order is preserved by interleaving in
  `section_doing_tasks()` to match the historical sequence.

## How to verify
```python
from lib.tasks_pkg.system_prompt_cc import build_static_prompt
a = build_static_prompt(cwd='', is_git=False, model='X',
                        has_real_tools=False, is_code_context=False)
assert 'OWASP' not in a            # code bullets gone
assert 'Primary working directory' not in a   # cwd gone
assert 'Notes:' not in a           # Notes block gone
assert 'powered by the model' not in a       # model bullet gone
```

