---
name: export-branch-isolation
description: export.py auto-isolates non-default branches to separate dirs (tofu-open--{branch}) to prevent .git history contamination
enabled: true
tags: [export, git, branching]
created: 2026-04-04T11:02:18Z
updated: 2026-04-04T11:02:18Z
---

# Export Branch Isolation

## Problem
When using `export.py --push --branch X` for different branches, they all exported to the same
directory (e.g. `../tofu-open/`) and shared the same `.git`. This caused:
- Commit history contamination between branches
- Force push from one branch overwrites another branch's history
- PRs and previous commits get wiped

## Solution (2026-04-04)
`export.py` now auto-appends `--{branch}` to the export directory name when the branch
differs from the repo's default:

```
--mode opensource (default branch=main) → ../tofu-open/
--mode opensource --branch cli_switch   → ../tofu-open--cli_switch/
--mode opensource --branch dev          → ../tofu-open--dev/
--mode internal (default branch=master) → ../tofu-meituan/
--mode internal --branch staging        → ../tofu-meituan--staging/
```

Each branch gets its own `.git` directory, completely independent histories.

## Usage
```bash
# Push to main (from tofu/ project, no cli_switch features)
cd tofu && python3 export.py --mode opensource --push -m 'message'

# Push to cli_switch (from chatui/ project, with cli_switch features)
cd chatui && python3 export.py --mode opensource --push --branch cli_switch -m 'message'
```

## Push Safety
- Normal `git push` is tried first
- If rejected, tries `git push -u` (new branch)
- Force push only as last resort, with explicit yellow warning

