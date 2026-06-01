---
name: chatui-export-push-workflow
description: Export and git push workflow: multi-remote (rangehow/ToFu + NiuTrans/ToFu), token location, CLI flags, version bumping
enabled: true
tags: [export, git, deployment]
created: 2026-04-06T07:37:50Z
updated: 2026-04-09T15:51:23Z
---

# Tofu Export & Push Workflow

## Token Location
GitHub token is hardcoded in `export.py` line ~1048 as `_GH_TOKEN`. Update it there when token rotates.

## Repos (Multi-Remote)
- **opensource**: Pushes to **two** remotes simultaneously:
  - `origin` → `github.com/rangehow/ToFu.git` (branch `main`)
  - `niutrans` → `github.com/NiuTrans/ToFu.git` (branch `main`)
- **internal**: Single remote:
  - `origin` → `github.com/rangehow/tofu-meituan.git` (branch `master`)

Config uses `'remotes'` list (not single `'remote'` key):
```python
_GIT_REPOS = {
    'opensource': {
        'remotes': [
            {'name': 'origin',   'url': f'https://{_GH_TOKEN}@github.com/rangehow/ToFu.git'},
            {'name': 'niutrans', 'url': f'https://{_GH_TOKEN}@github.com/NiuTrans/ToFu.git'},
        ],
        'branch': 'main',
    },
    ...
}
```

## Default Export Destinations
- opensource → `../tofu-open/`
- internal → `../tofu-meituan/`

## Typical Commands

```bash
# Opensource with version bump (pushes to BOTH rangehow/ToFu and NiuTrans/ToFu)
python3 export.py --mode opensource --push --bump minor -m 'description'

# Internal (no bump needed, just sync)
python3 export.py --mode internal --push -m 'v0.6.0: description'
```

## Version Bumping
- `--bump minor` for new features (0.5.2 → 0.6.0)
- `--bump patch` for bug fixes (0.6.0 → 0.6.1)
- `--bump major` for breaking changes
- Only the first export (opensource) should use `--bump`; internal just references the version

## Amend + Force-Push for CI Fixes
When fixing a CI failure without wanting a new commit in history:
1. Re-export: `python3 export.py --mode opensource --dest ../tofu-open`
2. `cd ../tofu-open && git add -A && git commit --amend --no-edit`
3. Move tag if needed: `git tag -d v0.6.1 && git tag v0.6.1`
4. `git push --force origin main --tags && git push --force niutrans main --tags`

## Notes
- `.git` and `data/` are preserved across re-exports for incremental commits
- Post-export runs ruff auto-fix and secret scan automatically
- Tags (v0.6.0) are only created when `--bump` is used
- Timeout should be ≥300s for the push command
- Commit is created once, then pushed to each remote in sequence
- If one remote fails, the others still get pushed (each push is independent)

## Bug: `tools/` dir name collision (fixed 2026-04-06)
- `export.py` excluded ALL dirs named `tools` (meant for top-level `tools/` md2cards project)
- This also excluded `lib/tools/` — a core package with tool definitions
- `.gitignore` had same issue: bare `tools/` matched `lib/tools/`
- Fix: `_TOP_LEVEL_ONLY_EXCLUDE_DIRS` in export.py + `/tools/` (anchored) in .gitignore

