---
name: chatui-export-push-workflow
description: Export and git push workflow: multi-remote, token, CLI flags, version bumping; ruff-strips-reexports trap, tar-overlay-never-deletes, debug/ excluded, internal-only test guards
enabled: true
tags: [export, git, deployment]
created: 2026-04-06T07:37:50Z
updated: 2026-07-02T09:00:00Z
---

# Tofu Export & Push Workflow

## Token Location
GitHub token is hardcoded in `export.py` line ~1048 as `_GH_TOKEN`. Update it there when token rotates.

## Repos (Multi-Remote)
- **opensource**: Pushes to **two** remotes simultaneously:
  - `origin` → `github.com/rangehow/ToFu.git` (branch `main`)
  - `niutrans` → `github.com/NiuTrans/ToFu.git` (branch `main`)
- **internal**: tofu-meituan, but push is DISABLED by policy (never pushes).

## Default Export Destinations
- opensource → `../tofu-open/`
- internal → `../tofu-meituan/` (local only, no push)

## Typical Commands
```bash
# Opensource with version bump (pushes to BOTH rangehow/ToFu and NiuTrans/ToFu)
python3 export.py --mode opensource --push --bump minor -m 'description'
# Opensource regular sync (no release tag)
python3 export.py --mode opensource --push -m 'description'
# Internal (local only; --push silently disabled)
python3 export.py --mode internal
```

## Version Bumping
- `--bump patch/minor/major`; only opensource bumps, internal just references it.
- Tags (vX.Y.Z) only created when `--bump` is used. Plain sync = no tag.

## ⚠️ ALWAYS run CI locally before push (the export breaks things the source CI never sees)
```bash
cd ../tofu-open
ruff check .                         # CI runs WHOLE-TREE `ruff check .`, NOT just lib/routes/tests
python -m pytest -m unit -q -p no:cacheprovider
```
A fresh opensource export can have DOZENS of failures the source repo doesn't.
Triage: re-run each failing test in isolation; pass-alone ⇒ pollution (often
cascading from one ImportError), fail-alone ⇒ real. Compare against the SOURCE
tree to tell export-introduced from pre-existing.

## TRAP 1 — `ruff --fix` strips RE-EXPORT imports (highest-impact, bitten 2026-06-26)
The export runs `ruff --fix` on `lib/ routes/ tests/`. Any `from X import (a,b,c)`
where some names are re-exported (imported here only so ANOTHER module can
`from this_module import a`) looks unused-in-file → ruff DELETES them →
`ImportError` at runtime + cascading test failures.
- Real case: `lib/tasks_pkg/endpoint_review.py` re-exports `STATE_CHANGING_TOOLS`,
  `accumulate_usage as _accumulate_usage`, `detect_stuck as _detect_stuck`,
  `replan_enabled as _replan_enabled` from `lib.agent_verdict` for `endpoint.py`.
- **Fix at SOURCE:** add `# noqa: F401  — re-exported …` on each such line.
  The noqa SURVIVES the export's ruff --fix (verified). Fix in source, not in
  the export tree, so it persists across re-exports.

## TRAP 2 — export tar-pipe is ADDITIVE; it never DELETES from dest
The copy overlays source onto dest. Files removed from source, or newly
dir-excluded, REMAIN physically in `../tofu-open` (and tracked in its git).
- After adding a dir to `OPENSOURCE_EXTRA_EXCLUDE_DIRS`, or to drop an orphan
  file, you must manually: `cd ../tofu-open && git rm -r --quiet <path> && rm -rf <path>`.
- The exclude only stops the NEXT copy from RE-adding it.
- 2026-06-26: had to manually drop `debug/` (149 files) and stale `install.py`
  (gone from source since v0.10.0) this way.

## TRAP 3 — CI lints the WHOLE tree; keep non-app dirs out of opensource
`.github/workflows/ci.yml` runs `ruff check .`. `ruff.toml` has NO `exclude`
(its header only DOCUMENTS `lib/ routes/ tests/`). So any shipped scratch dir
with lint errors reds the lint job. `OPENSOURCE_EXTRA_EXCLUDE_DIRS` now holds
`scripts, benchmarks, outputs, posters, debug`. Add new scratch/bench dirs here.

## TRAP 4 — internal-only tests must self-skip in opensource builds
Tests that import the non-shipped `export.py`, or assert internal MCP cards
(hope/llm/xuecheng) are present, CANNOT pass in opensource (stripped +
`TOFU_OPENSOURCE_BUILD` baked on). Guard at SOURCE so they run in source, skip
in opensource:
- `pytest.importorskip('export', reason='export.py not shipped in opensource')`
- `@pytest.mark.skipif(is_opensource_build(), reason=...)` (from `lib.mcp.registry`)
Files done: test_export_no_pkg_collision.py, test_mcp_catalog_internal_only.py,
test_mcp_call_health.py.

## Known pre-existing failures (fail in SOURCE too — NOT export bugs)
- `test_pg_copy_self_heal::test_step3_still_defers_for_same_path_remote`
- `test_swarm_async::TestAwaitAgents::test_await_no_ids_includes_early_finishers`
Don't block a push on these; verify they're unchanged vs last push (test files
byte-identical + production code untouched ⇒ not your regression).

## Amend + Force-Push for CI Fixes
1. Re-export (source-fix first): `python3 export.py --mode opensource --dest ../tofu-open`
2. `cd ../tofu-open && git add -A && git -c user.name=rangehow -c user.email=rangehow@users.noreply.github.com commit --amend --no-edit`
3. Tag if needed: `git tag -d vX.Y.Z && git tag vX.Y.Z`
4. `git push --force origin main --tags && git push --force niutrans main --tags`

> ⚠️ The `-c user.name=... -c user.email=...` flags are MANDATORY on every
> hand-run `git commit` / `git commit --amend` (matching `export.py:2673-2674`).
> See the "MANDATORY git identity" section below.

## ⚠️ MANDATORY git identity on EVERY out-of-band commit (bitten 2026-07-02)
Any hand-run `git commit` / `git commit --amend` to the opensource remotes
(i.e. NOT via `export.py`'s own push path, e.g. a token-scoped `git clone`+`cp`
single-file push, or an amend in `../tofu-open`) MUST pin the identity:
```bash
git -c user.name=rangehow -c user.email=rangehow@users.noreply.github.com \
    commit ...          # and for amends: commit --amend --no-edit
# a bare `git commit` also works if you first set repo-local identity:
git config user.name  rangehow
git config user.email rangehow@users.noreply.github.com
```
This mirrors what `export.py:2673-2674` already sets on its own push path — only
HAND-RUN git needs the reminder.

**Why it matters (真问题).** A commit's AUTHOR is independent of WHO pushed it
(auth token ≠ authorship). If `user.name`/`user.email` aren't pinned, the
clone's ambient/global git identity leaks into a PUBLIC commit. On 2026-07-02
an out-of-band install.sh push committed as `tofu-bot <tofu-bot@users.noreply.github.com>`;
that LEGACY noreply form (no `ID+` numeric prefix) is account-linkable by
username, so GitHub re-attributed the commit to an UNRELATED stranger's account
(`Tofu-bot`, id 106741602) and listed them as a contributor on both
rangehow/ToFu and NiuTrans/ToFu. Fix was a re-author amend + force-push
(tips `32f49d9` / `a8ca4fd`); the recurrence surface is THIS playbook, so the
pin lives here.

## Notes
- `.git` and `data/` are preserved across re-exports for incremental commits.
- Post-export runs ruff auto-fix (lib/routes/tests only) + secret scan automatically.
- `.env` is gitignored (not shipped) — its `TOFU_PG_STANDALONE=1` is what makes
  PG tests behave differently locally; on a clean GH runner it's absent.
- Push timeout ≥300s. Commit once, push to each remote independently.

## Bug: `tools/` dir name collision (fixed 2026-04-06)
- export.py excluded ALL dirs named `tools`; also hit `lib/tools/` (core pkg).
- Fix: `_TOP_LEVEL_ONLY_EXCLUDE_DIRS` in export.py + `/tools/` (anchored) in .gitignore.
- Guarded by test_export_no_pkg_collision.py (which is why it imports export).
