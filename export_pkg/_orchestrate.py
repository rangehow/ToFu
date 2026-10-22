"""Top-level export orchestration (split out of ``export.py``)."""

import os
import shutil
import subprocess
from pathlib import Path

from export_pkg import _state
from export_pkg._excludes import (
    _is_text_file,
    _should_exclude,
)
from export_pkg._export_core import (
    _create_skeleton,
    _dest_cleanup_targets,
    _print_export_summary,

    _restore_export_kept_files,
    _restore_opensource_kept_files,
    _regenerate_exported_runtime,
    _run_ruff_autofix,
)
from export_pkg._internal import (
    _bundle_internal_mcp_repos,
    _bundle_tofu_search_wheel,
    _portablize_bundled_mcp_config,
)
from export_pkg._lists import (
    ALWAYS_EXCLUDE_DIRS,
    ALWAYS_EXCLUDE_ROOT_ONLY_DIRS,
    C_BOLD,
    C_CYAN,
    C_DIM,
    C_END,
    C_GREEN,
    C_RED,
    C_YELLOW,
    OPENSOURCE_EXTRA_EXCLUDE_DIRS,
    PERSONAL_EXCLUDE_DIRS,
    ROOT,
    _have_tool,

    _FORCE_STRIP_FROM_DEST,
    logger,
)
from export_pkg._pipeline import (
    _export_personal_via_tar,
    _export_via_tar_with_sanitize,
)
from export_pkg._publish import (
    _git_push,
    _verify_opensource,
    _verify_publish_tree,
)
from export_pkg._sanitize import (
    _sanitize_defaults_for_export,
    _sanitize_source_opensource,
)
from export_pkg._snapshot import (
    _torn_snapshot_guard,
    _untracked_nested_files,
    _untracked_root_dirs,
)
from export_pkg._verify import (

    ExportIntegrityError,
    _verify_exported_js_syntax,
    _verify_exported_py_integrity,
    _verify_exported_py_syntax,
    _verify_exported_test_surface,
)
from export_pkg._version import (
    _read_version,
)


def export_project(mode: str, dest: Path, dry_run: bool = False,
                   push: bool = False, commit_msg: str | None = None,
                   is_release: bool = False, branch_override: str | None = None,
                   progress: bool = False, force: bool = False,
                   worktree: bool = False, history_base: str | None = None):
    """Main export function.

    Args:
        mode: Sanitization level ('personal', 'internal', 'opensource').
        dest: Destination directory.
        dry_run: If True, preview only — no files written.
        push: If True, auto-push to the configured upstream git repo after export.
        commit_msg: Custom git commit message (only used when push=True).
        is_release: If True, create a git tag for the current version.
        progress: If True, stream a live per-file transfer view during the
            tar copy so the operator can watch the transfer and spot any
            files that shouldn't be carried.
    """
    if mode == 'opensource' and push and worktree:
        raise ExportIntegrityError('Public commit synchronization requires committed source; --worktree cannot push')
    _state._EXPORT_SOURCE_SHA = None
    if dest.exists():
        print(f"\n  {C_YELLOW}\u26a0 Destination already exists: {dest} — overwriting{C_END}")
        print(f"  {C_CYAN}\U0001f4be Destination .git/, data/, and uploads/ will be preserved{C_END}")
        if not dry_run:
            # ── Selective cleanup: delete everything EXCEPT preserved dirs ──
            # The preserve/delete policy lives in _dest_cleanup_targets (mode-
            # aware): live-install dests preserve user data + excluded dirs +
            # non-source items; an opensource publish dest is a MIRROR of the
            # export set and keeps only _OPENSOURCE_DEST_PRESERVE.
            try:
                source_names = {p.name for p in ROOT.iterdir()}
            except OSError:
                source_names = set()
            targets = _dest_cleanup_targets(dest, mode, source_names)

            if targets:
                print(f"  {C_DIM}Cleaning {len(targets)} stale items from prior export…{C_END}")
                if _have_tool('rm'):
                    try:
                        subprocess.run(
                            ['rm', '-rf', '--', *[str(t) for t in targets]],
                            check=False,
                        )
                    except OSError as e:
                        logger.warning('rm -rf failed, falling back to rmtree: %s', e)
                        for t in targets:
                            if t.is_dir():
                                shutil.rmtree(t, ignore_errors=True)
                            else:
                                t.unlink(missing_ok=True)
                else:
                    for t in targets:
                        try:
                            if t.is_dir():
                                shutil.rmtree(t, ignore_errors=True)
                            else:
                                t.unlink(missing_ok=True)
                        except OSError as e:
                            logger.warning('Failed to remove %s: %s', t, e)
            remaining = sorted(name for name in _FORCE_STRIP_FROM_DEST
                               if (dest / name).exists() or (dest / name).is_symlink())
            if remaining:
                raise ExportIntegrityError(
                    'Export mandatory cleanup failed: ' + ', '.join(remaining))
            logger.info('Cleanup: removed %d items from dest (mode=%s)',
                        len(targets), mode)

    print(f"\n{C_BOLD}{chr(0x2550) * 64}{C_END}")
    print(f"{C_BOLD}  \U0001f4e6 Tofu Export \u2014 Mode: {mode.upper()}  (v{_read_version()}){C_END}")
    print(f"{C_BOLD}{chr(0x2550) * 64}{C_END}")
    print(f"  Source:  {C_DIM}{ROOT}{C_END}")
    print(f"  Dest:    {C_DIM}{dest}{C_END}")
    if dry_run:
        print(f"  {C_YELLOW}\U0001f50d DRY RUN — no files will be written{C_END}")
        if mode != 'personal' and not worktree:
            print(f"  {C_DIM}ℹ The real export copies committed HEAD (git archive), not the\n"
                  f"    worktree walked below — uncommitted files listed here do NOT ship.{C_END}")
            _torn_snapshot_guard(ROOT, allow_dirty=True, dry_run=True)
    print()

    # Set shared version state for the sanitization pipeline
    _state._EXPORT_VERSION = _read_version()

    # ── Fast path: ALL non-dry-run modes use streaming tar copy ──
    #
    # Per-file ``shutil.copy2`` is dog-slow on FUSE (~5 round-trips per file:
    # open/read/write/chmod/utime). On a tree of ~7k files across DolphinFS
    # that's the difference between ~5s (tar pipe) and 5+ minutes (per-file
    # walk).  Sanitization is then a TARGETED post-pass over only the files
    # that actually contain a sanitization trigger (located via a single rg
    # invocation), which for internal mode is ~4 files and for opensource
    # ~30-50 files.
    #
    # Personal mode runs the same tar pipe but skips the sanitize pass.
    # Dry-run still uses the legacy walk so users can preview what would
    # be sanitized without writing anything.
    if not dry_run:
        if mode == 'personal':
            stats = _export_personal_via_tar(dest, progress=progress)
            excluded_log: list = []
            _all_excluded_dirs = PERSONAL_EXCLUDE_DIRS
            # Personal mode skips _create_skeleton (it mirrors source data/
            # verbatim), but the sibling MCP repos live OUTSIDE the source
            # tree (ROOT.parent) so the tar copy never picks them up. Bundle
            # them explicitly, then rewrite the copied mcp_servers.json from
            # absolute `uvx --from /path` entries to bare launchers so it's
            # portable to another machine.
            _bundle_internal_mcp_repos(dest, mode)
            _bundle_tofu_search_wheel(dest, mode)
            _portablize_bundled_mcp_config(dest, mode)
        else:
            stats = _export_via_tar_with_sanitize(mode, dest, progress=progress,
                                                  worktree=worktree)
            excluded_log = []
            _all_excluded_dirs = (ALWAYS_EXCLUDE_DIRS
                                  if mode == 'internal'
                                  else ALWAYS_EXCLUDE_DIRS | OPENSOURCE_EXTRA_EXCLUDE_DIRS)

        # ── Skeleton (creates data/, logs/, .env, etc.) ──
        # Personal mode's tar already includes data/ and uploads/ so we skip
        # for it; internal/opensource need it because data/ was excluded.
        if mode != 'personal':
            _create_skeleton(dest, mode)

        # Render summary and exit (skip the legacy walk below).
        _print_export_summary(dest, mode, stats, excluded_log, _all_excluded_dirs)

        # Post-export tasks: lint (opensource only), verify, push.

        if mode == 'internal':
            _restore_export_kept_files(dest, mode)
            _verify_exported_test_surface(dest)
        if mode == 'opensource':
            _restore_opensource_kept_files(dest)
            _regenerate_exported_runtime(dest)
            _run_ruff_autofix(dest)
            _verify_opensource(dest)
            _verify_publish_tree(dest)
            # The public CI runs the shipped tests against the shipped tree;
            # both surfaces must agree or every collection-time ImportError
            # lands as a red lane on GitHub (2026-09 lesson).
            _verify_exported_test_surface(dest)
        # JS syntax safety net (internal + opensource both sanitize JS). Runs
        # BEFORE push so a sanitizer regression that corrupts JS fails the
        # export loudly instead of shipping a white-screening tree.
        if mode in ('internal', 'opensource'):
            _verify_exported_js_syntax(dest)
            _verify_exported_py_syntax(dest)
            _verify_exported_py_integrity(dest)
        if push:
            history_builder = None
            if mode == 'opensource':
                from export_pkg._history import synchronize_public_history
                if not _state._EXPORT_SOURCE_SHA:
                    raise ExportIntegrityError('Public commit synchronization requires an immutable source SHA')
                source_sha = _state._EXPORT_SOURCE_SHA

                def history_builder(public, tree, message, *, published_tip=None):
                    return synchronize_public_history(
                        ROOT, public, source_sha, tree, message,
                        history_base=history_base, published_tip=published_tip).public_tip
            _git_push(dest, mode, commit_msg, is_release=is_release,
                      branch_override=branch_override, force=force,
                      history_builder=history_builder)
        return

    # ── Dry-run: walk-based preview (no files written) ──
    stats = {
        'copied': 0,
        'sanitized': 0,
        'excluded': 0,
        'excluded_reasons': {},
    }

    # Collect the right dir set for excluded-items display
    _all_excluded_dirs = (PERSONAL_EXCLUDE_DIRS if mode == 'personal'
                          else ALWAYS_EXCLUDE_DIRS | ALWAYS_EXCLUDE_ROOT_ONLY_DIRS)

    excluded_log = []

    # Mirror the real tar-copy path: internal/opensource also skip untracked,
    # non-gitignored ROOT dirs (agent scratch). ``_should_exclude`` is a pure
    # per-file predicate with no git knowledge, so consult the same set here —
    # otherwise the preview would list ``module1/`` etc. as "will copy" while
    # the real run drops them. Computed once (one git call) before the walk.
    _stray_root_dirs: set[str] = (
        set() if mode == 'personal' else _untracked_root_dirs(ROOT))
    # Same alignment for untracked FILES in tracked dirs — the preview must
    # count exactly what the real copy drops.
    _stray_files: set[str] = (
        set() if mode == 'personal'
        else _untracked_nested_files(ROOT, _stray_root_dirs))

    for dirpath, dirnames, filenames in os.walk(ROOT):
        # Compute relative path
        rel_dir = os.path.relpath(dirpath, ROOT)
        if rel_dir == '.':
            rel_dir = ''

        # Pre-filter directories (modifying dirnames in-place skips subtrees).
        # At the root, also prune stray untracked dirs and log them so the
        # dry-run's exclude count matches what the real copy actually drops.
        kept = []
        for d in sorted(dirnames):
            if not rel_dir and d in _stray_root_dirs:
                stats['excluded'] += 1
                reason = 'untracked non-gitignored root dir'
                stats['excluded_reasons'][reason] = \
                    stats['excluded_reasons'].get(reason, 0) + 1
                excluded_log.append((d, f'{reason}: {d}'))
                continue
            if _should_exclude(
                    os.path.join(rel_dir, d) if rel_dir else d, d, mode):
                continue
            kept.append(d)
        dirnames[:] = kept

        for filename in sorted(filenames):
            relpath = os.path.join(rel_dir, filename) if rel_dir else filename
            src_path = Path(dirpath) / filename

            # Untracked files in tracked dirs (uncommitted WIP, tmp scratch) —
            # the real tar copy drops them; the preview must count them too.
            if relpath in _stray_files:
                stats['excluded'] += 1
                reason = 'untracked non-gitignored file'
                stats['excluded_reasons'][reason] = \
                    stats['excluded_reasons'].get(reason, 0) + 1
                excluded_log.append((relpath, f'{reason}: {relpath}'))
                continue

            # Check exclusion
            reason = _should_exclude(relpath, filename, mode)
            if reason:
                stats['excluded'] += 1
                cat = reason.split(':')[0].strip()
                stats['excluded_reasons'][cat] = stats['excluded_reasons'].get(cat, 0) + 1
                excluded_log.append((relpath, reason))
                continue

            # Dry-run only: show what would happen without writing.
            if mode in ('internal', 'opensource') and _is_text_file(str(src_path)):
                try:
                    content = src_path.read_text(encoding='utf-8', errors='replace')
                    sanitized = _sanitize_defaults_for_export(
                        content, relpath, version=_state._EXPORT_VERSION)
                    if mode == 'opensource':
                        sanitized = _sanitize_source_opensource(sanitized, relpath)
                    if sanitized != content:
                        print(f"  {C_YELLOW}\U0001f527{C_END} {relpath} "
                              f"{C_DIM}(would sanitize){C_END}")
                        stats['sanitized'] += 1
                        continue
                except Exception as e:
                    logger.debug('[export] dry-run sanitize preview failed for '
                                 '%s: %s', relpath, e)
            print(f"  {C_GREEN}\U0001f4c4{C_END} {relpath}")
            stats['copied'] += 1

    # ── Dry-run summary ──
    print(f"\n{C_BOLD}{chr(0x2500) * 64}{C_END}")
    print(f"  {C_GREEN}\u2713 Would copy:    {stats['copied']}{C_END}")
    if stats['sanitized']:
        print(f"  {C_YELLOW}\U0001f527 Would sanitize: {stats['sanitized']}{C_END}")
    print(f"  {C_RED}\u2717 Would exclude:  {stats['excluded']}{C_END}")
    if stats['excluded_reasons']:
        print(f"\n  {C_DIM}Exclusion breakdown:{C_END}")
        for reason, count in sorted(stats['excluded_reasons'].items(),
                                     key=lambda x: -x[1]):
            print(f"    {C_DIM}{reason}: {count}{C_END}")
    print(f"\n  {C_CYAN}Dry run complete \u2014 no files written.{C_END}\n")
