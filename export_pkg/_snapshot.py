"""Git-based untracked/dirty-tree scanning for the export (split out of
``export.py``)."""
import subprocess
from pathlib import Path

from export_pkg._lists import C_DIM, C_END, C_RED, C_YELLOW, _have_tool, logger

import shutil
import tempfile

from export_pkg import _state
from export_pkg._verify import (
    ExportIntegrityError,
)
from lib.linear_git_checkpoint import (
    LinearCheckpointError,
    preferred_export_ref,
)


def _gitignored_excludes(src: Path) -> list[str]:
    """Return tar ``--exclude=`` args for every path git ignores in ``src``.

    Runs ``git ls-files -o -i --exclude-standard --directory`` inside the
    source tree, which honours ``.gitignore`` / ``.git/info/exclude`` /
    the global excludesfile AND collapses a fully-ignored directory to a
    single entry — so we never enumerate the 800K+ files inside eval
    ``*_workdir/`` trees. This keeps the export's exclusions in lock-step
    with ``.gitignore`` instead of drifting against the hand-maintained
    ``ALWAYS_EXCLUDE_DIRS`` set.

    Best-effort: returns ``[]`` if ``src`` is not a git repo or git is
    unavailable. Used for internal/opensource exports only — personal mode
    deliberately keeps gitignored data (``.env``, ``data/``, ``uploads/``).

    Args:
        src: Source project root (the git work tree).

    Returns:
        A list of ``--exclude=./<path>`` args, one per ignored entry.
    """
    if not _have_tool('git'):
        logger.debug('git not on PATH; skipping gitignore-based excludes')
        return []
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '-o', '-i',
             '--exclude-standard', '--directory'],
            cwd=str(src), capture_output=True, text=True,
        )
    except Exception as e:
        logger.warning('git ls-files for gitignore excludes failed: %s', e)
        return []
    if proc.returncode != 0:
        logger.debug('git ls-files rc=%s (src not a repo?); '
                     'skipping gitignore excludes: %.200s',
                     proc.returncode, proc.stderr)
        return []

    excludes: list[str] = []
    for raw in proc.stdout.split('\0'):
        path = raw.strip().rstrip('/')
        if not path:
            continue
        excludes.append(f'--exclude=./{path}')
    logger.info('[Export] Honouring .gitignore: %d ignored path(s) excluded',
                len(excludes))
    return excludes


def _untracked_root_dirs(src: Path) -> set[str]:
    """Return the names of untracked, NON-gitignored ROOT directories.

    ``_gitignored_excludes`` handles paths git IGNORES, and the hand-maintained
    ``ALWAYS_EXCLUDE_*`` sets handle known product dirs. But a directory that is
    BOTH untracked AND not gitignored falls through both — e.g. agent scratch a
    code-exec run leaves at the repo root (``module1/``, ``module2/``), a stray
    ``scratchpad/`` / ``fix-xr/``, a ``.pytest_cache/`` or ``*.local_bak.*``
    backup. tar copies it verbatim, leaking non-product junk into the export
    (and, for opensource, breaking the ruff gate on unformatted scratch Python).

    We target ROOT-LEVEL DIRECTORIES only: ``git ls-files -o --directory``
    collapses a fully-untracked directory to a single ``dir/`` entry, while
    listing untracked files INSIDE a tracked dir (a new ``lib/foo.py``, a new
    ``tests/**/test_*.py``) individually — those are legitimate new source and MUST
    NOT be excluded. So we keep only depth-1 ``dir/`` entries and ignore every
    file entry.

    Pure query (no printing / no side effects) so BOTH the real tar-copy path
    (via ``_untracked_root_excludes``) and the ``--dry-run`` preview walk can
    consult the SAME set — otherwise the preview would list ``module1/`` etc. as
    "will copy" while the real run drops them, and an operator trusting the
    pre-flight gets bitten at release time.

    Best-effort: returns an empty set if ``src`` is not a git repo or git is
    unavailable. Used for internal/opensource exports only — personal mode is a
    full self-use backup and keeps everything.

    Args:
        src: Source project root (the git work tree).

    Returns:
        A set of root directory names (no ``./`` prefix, no trailing ``/``).
    """
    if not _have_tool('git'):
        logger.debug('git not on PATH; skipping untracked-root scan')
        return set()
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '-o',
             '--exclude-standard', '--directory'],
            cwd=str(src), capture_output=True, text=True,
        )
    except Exception as e:
        logger.warning('git ls-files for untracked-root scan failed: %s', e)
        return set()
    if proc.returncode != 0:
        logger.debug('git ls-files -o rc=%s (src not a repo?); '
                     'skipping untracked-root scan: %.200s',
                     proc.returncode, proc.stderr)
        return set()

    stray_dirs: set[str] = set()
    for raw in proc.stdout.split('\0'):
        entry = raw.strip()
        # Only fully-untracked DIRECTORIES (git appends a trailing '/'). File
        # entries are untracked files inside tracked dirs — legit new source.
        if not entry or not entry.endswith('/'):
            continue
        path = entry.rstrip('/')
        # Root-level only: a nested collapsed dir means its parent is tracked,
        # so it's a new source subtree, not a stray top-level scratch dir.
        if '/' in path:
            continue
        stray_dirs.add(path)
    return stray_dirs


def _untracked_nested_files(src: Path, stray_root_dirs: set[str]) -> set[str]:
    """Untracked, non-gitignored FILES that would otherwise ship verbatim.

    The root-dir rule collapses stray top-level dirs, but an untracked FILE
    inside a TRACKED dir used to slip straight into the public repo —
    measured 2026-08-05: the uncommitted tests/test_frontend_autopilot_run_notice.py
    shipped to rangehow/ToFu and red-filed the public CI, and the
    tests/tmp*.js NC-harness temp copies rode along the same way. The
    published tree is a MIRROR OF COMMITTED SOURCE: if a file is worth
    publishing it is worth ``git add``ing — tracking IS the keeper
    mechanism, so there is deliberately no second allowlist here.

    ``git ls-files -o --exclude-standard`` (no ``--directory``) enumerates
    every untracked file individually; entries under ``stray_root_dirs`` are
    already covered by the root-dir rule and filtered out so the exclude
    list stays short. Root-level untracked FILES (``_fe.tmp.js``,
    ``JOURNAL.md.lock``, …) are included — the same argument applies.

    Best-effort: empty set when git is unavailable / src is not a repo.
    Personal mode never consults this (full self-use backup).
    """
    if not _have_tool('git'):
        return set()
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '-o', '--exclude-standard'],
            cwd=str(src), capture_output=True, text=True,
        )
    except Exception as e:
        logger.warning('git ls-files for untracked-file scan failed: %s', e)
        return set()
    if proc.returncode != 0:
        logger.debug('git ls-files -o (files) rc=%s — skipping: %.200s',
                     proc.returncode, proc.stderr)
        return set()
    out: set[str] = set()
    for raw in proc.stdout.split('\0'):
        entry = raw.strip()
        if not entry or entry.endswith('/'):
            continue
        top = entry.split('/', 1)[0]
        if '/' in entry and top in stray_root_dirs:
            continue  # already excluded by the root-dir rule
        out.add(entry)
    return out


def _untracked_file_excludes(src: Path) -> list[str]:
    """tar excludes for :func:`_untracked_nested_files`, with the same loud
    operator-facing summary the root-dir rule gives."""
    stray = _untracked_nested_files(src, _untracked_root_dirs(src))
    if stray:
        shown = sorted(stray)
        head = ', '.join(shown[:8]) + (f' … (+{len(shown) - 8} more)' if len(shown) > 8 else '')
        logger.warning('[Export] Skipping %d untracked, non-gitignored '
                       'file(s) in tracked dirs: %s', len(shown), head)
        print(f"  {C_YELLOW}\u26a0 Skipping {len(shown)} untracked file(s) "
              f"(not committed — the export mirrors committed source): {head}{C_END}")
        print(f"  {C_DIM}  \u2192 if any is real source, `git add` it before "
              f"exporting (or `.gitignore` it if it's junk).{C_END}")
    return [f'--exclude=./{f}' for f in sorted(stray)]


def _untracked_root_excludes(src: Path) -> list[str]:
    """Build tar ``--exclude=./<dir>`` args for the stray root dirs.

    Thin wrapper over :func:`_untracked_root_dirs` that formats each name as a
    tar exclude AND surfaces the set loudly (log + terminal warning), so a
    genuinely-new source directory the operator forgot to ``git add`` is not
    silently dropped from the real copy.
    """
    stray_dirs = _untracked_root_dirs(src)
    if stray_dirs:
        listed = ', '.join(sorted(stray_dirs))
        logger.warning('[Export] Skipping %d untracked, non-gitignored '
                       'root dir(s): %s', len(stray_dirs), listed)
        print(f"  {C_YELLOW}\u26a0 Skipping {len(stray_dirs)} untracked, "
              f"non-gitignored root dir(s) (agent scratch?): {listed}{C_END}")
        print(f"  {C_DIM}  \u2192 if any is real source, `git add` it before "
              f"exporting (or `.gitignore` it if it's junk).{C_END}")
    return [f'--exclude=./{d}' for d in sorted(stray_dirs)]


def _dirty_tracked_files(src: Path) -> list[str]:
    """Tracked files whose worktree content differs from HEAD (staged or not).

    The export copies the WORKTREE but skips untracked files, so a dirty
    tracked file is the only way content that is not in HEAD (and may
    reference skipped untracked files) reaches the published tree.
    """
    if not _have_tool('git'):
        return []
    try:
        proc = subprocess.run(
            ['git', 'diff', '--name-only', '-z', 'HEAD'],
            cwd=str(src), capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.debug('git diff for torn-snapshot guard failed: %s', e)
        return []
    if proc.returncode != 0:
        return []
    return [e for e in proc.stdout.split('\0') if e.strip()]


def _torn_snapshot_pairs(src: Path) -> list[tuple[str, str]]:
    """(dirty tracked file, skipped untracked file it references) pairs.

    Incident 2026-08-06: a sibling's uncommitted server.py edit imported
    lib/logging_pkg/log_aggregates.py while that module was still untracked. The export
    shipped the dirty server.py (tracked) but dropped the module (untracked
    skip) — a TORN snapshot that red-filed public CI with 800+
    ModuleNotFoundError cascades. The untracked-file skip ("the export
    mirrors committed source") is only sound when dirty tracked files do
    not reference the files being skipped; this guard detects exactly that
    cross-reference so the operator can commit (or pass --allow-dirty)
    instead of publishing a tree that cannot even be imported.
    """
    skipped = _untracked_nested_files(src, _untracked_root_dirs(src))
    if not skipped:
        return []
    # Reference token per skipped file: its repo-relative path
    # ('lib/design_sys/fonts.py') — matched as a substring it also covers
    # leading-slash ('/static/…') web references — and, for Python files,
    # the dotted module ('lib.design_sys.fonts'; a package's __init__.py
    # contributes the package name 'lib.design_sys'). Bare stems ('fonts',
    # 'index') are far too generic — measured 2026-08-06: stem matching
    # false-positived on every preview/index/fonts word in the tree.
    tokens: dict[str, str] = {}
    for rel in sorted(skipped):
        tokens.setdefault(rel, rel)
        p = Path(rel)
        if p.suffix == '.py':
            dotted = rel[:-3].replace('/', '.')
            if p.stem == '__init__':
                dotted = dotted.rsplit('.', 1)[0]
            tokens.setdefault(dotted, rel)
    pairs: list[tuple[str, str]] = []
    for dirty in _dirty_tracked_files(src):
        # Prose never breaks an import: JOURNAL.md / docs routinely name
        # in-flight file paths (this project's JOURNAL convention), which
        # would otherwise false-trip the guard on every documented WIP.
        if Path(dirty).suffix.lower() in ('.md', '.rst', '.txt'):
            continue
        fpath = src / dirty
        try:
            if not fpath.is_file() or fpath.stat().st_size > 2_000_000:
                continue
            text = fpath.read_text(encoding='utf-8', errors='strict')
        except (OSError, UnicodeDecodeError):
            continue  # binary / unreadable — no textual reference possible
        for token, rel in tokens.items():
            if token in text and (dirty, rel) not in pairs:
                pairs.append((dirty, rel))
    return pairs


def _torn_snapshot_guard(src: Path, allow_dirty: bool,
                         dry_run: bool = False) -> None:
    """Abort (or loudly warn) when the export would publish a torn snapshot.

    See _torn_snapshot_pairs for the incident class. Best-effort: silent
    when git is unavailable or the tree is clean.
    """
    pairs = _torn_snapshot_pairs(src)
    if not pairs:
        return
    lines = '\n'.join(f'      {d}  →  references skipped untracked {u}'
                      for d, u in pairs[:12])
    more = f'\n      … (+{len(pairs) - 12} more)' if len(pairs) > 12 else ''
    head = ('DRY-RUN: torn snapshot detected'
            if dry_run
            else 'Torn snapshot detected (publishing anyway — --worktree)'
            if allow_dirty else 'Export aborted — torn snapshot detected')
    print(f"  {C_RED}✗ {head}:{C_END}\n"
          f"    Dirty tracked file(s) reference untracked file(s) that the\n"
          f"    export skips — the published tree would not even import:\n"
          f"{lines}{more}\n"
          f"    {C_DIM}→ `git add` the untracked file(s) (or commit the dirty\n"
          f"      ones away), or re-run with --worktree to publish the\n"
          f"      uncommitted state as-is.{C_END}")
    logger.error('Torn snapshot guard tripped: %s', pairs)
    if not dry_run and not allow_dirty:
        raise SystemExit(2)


# ═══════════════════════════════════════════════════════════
#  HEAD snapshot staging
# ═══════════════════════════════════════════════════════════
def _stage_head_snapshot(src: Path) -> Path | None:
    """Extract one committed ref into a local temp directory for export.

    The historical default remains ``HEAD``.  A repository that explicitly
    enables single-checkout linear checkpoints publishes ``refs/tofu/stable``
    (or its explicit ``tofu.exportRef`` / ``TOFU_EXPORT_SOURCE_REF``) instead.
    The chosen ref is resolved to a SHA before archive, so the copy remains
    immune to dirty files and concurrent ref movement.  Returns None (caller
    falls back to the worktree + torn-snapshot guard) only when Git/tar itself
    is unavailable; invalid explicit source-ref configuration fails closed.
    """
    if not _have_tool('git'):
        return None
    try:
        # Pin the sha FIRST and archive that exact commit: a sibling can land
        # a commit mid-export, and the post-copy integrity check must compare
        # against the SAME tree the snapshot was taken from (2026-08-06: a
        # mid-export design_sys commit made the HEAD-based check flag a file
        # the snapshot legitimately lacked).
        try:
            source_ref = preferred_export_ref(src)
        except LinearCheckpointError as error:
            raise ExportIntegrityError(
                f'Export source-ref configuration is unsafe: {error}') from error
        # Allocate only after source policy resolves. Invalid/missing explicit
        # refs fail closed without leaking an otherwise unreachable temp tree.
        tmp = Path(tempfile.mkdtemp(prefix='tofu-export-head-'))
        rev = subprocess.run(['git', 'rev-parse', '--verify',
                              f'{source_ref}^{{commit}}'], cwd=str(src),
                             capture_output=True, text=True, timeout=30)
        if rev.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        sha = rev.stdout.strip()
        arc = subprocess.Popen(['git', 'archive', '--format=tar', sha],
                               cwd=str(src), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        ext = subprocess.Popen(['tar', 'xf', '-'], cwd=str(tmp),
                               stdin=arc.stdout, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        if arc.stdout is not None:
            arc.stdout.close()
        _, xerr = ext.communicate()
        _, aerr = arc.communicate()
        if arc.returncode != 0 or ext.returncode != 0:
            logger.warning('Git snapshot failed ref=%s (arc rc=%s, tar rc=%s): %.200s %.200s',
                           source_ref,
                           arc.returncode, ext.returncode,
                           (aerr or b'').decode('utf-8', 'replace'),
                           (xerr or b'').decode('utf-8', 'replace'))
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        _state._EXPORT_SOURCE_SHA = sha
        return tmp
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('Git snapshot unavailable (%s) — worktree fallback', e)
        return None
