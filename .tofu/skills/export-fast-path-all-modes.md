---
name: export-fast-path-all-modes
description: Internal/opensource export now use same tar-pipe fast path as personal + targeted sanitize; rm-rf cleanup skipping excluded/non-source dirs; 10x speedup on FUSE
enabled: true
tags: [export, performance, fuse]
created: 2026-05-06T05:37:13Z
updated: 2026-05-06T05:37:13Z
---

# Export fast path for ALL modes

## Changes (2026-05-06)

Previously only personal mode used `_export_personal_via_tar` (streaming tar pipe).
Internal/opensource went through per-file `shutil.copy2` — painfully slow on FUSE.

**Now all three modes share the same tar-pipe copy**:
- `_build_tar_excludes_for_mode(mode, dest)` — returns (excludes, preserved) for any mode
- `_stream_tar_copy(src, dst, excludes)` — reusable subprocess pipeline
- `_export_via_tar_with_sanitize(mode, dest)` — internal/opensource variant: tar + targeted sanitize
- `_export_personal_via_tar` — thin wrapper over the same primitives

Sanitization became a **post-pass**, not inline with copy:
- Internal: directly sanitize a FIXED whitelist (`core.js`, `settings.js`, `model_config.py`, `index.html`, `trading.html`) + all `.html`/`.js` under `static/`. No rg scan needed.
- Opensource: `_collect_opensource_candidates` → `rg --files-with-matches` with all `_SECRETS`/`_ENDPOINTS`/`_INTERNAL_DOMAIN_LITERALS` as alternation. Python fallback if rg absent.
- `_verify_opensource` also switched to rg (one invocation per leak pattern).

## Smart destination cleanup (the big win on FUSE)

Old code: `shutil.rmtree` or `rm -rf` of EVERYTHING except `.git/data/uploads`.
This nukes `swebench_rerun_workdir` (5k+ files) every time — 5+ minutes of wasted FUSE I/O.

New code: three-way skip:
1. `_preserve_user_data` — `.git`, `data`, `uploads` (user's stuff)
2. `_preserve_excluded` — anything in `ALWAYS_EXCLUDE_DIRS | _TOP_LEVEL_ONLY_EXCLUDE_DIRS | OPENSOURCE_EXTRA_EXCLUDE_DIRS` — tar won't copy here anyway, no reason to rm
3. "Not in source" — tar wouldn't write here either

Only delete what tar will actually overwrite (source-present, non-excluded, non-user-data).

## Gotchas

- **`swebench_rerun_workdir`** was NOT in ALWAYS_EXCLUDE_DIRS but IS in source. Added to both `ALWAYS_EXCLUDE_DIRS` and `PERSONAL_EXCLUDE_DIRS` — ~5k files worth of SWE-bench eval artifacts.
- **rg from Python subprocess** is dramatically slower than interactive bash on this FUSE mount (seconds vs timeout). Cause unknown (FUSE/parallel-walker interaction). Don't rely on rg to scan a 10k+ file tree from Python — prefer a known-files whitelist.
- **Ruff auto-fix** now only runs for opensource mode (CI-cleanup for published code). Internal/personal skip it entirely.
- **HTML sanitize regex** must use `[\w-]+` NOT `[a-z\-]+` for attribute names — the hyphen-in-charclass in `[a-z\-]+` matches literal `\` + `-`, not hyphens. `[\w-]` works correctly.

## Timing

- Before: `--mode internal` timed out at 600s+ on dirty dest with swebench_rerun_workdir
- After: ~1:20 on cold cache, ~1:20 on warm cache (dominated by tar reading ~20k source files over DolphinFS)

## Key functions in export.py

- `_have_tool(name)` — shutil.which shortcut
- `_fast_count_files(root)` — uses fd if available
- `_build_tar_excludes_for_mode(mode, dest)` — unified exclude list builder
- `_stream_tar_copy(src, dst, excludes)` — one-shot tar pipe
- `_export_via_tar_with_sanitize(mode, dest)` — fast path for internal/opensource
- `_collect_internal_candidates(dest)` — fixed whitelist (no rg)
- `_collect_opensource_candidates(dest)` — rg-driven trigger scan
- `_rg_files_with_matches(root, patterns, extra_globs)` — with Python fallback

