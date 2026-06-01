---
name: export-internal-never-pushes
description: User policy: internal mode of export.py must NEVER push to GitHub; --push is silently disabled for internal
enabled: true
tags: [export, git, policy, internal]
created: 2026-04-17T09:32:12Z
updated: 2026-04-17T09:32:12Z
---

# Internal mode must NEVER push to GitHub

User policy (2026-04-17): `python3 export.py --mode internal` must never push
to any git remote. Internal mode is for local/self-use sharing only.

## Enforcement (two layers in export.py)

1. **CLI layer** (inside `main()`, right after `parser.parse_args()`):
   if `args.mode == 'internal' and args.push`, print a yellow warning and set
   `args.push = False`.

2. **`_git_push()` guard** (first check in the function): if `mode == 'internal'`,
   log and return immediately — safeguard against any programmatic caller.

## Do NOT

- Re-enable `--push` for internal mode.
- Add an internal entry to `_GIT_REPOS` that would push to GitHub.
- Suggest "use --push with internal" in docs/examples.

Existing `_GIT_REPOS['internal']` (tofu-meituan) is kept only for historical
reference; the guards above make it unreachable.

