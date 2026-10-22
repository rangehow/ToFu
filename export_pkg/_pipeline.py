"""Copy + sanitize pipelines: tar-exclude construction and the streaming
tar export paths (split out of ``export.py``)."""

import shutil
from pathlib import Path

from export_pkg import _state
from export_pkg._excludes import (
    _scrub_dest_pgdata_identity,
)
from export_pkg._lists import (
    ALWAYS_EXCLUDE_DIRS,
    ALWAYS_EXCLUDE_FILES,
    ALWAYS_EXCLUDE_GLOBS,
    ALWAYS_EXCLUDE_ROOT_ONLY_DIRS,
    ALWAYS_EXCLUDE_ROOT_ONLY_FILES,
    C_CYAN,
    C_DIM,
    C_END,
    OPENSOURCE_EXTRA_EXCLUDE_DIRS,
    OPENSOURCE_EXTRA_EXCLUDE_FILES,
    PERSONAL_EXCLUDE_DIRS,
    PERSONAL_EXCLUDE_FILES,
    PERSONAL_EXCLUDE_GLOBS,
    PERSONAL_EXCLUDE_GLOBS_HEAVY,
    ROOT,
    _AGENT_ARTIFACT_GLOB,
    _TOP_LEVEL_ONLY_EXCLUDE_DIRS,
    _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS,
    _fast_count_files,
    logger,
)
from export_pkg._sanitize import (
    _post_copy_sanitize,
)
from export_pkg._snapshot import (
    _dirty_tracked_files,
    _gitignored_excludes,
    _stage_head_snapshot,
    _torn_snapshot_guard,
    _untracked_file_excludes,
    _untracked_root_excludes,
)
from export_pkg._tar import (
    _stream_tar_copy,
)


def _build_tar_excludes_for_mode(mode: str, dest: Path, *,
                                 committed_snapshot: bool = False) -> tuple[list[str], list[str]]:
    """Build ``--exclude=`` args for the streaming tar copy, per mode.

    Returns ``(excludes, preserved)`` where:
      - ``excludes`` is the list of ``--exclude=…`` args (in the order
        wanted by GNU tar — top-level anchored entries first, then
        unanchored names that match anywhere in the tree).
      - ``preserved`` is the list of dest top-level dirs (e.g.
        ``data``, ``uploads``, ``.git``) that already exist and are
        being preserved (NOT re-copied from source).

    Modes:
      personal    — `PERSONAL_EXCLUDE_*` only (loosest)
      internal    — full ALWAYS_EXCLUDE_* set
      opensource  — full ALWAYS_EXCLUDE_* + OPENSOURCE_EXTRA_EXCLUDE_*
    """
    tar_excludes: list[str] = []

    if mode == 'personal':
        dirs = PERSONAL_EXCLUDE_DIRS | _TOP_LEVEL_ONLY_EXCLUDE_DIRS
        files = PERSONAL_EXCLUDE_FILES
        globs = PERSONAL_EXCLUDE_GLOBS | PERSONAL_EXCLUDE_GLOBS_HEAVY
    elif mode == 'internal':
        dirs = (ALWAYS_EXCLUDE_DIRS | _TOP_LEVEL_ONLY_EXCLUDE_DIRS
                | ALWAYS_EXCLUDE_ROOT_ONLY_DIRS)
        files = ALWAYS_EXCLUDE_FILES
        globs = ALWAYS_EXCLUDE_GLOBS
    else:  # opensource
        dirs = (ALWAYS_EXCLUDE_DIRS | OPENSOURCE_EXTRA_EXCLUDE_DIRS
                | _TOP_LEVEL_ONLY_EXCLUDE_DIRS | ALWAYS_EXCLUDE_ROOT_ONLY_DIRS
                | _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS)
        files = ALWAYS_EXCLUDE_FILES | OPENSOURCE_EXTRA_EXCLUDE_FILES
        globs = ALWAYS_EXCLUDE_GLOBS

    for d in dirs:
        # Top-level-only entries (e.g. 'paper', 'sundries', root 'data')
        # anchored at './' so a nested subdir of the same name elsewhere
        # isn't pruned.
        if (d in _TOP_LEVEL_ONLY_EXCLUDE_DIRS or d in ALWAYS_EXCLUDE_ROOT_ONLY_DIRS
                or (mode == 'opensource' and d in _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS)):
            tar_excludes.append(f'--exclude=./{d}')
        else:
            # Anchored AND nested — applies wherever it appears in the tree.
            tar_excludes.append(f'--exclude=./{d}')
            tar_excludes.append(f'--exclude={d}')
    for g in globs:
        tar_excludes.append(f'--exclude={g}')
    for f in files:
        tar_excludes.append(f'--exclude={f}')
    if mode != 'personal':
        for f in ALWAYS_EXCLUDE_ROOT_ONLY_FILES:
            # Anchored at './' so a nested same-named module (e.g.
            # packages/tofu-overleaf-mcp's tofu_overleaf_mcp/export.py) ships.
            # Personal mode is a full self-backup and keeps the toolchain.
            tar_excludes.append(f'--exclude=./{f}')

    # Agent-written artifacts (.tofu*) — one glob covers every current AND
    # future ``.tofu``-prefixed artifact in every mode, so a new artifact
    # never silently leaks into an export (the historical drift failure mode).
    tar_excludes.append(f'--exclude={_AGENT_ARTIFACT_GLOB}')

    # Honour .gitignore for internal/opensource so the export stays in
    # lock-step with the repo's ignore rules (covers new eval/bench
    # *_workdir/ dirs, caches, etc. without touching the hand-maintained
    # lists). Personal mode is INTENTIONALLY skipped — it's a full self-use
    # backup that keeps gitignored data (.env, data/, uploads/, .chatui/).
    # A frozen Git tree already excludes untracked/ignored worktree state.
    # Live ROOT may classify files present at the pinned SHA as untracked;
    # applying that list would silently remove committed snapshot members.
    if mode != 'personal' and not committed_snapshot:
        tar_excludes.extend(_gitignored_excludes(ROOT))
        # Also skip untracked, NON-gitignored root dirs (agent scratch that
        # neither git ignores nor the hand-maintained lists cover) — see
        # _untracked_root_excludes. Prevents stray code-exec dirs (module1/,
        # scratchpad/, …) leaking into the export and breaking the ruff gate.
        tar_excludes.extend(_untracked_root_excludes(ROOT))
        # …and untracked FILES inside tracked dirs (uncommitted new/WIP
        # source, tests/tmp*.js NC-temp copies) — the export mirrors
        # committed source; `git add` is the keeper. See
        # _untracked_nested_files for the incident that forced this.
        tar_excludes.extend(_untracked_file_excludes(ROOT))

    # Preserve dest user data + git history across re-exports. Same set for
    # all modes — these dirs belong to the destination user, not source.
    preserved: list[str] = []
    for p in ('data', 'uploads', '.git'):
        if (dest / p).exists():
            tar_excludes.append(f'--exclude=./{p}')
            preserved.append(p)
    return tar_excludes, preserved


def _export_personal_via_tar(dest: Path, progress: bool = False) -> dict:
    """Fast personal-mode export using a streaming ``tar c | tar x`` pipe.

    Per-file ``shutil.copy2`` is painfully slow on FUSE / network filesystems
    (DolphinFS, NFS, etc.) because every file incurs multiple round-trips
    (create, stat, chmod, utime).  Piping a single tar stream instead lets
    the kernel batch I/O and the destination side writes sequentially.

    Preservation: if the destination already has ``data/``, ``uploads/``, or
    ``.git/``, those are EXCLUDED from the source tar — so the destination's
    own data, uploaded files, and git history are never clobbered by a
    re-export.  Personal mode deliberately includes its own ``data/`` and
    ``uploads/`` only when the destination is empty / fresh.

    Chat history is NOT carried: the PG cluster, SQLite DBs, and SQL dumps
    are all excluded (see PERSONAL_EXCLUDE_*), so a fresh destination boots
    with an empty database while config + credentials (data/config/) come
    across intact.

    Returns a stats dict compatible with the walk-based path.
    """
    dest.mkdir(parents=True, exist_ok=True)
    tar_excludes, preserved = _build_tar_excludes_for_mode('personal', dest)
    if preserved:
        print(f"  {C_CYAN}\U0001f4be Preserving dest: {', '.join(preserved + ['/'])}"
              f"  (not overwritten from source){C_END}")

    # Personal mode does NOT carry chat history: pgdata/, *.db, and any
    # SQL dumps are excluded (see PERSONAL_EXCLUDE_*), so a fresh dest boots
    # with an empty DB. Config + credentials (data/config/) still copy over.

    print(f"  {C_DIM}Streaming tar pipe (src → dst) — much faster on FUSE…{C_END}")

    stats = {'copied': 0, 'sanitized': 0, 'excluded': 0, 'excluded_reasons': {}}

    _stream_tar_copy(ROOT, dest, tar_excludes, progress=progress)

    # Count files at dest for the summary (best-effort, fast path via fd).
    stats['copied'] = _fast_count_files(dest)

    # ── Scrub host-identity files from any pgdata the dest may already have ──
    # Belt-and-braces: if a previous (pre-dump) export left a pgdata there, or
    # a user hand-copied one in, make sure the dest PG bootstrap never silently
    # connects to the source machine via the old ``.pg_owner_host`` marker.
    _scrub_dest_pgdata_identity(dest)

    return stats


def _export_via_tar_with_sanitize(mode: str, dest: Path,
                                  progress: bool = False,
                                  worktree: bool = False) -> dict:
    """Fast non-personal export: bulk tar copy + post-pass sanitize.

    This is the internal/opensource analogue of ``_export_personal_via_tar``.
    Workflow:
      1. Stream the source tree through ``tar c | tar x`` with all the
         per-mode exclude flags (NOT the legacy os.walk/copy_file loop).
      2. After bulk copy, run a single ``rg`` search for sanitization
         triggers; for each hit, read+rewrite that file in place.
      3. Purge runtime artifacts the legacy path would have skipped (logs/,
         .lock files, key_stats.json, etc.) — same logic as before.

    Returns a stats dict matching the legacy walk path's shape so the
    summary printer doesn't need to special-case anything.
    """
    dest.mkdir(parents=True, exist_ok=True)
    snapshot: Path | None = None
    src = ROOT
    _state._EXPORT_SOURCE_SHA = None
    if worktree:
        logger.warning('--worktree: exporting UNCOMMITTED worktree state')
        _torn_snapshot_guard(ROOT, allow_dirty=True)  # warn, never block
    else:
        snapshot = _stage_head_snapshot(ROOT)
        if snapshot is not None:
            src = snapshot
            try:
                version_file = src / 'VERSION'
                _state._EXPORT_VERSION = (version_file.read_text(encoding='utf-8').strip()
                                          if version_file.is_file() else '0.0.0')
            except (OSError, UnicodeError):
                shutil.rmtree(snapshot, ignore_errors=True)
                raise
            dirty = _dirty_tracked_files(ROOT)
            if dirty:
                shown = ', '.join(dirty[:8]) + (f' … (+{len(dirty) - 8})' if len(dirty) > 8 else '')
                print(f"  {C_DIM}ℹ Export source = committed Git snapshot; {len(dirty)} dirty "
                      f"tracked file(s) NOT published (commit to include, or\n"
                      f"  --worktree to export uncommitted state): {shown}{C_END}")
        else:
            # Worktree fallback — the torn-snapshot hazard applies, so guard hard.
            _torn_snapshot_guard(ROOT, allow_dirty=False)
    tar_excludes, preserved = _build_tar_excludes_for_mode(
        mode, dest, committed_snapshot=snapshot is not None)
    if preserved:
        print(f"  {C_CYAN}\U0001f4be Preserving dest: {', '.join(preserved + ['/'])}"
              f"  (not overwritten from source){C_END}")

    print(f"  {C_DIM}Streaming tar pipe (src → dst) — fast path…{C_END}")
    try:
        _stream_tar_copy(src, dest, tar_excludes, progress=progress)
    finally:
        if snapshot is not None:
            shutil.rmtree(snapshot, ignore_errors=True)

    # Targeted sanitize pass — only opens files containing trigger patterns.
    print(f"  {C_DIM}Running targeted sanitize pass (rg-driven)…{C_END}")
    san_stats = _post_copy_sanitize(dest, mode)

    copied = _fast_count_files(dest)
    return {
        'copied': copied - san_stats['sanitized'],
        'sanitized': san_stats['sanitized'],
        'excluded': 0,            # tar handles exclusions silently — no per-file count
        'excluded_reasons': {},
    }
