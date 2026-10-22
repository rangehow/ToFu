"""Core export summary / cleanup / post-export transform helpers (split out of
``export.py``)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

from export_pkg import _state
from export_pkg._lists import (
    ALWAYS_EXCLUDE_DIRS,
    C_BOLD,
    C_DIM,
    C_END,
    C_GREEN,
    C_RED,
    C_YELLOW,
    OPENSOURCE_EXTRA_EXCLUDE_DIRS,
    ROOT,
    _FORCE_STRIP_FROM_DEST,
    _OPENSOURCE_DEST_PRESERVE,
    _OPENSOURCE_KEEP_FILES,
    _TOP_LEVEL_ONLY_EXCLUDE_DIRS,
    logger,
)

import textwrap

from export_pkg._excludes import _is_text_file, _safe_export_relative_path
from export_pkg._internal import (
    _bundle_internal_mcp_repos,
    _bundle_tofu_search_wheel,
    _copy_installed_skills,
    _patch_install_sh_proxy,
    _portablize_bundled_mcp_config,
    _purge_runtime_artifacts,
    _seed_internal_config,
    _stage_portable_mcp_config,
)
from export_pkg._sanitize import _sanitize_source_opensource
from export_pkg._verify import ExportIntegrityError


def _print_export_summary(dest: Path, mode: str, stats: dict,
                           excluded_log: list, all_excluded_dirs: set) -> None:
    """Print the standard end-of-export summary block.

    Extracted from ``export_project`` so both the fast path and the
    dry-run walk path produce identical output.
    """
    print(f"\n{C_BOLD}{chr(0x2500) * 64}{C_END}")
    print(f"  {C_GREEN}\u2713 Copied:    {stats['copied']}{C_END}")
    if stats.get('sanitized'):
        print(f"  {C_YELLOW}\U0001f527 Sanitized: {stats['sanitized']}{C_END}")
    if stats.get('excluded'):
        print(f"  {C_RED}\u2717 Excluded:  {stats['excluded']}{C_END}")

    if stats.get('excluded_reasons'):
        print(f"\n  {C_DIM}Exclusion breakdown:{C_END}")
        for reason, count in sorted(stats['excluded_reasons'].items(),
                                     key=lambda x: -x[1]):
            print(f"    {C_DIM}{reason}: {count}{C_END}")

    if excluded_log:
        print(f"\n  {C_DIM}Key excluded items:{C_END}")
        shown_dirs: set = set()
        for relpath, reason in excluded_log:
            parts = Path(relpath).parts
            for part in parts:
                if part in all_excluded_dirs or (
                    mode == 'opensource' and part in OPENSOURCE_EXTRA_EXCLUDE_DIRS
                ):
                    if part not in shown_dirs:
                        shown_dirs.add(part)
                        print(f"    {C_RED}\u2717{C_END} "
                              f"{C_DIM}{part}/ (entire directory){C_END}")
                    break
            else:
                print(f"    {C_RED}\u2717{C_END} {C_DIM}{relpath} \u2014 {reason}{C_END}")

    print(f"\n  {C_GREEN}{chr(0x2550) * 50}{C_END}")
    print(f"  {C_GREEN}\u2705 Export complete!{C_END}")
    print(f"  {C_GREEN}   {dest}{C_END}")
    print(f"  {C_GREEN}{chr(0x2550) * 50}{C_END}")
    print(f"\n  {C_BOLD}Next steps:{C_END}")
    print(f"  1. cd {dest}")
    if mode == 'personal':
        print(f"  2. python3 server.py          {C_DIM}# everything is ready as-is{C_END}")
    elif mode == 'internal':
        print(f"  2. python3 server.py          {C_DIM}# auto-creates SQLite database{C_END}")
    else:
        print(f"  2. cp .env.example .env       {C_DIM}# fill in your API key{C_END}")
        print(f"  3. python3 server.py          {C_DIM}# auto-creates SQLite database{C_END}")
    print(f"\n  {C_DIM}If running on the same machine as the original:{C_END}")
    print(f"  {C_DIM}  PORT=15001 python3 server.py  # use a different web port{C_END}")
    print(f"  {C_DIM}  (SQLite — no port conflicts possible){C_END}")
    print()


# Runtime/operator state that may exist in a dest doubling as a live
def _dest_cleanup_targets(dest: Path, mode: str, source_names: set) -> list:
    """Top-level items of ``dest`` to delete before the tar copy.

    Non-opensource dests are LIVE INSTALLS: preserve user data AND every
    export-excluded dir (rm -rf'ing a 5k-file workdir is 100% wasted FUSE
    I/O — the next tar copy wouldn't put anything back there anyway), plus
    anything not in source (the user may have created it post-export).

    An opensource dest is a PUBLISH MIRROR of the export set, not a live
    install: preserve ONLY operator/runtime state
    (:data:`_OPENSOURCE_DEST_PRESERVE`); every other top-level item —
    including export-EXCLUDED content dirs (promo/, overleaf_cache/) and
    stale entries no longer in source — is deleted so the published tree
    equals the export set. Without this, excluded content committed once
    rides every subsequent ``git add -A`` forever: this is exactly how
    promo/ (~26MB) stayed on GitHub years after entering
    ALWAYS_EXCLUDE_DIRS (2026-06-10), doubling the update tarball.
    """
    if mode == 'opensource':
        preserve = _OPENSOURCE_DEST_PRESERVE
        mirror_delete = True
    else:
        preserve = ({'.git', 'data', 'uploads'} | ALWAYS_EXCLUDE_DIRS
                    | OPENSOURCE_EXTRA_EXCLUDE_DIRS
                    | _TOP_LEVEL_ONLY_EXCLUDE_DIRS)
        mirror_delete = False
    targets = []
    for item in dest.iterdir():
        if item.name in _FORCE_STRIP_FROM_DEST:
            targets.append(item)
            continue
        if item.name in preserve:
            continue
        if not mirror_delete and item.name not in source_names:
            # Not in source → tar won't write here → no need to delete.
            continue
        targets.append(item)
    return targets

def _restore_export_kept_files(dest: Path, mode: str) -> None:
    """Restore allowlisted build/test dependencies for a shareable export.

    Both tiers omit development directories that shipped tests still import.
    The existing keep-list owns that dependency closure. Public copies pass
    through the public sanitizer; internal copies retain their source semantics.
    Git ignore exceptions ensure these files reach the actual publication index.
    """
    if mode not in ('internal', 'opensource'):
        raise ValueError(f'Unsupported retained-source export mode: {mode}')
    # Capture the immutable source once: neither later HEAD movement nor dirty
    # helper edits may change the build dependency bytes selected for this run.
    source_sha = _state._EXPORT_SOURCE_SHA
    if source_sha is not None and not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', source_sha):
        raise ExportIntegrityError('Invalid pinned SHA for retained source')

    def git_bytes(args: list[str]) -> bytes:
        try:
            result = subprocess.run(['git', *args], cwd=ROOT,
                                    capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ExportIntegrityError(f'Could not read pinned retained source: {error}') from error
        if result.returncode:
            raise ExportIntegrityError('Could not read retained source from pinned SHA')
        return result.stdout

    restored = []
    for rel in sorted(_OPENSOURCE_KEEP_FILES):
        try:
            relative = _safe_export_relative_path(rel)
        except (ValueError, UnicodeError) as error:
            raise ExportIntegrityError(f'Unsafe retained source path: {rel!r}') from error
        src = ROOT / relative
        target = dest / relative
        try:
            if source_sha is not None:
                entry = git_bytes(['ls-tree', '-z', source_sha, '--', rel]).rstrip(b'\0')
                meta, separator, raw_path = entry.partition(b'\t')
                fields = meta.split()
                if (not separator or raw_path != rel.encode('utf-8') or len(fields) != 3
                        or fields[0] not in {b'100644', b'100755'} or fields[1] != b'blob'):
                    raise ExportIntegrityError(f'Missing or unsafe pinned retained source: {rel}')
                content = git_bytes(['show', f'{source_sha}:{rel}'])
            else:
                # Only the explicitly mutable/non-Git export path reads ROOT.
                if src.is_symlink():
                    raise ExportIntegrityError(f'Unsafe retained source symlink: {rel}')
                if not src.is_file():
                    logger.warning('Keep-file missing from source, skipping: %s', rel)
                    continue
                content = src.read_bytes()
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                raise ExportIntegrityError(f'Unsafe retained source destination: {rel}')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if source_sha is not None:
                target.chmod(0o755 if fields[0] == b'100755' else 0o644)
            else:
                shutil.copystat(src, target)
            if mode == 'opensource' and _is_text_file(str(target)):
                text = content.decode('utf-8')
                scrubbed = _sanitize_source_opensource(text, rel)
                if scrubbed != text:
                    target.write_bytes(scrubbed.encode('utf-8'))
            restored.append(rel)
        except OSError as error:
            raise ExportIntegrityError(f'Failed to restore retained source {rel}: {error}') from error
    ignore_file = dest / '.gitignore'
    if ignore_file.is_file():
        tier = 'public' if mode == 'opensource' else 'internal'
        marker = f'# BEGIN tofu {tier} retained source\n'
        original = ignore_file.read_text(encoding='utf-8').split(marker)[0].rstrip()
        parents = {parent.as_posix() for rel in restored
                   for parent in Path(rel).parents if parent.as_posix() != '.'}
        entries = [f'!/{parent}/' for parent in sorted(parents, key=lambda p: (p.count('/'), p))]
        entries.extend(f'!/{rel}' for rel in sorted(restored))
        ignore_file.write_text(
            original + '\n\n' + marker + '\n'.join(entries) + '\n', encoding='utf-8')
    if restored:
        print(f"  {C_GREEN}📌 Restored build-required files ({mode}){C_END}")
        print(f"     {C_DIM}{', '.join(restored)}{C_END}")


def _restore_opensource_kept_files(dest: Path) -> None:
    """Compatibility facade for the public retained-source transform."""
    _restore_export_kept_files(dest, 'opensource')


def _regenerate_exported_runtime(dest: Path) -> None:
    """Recompose retained outputs from the exact sanitized public sources."""
    if not (dest / 'frontend/src/runtime/sections/manifest.json').is_file():
        return
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Public runtime regeneration requires Node on the export host')
    for generator in ('compose_frontend_runtime.mjs', 'update_runtime_actions.mjs'):
        result = subprocess.run(
            [node, str(dest / 'scripts' / generator), '--write'],
            cwd=dest, capture_output=True, text=True, timeout=60,
            env={**os.environ, 'NODE_PATH': str(ROOT / 'node_modules')},
        )
        if result.returncode:
            raise RuntimeError(
                f'Public runtime regeneration failed ({generator}): '
                + (result.stderr or result.stdout)[-2000:])


def _run_ruff_autofix(dest: Path):
    """Run ruff --fix on the exported code to auto-fix lint issues.

    This ensures the exported code stays clean even if the source has minor
    style drift (import sorting, deprecated typing imports, Optional→X|None,
    etc.).  All auto-fixable rules are applied.

    CRITICAL: ``--unfixable F401,F811`` forbids the autofix from ever DELETING
    imports (or duplicate defs), even though those diagnostics are still
    reported. Tofu's facade-preserving split packages import symbols in a
    submodule purely so the package ``__init__`` can re-export them; ruff sees
    those as "unused" (F401) and, left to its own devices, would delete them —
    silently breaking ``import lib.mcp`` / ``import lib.tasks_pkg.orchestrator``
    at boot in the shipped opensource build. The source tree is already
    lint-clean, so the export never needs to strip imports to stay green;
    removing an import is pure downside here. Keep this guard.
    """
    print(f"\n  {C_BOLD}🔧 Running ruff auto-fix on exported code…{C_END}")
    try:
        result = subprocess.run(
            ['python', '-m', 'ruff', 'check', '--fix', '--unsafe-fixes',
             '--unfixable', 'F401,F811',
             str(dest / 'lib'), str(dest / 'routes'), str(dest / 'tests')],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout or '').strip()
        if 'error' in output.lower() or result.returncode != 0:
            # Some unfixable errors remain — that's OK, just report
            fixed_match = re.search(r'(\d+) fixed', output)
            remain_match = re.search(r'(\d+) remaining', output)
            fixed = fixed_match.group(1) if fixed_match else '0'
            remaining = remain_match.group(1) if remain_match else '?'
            print(f"    {C_YELLOW}Auto-fixed {fixed} issues, {remaining} remaining{C_END}")
        elif 'Found' in output:
            fixed_match = re.search(r'(\d+) fixed', output)
            fixed = fixed_match.group(1) if fixed_match else '0'
            print(f"    {C_GREEN}✅ Auto-fixed {fixed} lint issues{C_END}")
        else:
            print(f"    {C_GREEN}✅ No lint issues found{C_END}")
    except FileNotFoundError:
        print(f"    {C_DIM}ruff not installed — skipping auto-fix{C_END}")
    except subprocess.TimeoutExpired:
        print(f"    {C_YELLOW}ruff timed out — skipping{C_END}")
    except Exception as e:
        print(f"    {C_YELLOW}ruff auto-fix failed: {e}{C_END}")


# ═══════════════════════════════════════════════════════════
#  Skeleton creation (full copy + per-mode adjustments)
# ═══════════════════════════════════════════════════════════
def _create_skeleton(dest: Path, mode: str):
    """Create empty directories and placeholder files that the app expects."""
    # ── FIRST: purge transient runtime artifacts from a previous run ──
    # Logs, lockfiles, and key-stats counters are regenerable noise and
    # safe to remove.  Databases (chatui.db, pgdata/) and uploads/ are
    # PRESERVED so the destination user's chat history survives re-exports.
    _purge_runtime_artifacts(dest)

    # Empty dirs the app needs
    for d in ['data', 'data/config', 'logs', 'uploads/images']:
        (dest / d).mkdir(parents=True, exist_ok=True)
        gitkeep = dest / d / '.gitkeep'
        if not gitkeep.exists():
            gitkeep.touch()

    # ── Internal mode: seed server_config.json with providers & shared settings ──
    # Without this, colleagues get an empty config and "API not found" errors.
    # Only seeds if the destination doesn't already have one (don't overwrite
    # user's own config on re-export).
    if mode == 'internal':
        _seed_internal_config(dest)
        # The bulk copy strips mcp_servers.json (it holds personal tokens), so
        # an internal destination would otherwise boot with NO MCP servers even
        # though vendor/ carries the code. Stage the portable subset here, after
        # the config seed has created data/config/.
        _stage_portable_mcp_config(dest)

    # Installed skills are user state under the two roots the bulk copy strips
    # (``.tofu*`` and root ``data/``). Carry them so an internal export arrives
    # with the team's skill catalogue already installed.
    _copy_installed_skills(dest, mode)

    # ── Personal & internal: bundle sibling MCP server repos (hope-mcp,
    # xuecheng-mcp) under vendor/ so install.sh can pip-install them.
    # These are private Meituan-internal repos not published to PyPI, so
    # a fresh destination install has no way to fetch them otherwise.
    # Opensource mode skips this entirely (repos are internal-only).
    _bundle_internal_mcp_repos(dest, mode)

    # ── Personal & internal: bundle the prebuilt tofu-search wheel under
    # vendor/ so install.sh can pip-install it behind a corp mirror that
    # lacks the package. Opensource installs it from public PyPI instead.
    _bundle_tofu_search_wheel(dest, mode)

    # ── Personal & internal: rewrite hope/xuecheng entries in the copied
    # mcp_servers.json from absolute source-machine `uvx --from /path` to
    # bare `hope-mcp` / `xuecheng-mcp` launchers, so the persisted config
    # matches the vendor/ pip-install and works on any destination machine.
    _portablize_bundled_mcp_config(dest, mode)

    # ── Personal & internal: bake corp proxy into install.sh ──
    # install.sh runs BEFORE server.py boots and can't read server_config.json,
    # but every step needs outbound HTTPS.  Inject the proxy env-vars near the
    # top of install.sh so colleagues / a fresh personal copy can install
    # behind the corp firewall without manually exporting http_proxy first.
    _patch_install_sh_proxy(dest, mode)

    # .gitignore for uploads/images
    gi = dest / 'uploads' / 'images' / '.gitignore'
    gi.write_text('*\n!.gitkeep\n')

    # Ensure tests/screenshots dir exists
    ss_dir = dest / 'tests' / 'screenshots'
    if ss_dir.exists():
        gi_ss = ss_dir / '.gitignore'
        if not gi_ss.exists():
            gi_ss.write_text('*.png\n')

    # A public export is a source distribution: never materialize mutable
    # configuration inside it. Users create .env from the tracked template.
    # Internal exports remain runnable copies and receive their local defaults.
    if mode != 'opensource':
        env_file = dest / '.env'
        env_lines = [
            '# Auto-generated by export.py',
            '# PPTX translation module is OFF by default.',
            '# Set PPTX_TRANSLATE_ENABLED=1 to enable it.',
            'PPTX_TRANSLATE_ENABLED=0',
            '',
            '# This is a standalone single-machine copy. Never defer to a remote',
            '# PostgreSQL owner recorded in an inherited pgdata: on shared storage a',
            '# copy sees pgdata at the same absolute path as the source, so the',
            '# .pg_owner_host / heartbeat left by the source machine (or a previous',
            '# container) would otherwise route every DB call across a dead link and',
            '# crash with "connection ... timeout expired". With this flag set, the',
            '# copy clears the inherited marker on startup and owns PG locally.',
            '# Unset it ONLY for a same-path multi-host failover deployment.',
            'TOFU_PG_STANDALONE=1',
            '',
        ]
        env_file.write_text('\n'.join(env_lines))
        logger.info('Created .env (PPTX_TRANSLATE_ENABLED=0, TOFU_PG_STANDALONE=1)')

    # .env.example should have been copied from source.  Create a minimal
    # fallback only if the copy is missing (e.g. excluded by a filter).
    env_example = dest / '.env.example'
    if not env_example.exists():
        env_example.write_text(textwrap.dedent("""\
            # Tofu — Environment Configuration
            # Copy this file to .env and fill in your values:
            #   cp .env.example .env
            #
            # See README.md "Environment Variables" section for full reference.

            # LLM_API_KEYS=sk-key1,sk-key2
            # LLM_BASE_URL=https://api.openai.com/v1
            # LLM_MODEL=gpt-4o
            # PORT=15000
            # TRADING_ENABLED=0
            # PPTX_TRANSLATE_ENABLED=0
        """))
