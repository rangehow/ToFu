"""lib.conversations.project_acceptance — the codified fresh-worktree
acceptance gate for landing a slice on this shared, contested working tree.

Why this exists
---------------
``project_commit`` proves, by byte-identity, that a commit contains no foreign
ADDITIONS — no sibling's hunks were swept in. It CANNOT prove the landed HEAD
is self-consistent: if a slice REMOVES a module-level symbol while a NON-slice
file still references it, the commit's --stat and zero-signature grep are both
clean, yet ``import`` breaks at HEAD. That is the exact split-brain the
2026-07-11 engine-core commit hit (``defer_task`` removed from
``project_board.py`` while ``routes/api_v1/project.py`` + ``lib/tools/
conversation.py`` still called it). Only a fresh checkout AT the candidate HEAD
surfaced it — a hand-run shell ritual, therefore unenforceable.

This module makes that ritual a callable with two layers:

``detect_orphaned_callers(base, slice_files, *, at_ref)``
    The load-bearing primitive, and deliberately WORKTREE-FREE. After a commit,
    every non-slice file stays exactly as it is at ``at_ref``. So the set of
    module-level symbols the slice REMOVES (present at ``at_ref``, absent in the
    working-tree slice file) — grepped across the ``at_ref`` tree EXCLUDING the
    slice paths — is precisely the set of callers the landed HEAD would orphan.
    No worktree, no test run: a cheap, deterministic split-brain predictor.

``run_acceptance_gate(base, *, files, test_paths, at_ref)``
    The full gate: spin a detached worktree at ``at_ref``, overlay the working-
    tree version of each declared slice file, provision a throwaway SQLite DB,
    run the declared pytest paths, AND run the orphan scan. ``ok`` is
    ``green AND selfConsistent`` — a slice can be test-green yet split-brained,
    so BOTH must hold. Best-effort cleanup of the worktree; never raises.

Neither function mutates the caller's working tree or index — the gate builds
its own throwaway worktree and the detector only reads.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_GIT_TIMEOUT = 120
_TEST_TIMEOUT = 900


def _git(base_path: str, *args: str) -> tuple[int, str, str]:
    """Run a git command; return ``(rc, stdout, stderr)``. Never raises for a
    non-zero exit — only a genuine spawn failure / timeout yields rc=-1."""
    try:
        p = subprocess.run(
            ['git', *args], cwd=base_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT, check=False,
        )
        return (p.returncode,
                p.stdout.decode('utf-8', 'replace'),
                p.stderr.decode('utf-8', 'replace'))
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('[Gate] git %s failed to run: %s', ' '.join(args), e)
        return -1, '', str(e)


def _is_git_repo(base_path: str) -> bool:
    rc, out, _ = _git(base_path, 'rev-parse', '--is-inside-work-tree')
    return rc == 0 and out.strip() == 'true'


def _norm(rel: str) -> str:
    """Project-relative posix path, stripping a literal leading ``./`` prefix
    (NOT ``lstrip('./')`` — a char-set that mangles dotfiles)."""
    return re.sub(r'^(?:\./)+', '', str(rel).replace('\\', '/'))


def _module_symbols(source: str) -> set[str]:
    """Module-level defined names in Python ``source``: top-level ``def`` /
    ``class`` / ``NAME = ...`` (and ``NAME: T = ...``). These are the names a
    sibling file can import or reference; nested defs are not importable so are
    excluded. Falls back to a regex scan if the source does not parse (e.g. a
    partial working-tree state), so the detector degrades safely rather than
    silently reporting nothing."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        for m in re.finditer(r'(?m)^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)',
                             source):
            names.add(m.group(1))
        for m in re.finditer(r'(?m)^([A-Z_][A-Z0-9_]*)\s*(?::[^=\n]+)?=', source):
            names.add(m.group(1))
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _blob_at_ref(base_path: str, ref: str, rel: str) -> str | None:
    """Content of ``rel`` at ``ref``, or None if the path does not exist there."""
    rc, out, _ = _git(base_path, 'show', f'{ref}:{rel}')
    if rc != 0:
        return None
    return out


def _read_working(base_path: str, rel: str) -> str | None:
    """Working-tree content of ``rel``; None if absent (slice deletes it)."""
    abs_p = os.path.join(os.path.abspath(base_path), rel)
    if not os.path.exists(abs_p):
        return None
    try:
        with open(abs_p, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError as e:
        logger.warning('[Gate] cannot read %s: %s', rel, e)
        return None


def _grep_symbol(base_path: str, ref: str, symbol: str,
                 exclude: set[str]) -> list[str]:
    """Files in the ``ref`` tree (excluding ``exclude`` slice paths) that
    reference ``symbol`` as a whole word. Uses ``git grep`` against the ref so
    it reads the COMMITTED tree, never the working tree."""
    rc, out, _ = _git(base_path, 'grep', '-l', '-w', '-e', symbol, ref, '--',
                       '*.py')
    if rc != 0:
        return []
    hits: list[str] = []
    for line in out.splitlines():
        # `git grep <ref>` prints 'ref:path'
        _, _, path = line.partition(':')
        path = _norm(path)
        if path and path not in exclude:
            hits.append(path)
    return hits


def detect_orphaned_callers(base_path: str, slice_files: list[str], *,
                            at_ref: str = 'HEAD') -> dict:
    """Predict whether committing ``slice_files`` leaves a split-brain at HEAD.

    Worktree-free. For each slice file: the module-level symbols present at
    ``at_ref`` but ABSENT in the current working-tree version are the REMOVED
    symbols. Any removed symbol still referenced (whole-word) by a NON-slice
    ``.py`` file at ``at_ref`` is an orphan → the landed HEAD would break.

    Returns ``{ok, selfConsistent, removedSymbols, orphans[], slice, error?}``
    where each orphan is ``{symbol, referencedBy[]}``.
    """
    out: dict = {'ok': False, 'selfConsistent': True, 'removedSymbols': [],
                 'orphans': [], 'slice': []}
    base = _norm_base(base_path)
    if not base or not _is_git_repo(base):
        out['error'] = 'not a git repository'
        return out
    slice_set = {_norm(f) for f in slice_files if f}
    out['slice'] = sorted(slice_set)
    if not slice_set:
        out['error'] = 'no slice files declared'
        return out

    removed: set[str] = set()
    for rel in sorted(slice_set):
        if not rel.endswith('.py'):
            continue
        head_src = _blob_at_ref(base, at_ref, rel)
        if head_src is None:
            # New file at at_ref (added by the slice) → removes nothing.
            continue
        head_syms = _module_symbols(head_src)
        work_src = _read_working(base, rel)
        work_syms = set() if work_src is None else _module_symbols(work_src)
        removed |= (head_syms - work_syms)

    out['removedSymbols'] = sorted(removed)
    for sym in sorted(removed):
        refs = _grep_symbol(base, at_ref, sym, slice_set)
        if refs:
            out['orphans'].append({'symbol': sym, 'referencedBy': sorted(set(refs))})
    out['selfConsistent'] = not out['orphans']
    out['ok'] = True
    return out


def _norm_base(project_path: str) -> str:
    try:
        from lib.conversations.project_feed import normalize_project_path
        return normalize_project_path(project_path)
    except Exception as e:  # normalize is best-effort; fall back to the raw path
        logger.debug('[Gate] normalize_project_path failed: %s', e)
        return project_path


def run_acceptance_gate(base_path: str, *, files: list[str],
                        test_paths: list[str], at_ref: str = 'HEAD') -> dict:
    """Run the full fresh-worktree acceptance gate for a candidate slice.

    Steps: (1) detached worktree at ``at_ref``; (2) overlay the working-tree
    version of each declared slice file onto it (a slice that DELETES a file
    removes it in the overlay too); (3) provision a throwaway SQLite DB
    (``TOFU_DB_PATH`` in the worktree, ``TOFU_REQUIRE_PG`` unset, ``TOFU_MLOCK=0``);
    (4) run the declared ``test_paths`` with ``-p no:napari``; (5) run the
    orphan scan against ``at_ref`` from the ORIGINAL repo.

    ``ok = green AND selfConsistent``. Best-effort worktree cleanup; the gate
    never raises — a failure to build/run returns ``ok=False`` with an error.
    """
    result: dict = {'ok': False, 'green': False, 'selfConsistent': True,
                    'orphans': [], 'testSummary': '', 'error': ''}
    base = _norm_base(base_path)
    if not base or not _is_git_repo(base):
        result['error'] = 'not a git repository'
        return result
    if not files:
        result['error'] = 'no slice files declared'
        return result
    if not test_paths:
        result['error'] = 'no test_paths declared'
        return result

    slice_files = [_norm(f) for f in files if f]

    # Step 5 first (cheap, worktree-free) so an obvious split-brain is reported
    # even if the worktree build later fails.
    scan = detect_orphaned_callers(base, slice_files, at_ref=at_ref)
    result['selfConsistent'] = scan.get('selfConsistent', True)
    result['orphans'] = scan.get('orphans', [])
    result['removedSymbols'] = scan.get('removedSymbols', [])

    wt = tempfile.mkdtemp(prefix='tofu_gate_')
    try:
        rc, _, err = _git(base, 'worktree', 'add', '--detach', wt, at_ref)
        if rc != 0:
            result['error'] = f'worktree add failed: {err.strip()[:200]}'
            return result

        # (2) Overlay the declared slice files (working-tree versions).
        for rel in slice_files:
            src = os.path.join(os.path.abspath(base), rel)
            dst = os.path.join(wt, rel)
            if not os.path.exists(src):
                # slice deletes this file → remove it from the overlay too
                if os.path.exists(dst):
                    try:
                        os.remove(dst)
                    except OSError as e:
                        logger.warning('[Gate] overlay-delete %s: %s', rel, e)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

        # (3) Throwaway DB + safe env.
        env = dict(os.environ)
        env['TOFU_MLOCK'] = '0'
        env.pop('TOFU_REQUIRE_PG', None)
        env['TOFU_DB_PATH'] = os.path.join(wt, '.tofu_gate.db')

        # (4) Run the declared tests.
        rc, out, errout = _run_pytest(wt, test_paths, env)
        summary = _tail(out + errout, 25)
        result['testSummary'] = summary
        result['green'] = (rc == 0)
        if rc != 0 and 'no tests ran' in summary.lower():
            result['green'] = False
    finally:
        _remove_worktree(base, wt)

    result['ok'] = bool(result['green'] and result['selfConsistent'])
    audit_log('acceptance_gate', project_path=base, at_ref=at_ref,
              files=len(slice_files), green=result['green'],
              self_consistent=result['selfConsistent'], ok=result['ok'])
    logger.info('[Gate] at=%s files=%d green=%s consistent=%s ok=%s',
                at_ref[:12], len(slice_files), result['green'],
                result['selfConsistent'], result['ok'])
    return result


def _run_pytest(cwd: str, test_paths: list[str], env: dict) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            [sys.executable, '-m', 'pytest', '-p', 'no:napari', '-q', *test_paths],
            cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_TEST_TIMEOUT, check=False,
        )
        return (p.returncode,
                p.stdout.decode('utf-8', 'replace'),
                p.stderr.decode('utf-8', 'replace'))
    except subprocess.TimeoutExpired as e:
        logger.warning('[Gate] pytest timed out after %ss', _TEST_TIMEOUT)
        return -1, '', f'pytest timed out: {e}'
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('[Gate] pytest failed to run: %s', e)
        return -1, '', str(e)


def _tail(text: str, n: int) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return '\n'.join(lines[-n:])


def _remove_worktree(base: str, wt: str) -> None:
    """Best-effort worktree teardown. Old git here lacks ``worktree remove``;
    fall back to rmtree + prune. Never raises."""
    rc, _, _ = _git(base, 'worktree', 'remove', '--force', wt)
    if rc != 0:
        try:
            shutil.rmtree(wt, ignore_errors=True)
        except OSError as e:
            logger.debug('[Gate] rmtree %s: %s', wt, e)
    _git(base, 'worktree', 'prune')


__all__ = ['detect_orphaned_callers', 'run_acceptance_gate']
