---
name: export-personal-fast-path-tar
description: Personal-mode export uses streaming tar pipe; preserves dest data/uploads/.git
enabled: true
tags: [export, fuse, performance, tar]
created: 2026-04-28T16:27:21Z
updated: 2026-04-28T16:27:21Z
---

## Personal-mode export: fast path via streaming tar

`export.py` → `_export_personal_via_tar(dest)` replaces per-file `shutil.copy2`
for personal mode with a streaming `tar cf - ... | tar xf -` pipe.

**Why**: Per-file copy on FUSE/DolphinFS/NFS is dog-slow (many round-trips
per file). Cross-cluster (e.g. `hadoop-aipnlp` → `hadoop-nlp-sh02`), a
walk-based copy can take 10+ minutes for ~7k small files; tar pipe finishes
in seconds.

**Preservation contract** — if the destination already contains:
- `data/` (chat history DB, configs)
- `uploads/` (user-uploaded images)
- `.git/` (local git history)

…those dirs are added to the tar exclude list so the source NEVER overwrites
the destination's user data. The walk-based paths (internal/opensource) have
a similar preserve set via the `_preserve = {'.git', 'data', 'uploads'}`
cleanup block plus `_purge_runtime_artifacts` being chat-history-safe.

**No timeout**: cross-DC copies can take a while; the tar `communicate()`
runs without a timeout argument, as the user explicitly asked.

**Activation**: mode==personal and not dry_run → tar path runs and `return`s
early (skips the walk). Dry-run still uses the walk so users can preview.

**Excludes** are built from the existing `PERSONAL_EXCLUDE_*` sets, so
changes to those lists automatically apply to the tar path too.

