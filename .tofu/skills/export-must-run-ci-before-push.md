---
name: export-must-run-ci-before-push
description: Always run ruff lint + pytest unit tests locally on exported code BEFORE pushing to GitHub; CI failure requires force-push fix
enabled: true
tags: [export, ci, ruff, testing, workflow]
created: 2026-04-09T19:13:10Z
updated: 2026-04-09T19:13:10Z
---

# Export: Run CI Locally Before Push

## Rule
Before pushing exported code to GitHub, ALWAYS run the CI checks locally first:

```bash
cd /path/to/tofu-open
ruff check lib/ routes/ tests/        # Must pass with 0 errors
python -m pytest -m unit --tb=short -q  # Must pass all tests
```

## If CI fails after push
1. `git reset --soft HEAD~1` — undo the commit but keep changes staged
2. `git tag -d vX.Y.Z` — delete the tag
3. Fix the issue in the source repo
4. Re-export: `python export.py --mode opensource --dest ../tofu-open`
5. Verify locally: `ruff check` + `pytest`
6. `git add -A && git commit -m "..." && git tag vX.Y.Z`
7. Force push to ALL remotes: `git push origin main --force && git push origin vX.Y.Z --force`
8. Repeat for each remote (niutrans, etc.)

## Common lint issues
- F401: Unused imports — add `# noqa: F401` if import is intentional (side effects)
- The export pipeline runs `ruff --fix` automatically, but some issues (like missing noqa comments in source) survive

