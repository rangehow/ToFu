#!/usr/bin/env python3
"""export.py — Project export with three-level sanitization.

Usage:
  python3 export.py --mode personal --dest /path/to/output   # self-use copy
  python3 export.py --mode internal --dest /path/to/output
  python3 export.py --mode opensource --dest /path/to/output
  python3 export.py --mode personal                          # defaults to ../tofu-personal
  python3 export.py --mode internal                          # defaults to ../tofu-internal
  python3 export.py --mode opensource                        # defaults to ../tofu-opensource
  python3 export.py --dry-run --mode opensource              # preview only, no copy

Sanitization levels:
  personal   — For yourself (another machine, backup, etc.)
               ✓ Keeps your config + credentials: API keys, providers,
                 MCP server tokens (data/config/), skills, uploads.
               ✗ Does NOT carry chat history: the PG cluster (data/pgdata/),
                 SQLite *.db files, and any SQL dumps (data/pg_backup.sql,
                 data/pg_backups/) are all skipped, so the destination boots
                 with a fresh empty database.
               ✗ Also skips bulky regenerable junk: __pycache__, .git,
                 node_modules, offline_pkgs, *.pyc, logs/, and other large
                 runtime artifacts.
               No other sanitization applied.

  internal   — For colleagues within the company.

               ✗ Removes the maintainer export toolchain and private markers.
               ✓ Keeps API keys + internal endpoints: providers/models/proxy
                 are seeded into data/config/server_config.json.
               ✗ Removes personal data: databases, chat history, skills,
                 logs, session caches, uploaded files, personal notes,
                 Feishu credentials (personal app config),
                 ~/.chatui references are cleaned (legacy filename retained for back-compat).

  opensource — For public release (GitHub, etc.)
               ✗ Removes ALL of the above, PLUS:
               ✗ Hardcoded API keys → placeholder
               ✗ Internal endpoints (aigc.sankuai.com) → env-var-only
               ✗ Feishu credentials → empty
               ✗ Internal domain references (.sankuai.com, .meituan.com)
               ✗ Internal model names (LongCat-*, aws.claude-*) retained
                 as working defaults (they're just model strings)
               ✗ Hardcoded absolute paths (/mnt/dolphinfs/...)
               ✗ Security audit report (reveals attack surface)
               ✗ Maintainer-only settings tabs (A/B cost experiment): the
                 fragment is excluded here AND lib/settings_panels strips the
                 tab at render time (TOFU_OPENSOURCE_BUILD baked to '1')
               ✗ The export toolchain itself (export.py + export_pkg/):
                 export_pkg/_lists.py carries private markers as
                 sanitization patterns — never published in any form.

Layout: this file is a thin facade — the implementation lives in
export_pkg/ (see export_pkg/AGENTS.md).
"""


import argparse
import sys
import textwrap
import subprocess  # noqa: F401  # tests monkeypatch export.subprocess.run
import tempfile  # noqa: F401  # tests monkeypatch export.tempfile.mkdtemp
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_pkg import (
    _state as _state,
)
from export_pkg._lists import (
    ALWAYS_EXCLUDE_DIRS as ALWAYS_EXCLUDE_DIRS,
    ALWAYS_EXCLUDE_FILES as ALWAYS_EXCLUDE_FILES,
    ALWAYS_EXCLUDE_GLOBS as ALWAYS_EXCLUDE_GLOBS,
    ALWAYS_EXCLUDE_ROOT_ONLY_DIRS as ALWAYS_EXCLUDE_ROOT_ONLY_DIRS,
    ALWAYS_EXCLUDE_ROOT_ONLY_FILES as ALWAYS_EXCLUDE_ROOT_ONLY_FILES,
    C_BOLD as C_BOLD,
    C_CYAN as C_CYAN,
    C_DIM as C_DIM,
    C_END as C_END,
    C_GREEN as C_GREEN,
    C_RED as C_RED,
    C_YELLOW as C_YELLOW,
    OPENSOURCE_EXTRA_EXCLUDE_DIRS as OPENSOURCE_EXTRA_EXCLUDE_DIRS,
    OPENSOURCE_EXTRA_EXCLUDE_FILES as OPENSOURCE_EXTRA_EXCLUDE_FILES,
    PERSONAL_EXCLUDE_DIRS as PERSONAL_EXCLUDE_DIRS,
    PERSONAL_EXCLUDE_FILES as PERSONAL_EXCLUDE_FILES,
    PERSONAL_EXCLUDE_GLOBS as PERSONAL_EXCLUDE_GLOBS,
    PERSONAL_EXCLUDE_GLOBS_HEAVY as PERSONAL_EXCLUDE_GLOBS_HEAVY,
    ROOT as ROOT,
    _AGENT_ARTIFACT_GLOB as _AGENT_ARTIFACT_GLOB,
    _ENDPOINTS as _ENDPOINTS,
    _FORCE_STRIP_FROM_DEST as _FORCE_STRIP_FROM_DEST,
    _INSTALL_SH_PROXY_BEGIN as _INSTALL_SH_PROXY_BEGIN,
    _INSTALL_SH_PROXY_END as _INSTALL_SH_PROXY_END,
    _INTERNAL_CONFIG_KEYS as _INTERNAL_CONFIG_KEYS,
    _INTERNAL_DOMAIN_LITERALS as _INTERNAL_DOMAIN_LITERALS,
    _INTERNAL_FIXED_CANDIDATES as _INTERNAL_FIXED_CANDIDATES,
    _INTERNAL_IDENTIFIER_REPLACEMENTS as _INTERNAL_IDENTIFIER_REPLACEMENTS,
    _INTERNAL_SANITIZE_TRIGGERS as _INTERNAL_SANITIZE_TRIGGERS,
    _OPENSOURCE_DEST_PRESERVE as _OPENSOURCE_DEST_PRESERVE,
    _OPENSOURCE_FIXED_CANDIDATES as _OPENSOURCE_FIXED_CANDIDATES,
    _OPENSOURCE_KEEP_FILES as _OPENSOURCE_KEEP_FILES,
    _PUBLISH_FORBIDDEN_NAMES as _PUBLISH_FORBIDDEN_NAMES,
    _PUBLISH_FORBIDDEN_ROOTS as _PUBLISH_FORBIDDEN_ROOTS,
    _PUBLISH_FORBIDDEN_SUFFIXES as _PUBLISH_FORBIDDEN_SUFFIXES,
    _PUBLISH_MAX_FILE_BYTES as _PUBLISH_MAX_FILE_BYTES,
    _PUBLISH_MAX_TREE_BYTES as _PUBLISH_MAX_TREE_BYTES,
    _PUBLISH_PLACEHOLDERS as _PUBLISH_PLACEHOLDERS,
    _RG_MAX_FILESIZE as _RG_MAX_FILESIZE,
    _RG_MAX_FILESIZE_BYTES as _RG_MAX_FILESIZE_BYTES,
    _SECRETS as _SECRETS,
    _TOP_LEVEL_ONLY_EXCLUDE_DIRS as _TOP_LEVEL_ONLY_EXCLUDE_DIRS,
    _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS as _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS,
    _fast_count_files as _fast_count_files,
    _have_tool as _have_tool,
    is_agent_artifact as is_agent_artifact,
    logger as logger,
)
from export_pkg._excludes import (
    _glob_match as _glob_match,
    _is_text_file as _is_text_file,
    _scrub_dest_pgdata_identity as _scrub_dest_pgdata_identity,
    _should_exclude as _should_exclude,
)
from export_pkg._handoff import (
    run_handoff as run_handoff,
)
from export_pkg._export_core import (
    _create_skeleton as _create_skeleton,
    _dest_cleanup_targets as _dest_cleanup_targets,
    _print_export_summary as _print_export_summary,
    _restore_opensource_kept_files as _restore_opensource_kept_files,
    _run_ruff_autofix as _run_ruff_autofix,
)
from export_pkg._internal import (
    _bundle_internal_mcp_repos as _bundle_internal_mcp_repos,
    _bundle_tofu_search_wheel as _bundle_tofu_search_wheel,
    _copy_file as _copy_file,
    _patch_install_sh_proxy as _patch_install_sh_proxy,
    _portablize_bundled_mcp_config as _portablize_bundled_mcp_config,
    _purge_runtime_artifacts as _purge_runtime_artifacts,
    _seed_internal_config as _seed_internal_config,
)
from export_pkg._publish import (
    ExportPublishSafetyError as ExportPublishSafetyError,
    _GH_TOKEN as _GH_TOKEN,
    _GIT_REPOS as _GIT_REPOS,
    _git_push as _git_push,
    _is_nonff_push_rejection as _is_nonff_push_rejection,
    _load_gh_token as _load_gh_token,
    _publish_candidate_paths as _publish_candidate_paths,
    _publish_tree_findings as _publish_tree_findings,
    _push_branch as _push_branch,
    _push_tag as _push_tag,
    _resolve_authenticated_remotes as _resolve_authenticated_remotes,
    _tag_push_action as _tag_push_action,
    _verify_opensource as _verify_opensource,
    _verify_publish_tree as _verify_publish_tree,
    _verify_staged_publish_snapshot as _verify_staged_publish_snapshot,
)
from export_pkg._sanitize import (
    _collect_internal_candidates as _collect_internal_candidates,
    _collect_opensource_candidates as _collect_opensource_candidates,
    _opensource_sanitize_triggers as _opensource_sanitize_triggers,
    _post_copy_sanitize as _post_copy_sanitize,
    _rg_excluded_globs as _rg_excluded_globs,
    _rg_files_with_matches as _rg_files_with_matches,
    _sanitize_defaults_for_export as _sanitize_defaults_for_export,
    _sanitize_source_opensource as _sanitize_source_opensource,
    _scan_oversized_text_files as _scan_oversized_text_files,
)
from export_pkg._snapshot import (
    _dirty_tracked_files as _dirty_tracked_files,
    _gitignored_excludes as _gitignored_excludes,
    _stage_head_snapshot as _stage_head_snapshot,
    _torn_snapshot_guard as _torn_snapshot_guard,
    _torn_snapshot_pairs as _torn_snapshot_pairs,
    _untracked_file_excludes as _untracked_file_excludes,
    _untracked_nested_files as _untracked_nested_files,
    _untracked_root_dirs as _untracked_root_dirs,
    _untracked_root_excludes as _untracked_root_excludes,
)
from export_pkg._tar import (
    _ellipsize as _ellipsize,
    _stream_tar_copy as _stream_tar_copy,
    _tar_progress_consumer as _tar_progress_consumer,
)
from export_pkg._verify import (
    ExportIntegrityError as ExportIntegrityError,
    ExportSyntaxError as ExportSyntaxError,
    ExportTestSurfaceError as ExportTestSurfaceError,
    _test_surface_findings as _test_surface_findings,
    _verify_exported_js_syntax as _verify_exported_js_syntax,
    _verify_exported_py_integrity as _verify_exported_py_integrity,
    _verify_exported_py_syntax as _verify_exported_py_syntax,
    _verify_exported_test_surface as _verify_exported_test_surface,
)
from export_pkg._version import (
    _bump_version as _bump_version,
    _commit_version_to_origin as _commit_version_to_origin,
    _next_version as _next_version,
    _read_version as _read_version,
)
from lib.linear_git_checkpoint import (
    LinearCheckpointError as LinearCheckpointError,
    preferred_export_ref as preferred_export_ref,
)
from export_pkg._pipeline import (
    _build_tar_excludes_for_mode as _build_tar_excludes_for_mode,
    _export_personal_via_tar as _export_personal_via_tar,
    _export_via_tar_with_sanitize as _export_via_tar_with_sanitize,
)
from export_pkg._orchestrate import (
    export_project as export_project,
)
# Rebindable mutable globals. ``export_pkg`` code reads/writes the canonical
# copies in ``export_pkg._state``; these facade mirrors keep the historical
# module surface stable (nothing reads them at runtime).
_EXPORT_VERSION = _state._EXPORT_VERSION
_EXPORT_SOURCE_SHA = _state._EXPORT_SOURCE_SHA

# Source-text pin (tests/storage_authority_sql/test_inspect_conversation.py): the guard asserts this
# literal appears in THIS facade's source, because debug/ is an opensource-
# excluded dir whose keep-file entry must be visible in the shipped release
# tool. The authoritative set lives in export_pkg._lists._OPENSOURCE_KEEP_FILES.
# 'debug/inspect_conversation.py'



def main():
    parser = argparse.ArgumentParser(
        description='Export Tofu project with sanitization.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 export.py --mode personal --dest ~/tofu-backup
              python3 export.py --mode internal --dest ~/tofu-team
              python3 export.py --mode opensource --dest ~/tofu-public
              python3 export.py --mode opensource --dry-run
              python3 export.py --mode opensource --push --bump patch -m 'bug fixes'
              python3 export.py --mode opensource --push --bump minor -m 'new feature'
              python3 export.py --mode internal --push
              python3 export.py --mode internal --handoff /path/to/handoff --push
        """),
    )
    parser.add_argument(
        '--mode', required=True, choices=['personal', 'internal', 'opensource'],
        help='Sanitization level: personal (self-use), internal (company), or opensource (public)',
    )
    parser.add_argument(
        '--dest',
        help='Destination directory (default: ../tofu-<mode>)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview what would be copied/excluded without writing files',
    )
    parser.add_argument(
        '--push', action='store_true',
        help='Auto-push to upstream git repo after export (opensource only; internal never pushes)',
    )
    parser.add_argument(
        '--msg', '-m',
        help='Git commit message (used with --push)',
    )
    parser.add_argument(
        '--bump', choices=['major', 'minor', 'patch'],
        help='Bump version before export (major/minor/patch)',
    )

    parser.add_argument(
        '--release', action='store_true',
        help='Tag the already-prepared, committed release version (does not bump metadata)',
    )
    parser.add_argument(
        '--history-base',
        help='First Open publication: explicit source cutoff for sanitized commit synchronization; later runs resume the private cursor',
    )
    parser.add_argument(
        '--branch',
        help='Override git branch name (default: from repo config)',
    )
    parser.add_argument(
        '--progress', action='store_true',
        help='Stream a live per-file view during the copy (shows each '
             'top-level dir entered + a running transferred-files counter) '
             'so you can watch the transfer and catch files that should be excluded',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='With --push: on a non-fast-forward rejection, OVERWRITE the remote '
             '(force-push branch + move existing release tags). Default preserves '
             'remote history via an ours-merge fast-forward instead',
    )
    parser.add_argument(
        '--worktree', action='store_true',
        help='Export the WORKTREE including uncommitted changes (legacy behavior). '
             'Default internal/opensource exports copy an immutable committed '
             'ref (HEAD, or refs/tofu/stable for an opted-in linear repository) '
             'via git archive, so in-flight edits never tear the published tree. '
             'With --handoff (internal): the release branch mirrors the current '
             'tree — dirty tracked files AND untracked source files ship',
    )
    parser.add_argument(
        '--handoff', metavar='DIR',
        help='Cross-cluster handoff: write only bootstrap.sh + tofu_state.tar.gz '
             '(skills/memories/config) to DIR instead of copying the tree. The '
             'remote host runs bootstrap.sh to git-clone + restore state + '
             'install — no cross-cluster file copy.',
    )
    parser.add_argument(
        '--git-url',
        help='Clone URL baked into the handoff bootstrap. Internal mode '
             'defaults to the release repo (git.sankuai.com, branch master); '
             'required for personal mode. With --push, internal exports a '
             'sanitized snapshot and pushes it onto master; personal pushes '
             'HEAD to this URL.',
    )

    args = parser.parse_args()

    if args.release and args.bump:
        parser.error('--release publishes prepared metadata; do not combine it with --bump')
    if args.history_base and args.mode != 'opensource':
        parser.error('--history-base applies only to Open commit synchronization')
    if args.mode == 'opensource' and args.push and args.bump:
        parser.error('Prepare all version metadata and commit it before publishing; use --release')
    # ── Cross-cluster handoff: small artifacts, git is the transport ──
    if args.handoff:
        if args.worktree and args.mode != 'internal':
            parser.error('--handoff (personal) pushes the committed branch verbatim; '
                         '--worktree is meaningless there')
        if args.bump:
            parser.error('--handoff does not export a tree; --bump is meaningless here')
        run_handoff(args.mode, Path(args.handoff).resolve(), args.git_url,
                    push=args.push, dry_run=args.dry_run, worktree=args.worktree)
        return

    # ── Internal mode NEVER pushes to GitHub (user policy) ──
    # Internal is for local/self-use sharing only; silently disable --push.
    # (Handoff is exempt: its --push snapshot-pushes the sanitized export
    # onto the internal release repo on corp GitLab — never GitHub.)
    if args.mode == 'internal' and args.push and not args.handoff:
        print(f"  {C_YELLOW}\u26a0 --push is disabled for internal mode; skipping git push.{C_END}")
        logger.info('--push ignored for internal mode (disabled by policy)')
        args.push = False

    # ── Handle version bumping ──
    version = _read_version()
    if args.bump:
        if args.dry_run:
            # Preview only — compute the next version WITHOUT writing/committing.
            version = _next_version(version, args.bump)
            print(f"  {C_CYAN}\U0001f4cc Version: v{version} "
                  f"{C_DIM}(dry run — VERSION not written){C_END}")
        else:
            version = _bump_version(args.bump)
            print(f"  {C_CYAN}\U0001f4cc Version: v{version}{C_END}")
            # Commit the bump into THIS origin tree so origin's VERSION and the
            # published release tag never drift.
            _commit_version_to_origin(version)
    else:
        print(f"  {C_DIM}Version: v{version} (use --bump patch/minor/major to increment){C_END}")

    # Only prefix commit message with version tag on explicit releases (--bump)
    commit_msg = args.msg
    is_release = bool(args.bump or args.release)
    if args.push and not args.dry_run:
        if is_release:
            # Release commit: prefix with version tag
            if commit_msg:
                commit_msg = f'v{version}: {commit_msg}'
            else:
                commit_msg = f'v{version}'
        else:
            # Regular push: use message as-is, or a timestamped default
            if not commit_msg:
                from datetime import datetime as _dt
                commit_msg = f'update {_dt.now().strftime("%Y-%m-%d %H:%M")}'

    if args.dest:
        dest = Path(args.dest).resolve()
    else:
        _default_dirs = {
            'personal': 'tofu-personal',
            'internal': 'tofu-meituan',
            'opensource': 'tofu-open',
        }
        base_dir = _default_dirs[args.mode]
        # When using a non-default branch, isolate to a branch-specific
        # export directory.  This prevents .git history contamination when
        # the same repo is pushed to different branches from different
        # source projects (e.g. main from tofu/, cli_switch from tofu_branch/).
        default_branch = _GIT_REPOS.get(args.mode, {}).get('branch')
        if args.branch and args.branch != default_branch:
            base_dir = f'{base_dir}--{args.branch}'
        dest = ROOT.parent / base_dir

    export_project(args.mode, dest, dry_run=args.dry_run,
                   push=args.push, commit_msg=commit_msg,
                   is_release=is_release, branch_override=args.branch,
                   progress=args.progress, force=args.force,
                   worktree=args.worktree, history_base=args.history_base)


if __name__ == '__main__':
    main()
