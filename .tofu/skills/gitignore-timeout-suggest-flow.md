---
name: gitignore-timeout-suggest-flow
description: grep timeout footer + /api/project/gitignore/{suggestions,accept,dismiss} flow — never silent, .gitignore writes only on explicit POST
enabled: true
tags: [search, gitignore, timeout, convention]
created: 2026-05-08T07:41:02Z
updated: 2026-05-08T07:41:02Z
---

# Gitignore Timeout-Suggest Flow

User explicitly approved this on 2026-05-08, reconciling with the prior
`grep-find-timeout-robustness-improvements` directive (no silent runtime
exclusion). The flow is suggestion-based — the user must accept before any
.gitignore write.

## Module
`lib/project_mod/gitignore_suggest.py`
- `record_timeout_and_probe(base, reason)` — depth-1 scan with 5 s budget,
  ranks subdirs by entry count, registers top 2 dirs that are >= 1000
  entries AND not already in .gitignore AND not in `_SOURCE_DIR_WHITELIST`
  (lib/src/routes/static/...). 24 h TTL, max 5 per project.
- `format_footer(suggestions)` — short string appended to the timeout
  message returned to the model.
- `accept_suggestions(base, dirs)` — only writes dirs already in the
  registry (defense vs. arbitrary input). Appends a dated header
  `# ── Auto-added by grep_search timeout probe on YYYY-MM-DD ──`
  followed by `dir/` entries. Skips already-present dirs. Audit-logged
  via `audit_log('gitignore_auto_added', ...)`.
- `dismiss_suggestions(base, dirs)` — drops registry entries.

## Hook points (read_tools.py)
`_run_rg` and `_run_gnu_grep` timeout branches call
`record_timeout_and_probe(base, reason='rg_timeout'|'grep_timeout')` and
append `format_footer(...)` to the existing "Grep timed out…" message.

## Endpoints (routes/project.py)
- `GET  /api/project/gitignore/suggestions?projectPath=...`
- `POST /api/project/gitignore/accept   {projectPath, dirs:[...]}`  (rate-limited 10/min)
- `POST /api/project/gitignore/dismiss  {projectPath, dirs:[...]}`

## Why this respects the prior "no silent exclusion" rule
- Searches behave EXACTLY as before until the user opts in.
- Footer only adds informational text to the timeout message.
- .gitignore is the user-owned, VCS-visible mechanism — every write is
  visible in `git status` and trivially reversible.

## DO NOT
- Add a runtime `_RUNTIME_SLOW_DIRS` set that silently excludes dirs
  from rg/grep — user has rejected silent exclusion twice.
- Auto-accept suggestions without user click.
- Write to anything other than `.gitignore` from accept_suggestions.

