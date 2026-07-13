"""lib.conversations.project_commit — a SAFE, contamination-proof commit seam.

Why this exists
---------------
This repository runs with a large, persistent, MULTI-CONVERSATION uncommitted
working tree (hundreds of dirty files: the owner's in-progress work + several
sibling conversations each mid-epic, none committing until sign-off). In that
world raw git is a footgun for an agent:

* ``git add -A`` sweeps every sibling's half-finished work into one commit.
* ``git add -- <file>`` stages the file's ENTIRE working-tree diff — so if that
  same file also carries a live sibling's uncommitted hunks, those foreign
  hunks are swept in too (documented here as commits ballooning +414/+196).
* ``git commit -- <pathspec>`` commits the WORKING-TREE version of the path,
  silently overriding any partial staging (see the ``git-commit-pathspec-
  overrides-index`` project skill).

So agents correctly learned that "leave it uncommitted" is the safe default —
and work piles up. This module makes **"commit my own declared set"** the safe
default instead, by encoding every one of those traps into one fixed sequence.

The contamination gate (the load-bearing part)
-----------------------------------------------
File-level isolation ≠ hunk-level isolation. Knowing *which files* this
conversation touched (from file-history) does NOT stop a sibling's hunks that
landed in those same files from being swept in by ``git add <file>``.

The gate is therefore **byte-identity**, which is strictly stronger than a
``git diff --numstat`` magnitude check (a sibling replacing N of my lines with
N different lines has an identical numstat but different bytes): a candidate
file is CLEAN only if its current working-tree bytes reproduce EXACTLY the
post-image this conversation last recorded in file-history (``snapshots.jsonl``
pins, per round, the ``convId`` and the post-round blob version of each file).
Any file whose working tree diverges from this conversation's own last recorded
post-image carries changes this conversation did not make → CONTAMINATED →
EXCLUDED from the commit and reported back, never silently staged. ``numstat``
is still computed and surfaced as human-readable evidence.

Public API
----------
``plan_commit(project_path, conv_id, *, files)``
    Pure analysis — returns the clean / contaminated / ignored buckets. No
    mutation. Safe to call for a dry-run / sign-off preview. ``files`` is
    REQUIRED (the agent declares what it edited; no lifetime-derived default).
``do_commit(project_path, conv_id, message, *, files)``
    Runs the fixed sequence: stage ONLY the provably-clean set, verify the
    index holds exactly that set, ``git commit`` with NO pathspec, then verify
    the commit's file list equals the clean set. Never ``-A``, never a pathspec
    commit, never stash/checkout.
``execute_commit_tool(fn_args, *, current_conv_id, project_path)``
    Agent-tool entry → human-readable string.
"""
from __future__ import annotations

import os
import re
import subprocess

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# Generated JS bundle outputs — content-hashed (bundle-<8hex>.js /
# feature-<8hex>.js). An agent must NEVER commit these; only a release step
# rebuilds + commits them. This is where policy (b) lives at the tool boundary
# (a defence-in-depth on top of .gitignore, which does not cover an
# already-tracked bundle committed before the ignore rule existed).
_GENERATED_BUNDLE_RE = re.compile(r'(^|/)(bundle|feature)-[0-9a-f]{8}\.js$')

_GIT_TIMEOUT = 60


def _git(base_path: str, *args: str, _stdin: str | None = None) -> tuple[int, str, str]:
    """Run a git command in ``base_path``. Returns ``(returncode, stdout, stderr)``.

    Never raises for a non-zero exit — the caller inspects the code. Only a
    genuine spawn failure / timeout is caught, logged, and returned as rc=-1.
    ``_stdin`` feeds text to the process (used by ``check-ignore --stdin``).
    """
    try:
        p = subprocess.run(
            ['git', *args], cwd=base_path,
            input=(_stdin.encode('utf-8') if _stdin is not None else None),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT, check=False,
        )
        out = p.stdout.decode('utf-8', 'replace')
        err = p.stderr.decode('utf-8', 'replace')
        if p.returncode != 0:
            logger.debug('[Commit] git %s → rc=%d err=%.200s',
                         ' '.join(args), p.returncode, err)
        return p.returncode, out, err
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('[Commit] git %s failed to run: %s', ' '.join(args), e)
        return -1, '', str(e)


def _is_git_repo(base_path: str) -> bool:
    rc, out, _ = _git(base_path, 'rev-parse', '--is-inside-work-tree')
    return rc == 0 and out.strip() == 'true'


def _dirty_paths(base_path: str) -> set[str]:
    """Every path that differs from HEAD (modified/added/untracked), as
    project-relative posix paths — the set of files a commit could touch."""
    rc, out, _ = _git(base_path, 'status', '--porcelain', '-uall', '-z')
    if rc != 0:
        return set()
    paths: set[str] = set()
    for entry in out.split('\0'):
        if not entry:
            continue
        # porcelain -z: 'XY <path>' (rename 'XY <to>\0<from>' handled by split).
        path = entry[3:] if len(entry) > 3 else ''
        if path:
            paths.add(path.replace('\\', '/'))
    return paths


def _batch_numstat(base_path: str, rels: list[str]) -> dict:
    """One ``git diff --numstat`` for ALL paths → {rel: '+A/-D'}. Untracked
    files don't appear (nothing to diff) → absent = '' at lookup."""
    if not rels:
        return {}
    rc, out, _ = _git(base_path, 'diff', '--numstat', '--', *rels)
    if rc != 0:
        return {}
    stats: dict = {}
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) >= 3:
            add, dele, path = parts[0], parts[1], parts[2]
            stats[path.replace('\\', '/')] = f'+{add}/-{dele}'
    return stats


def _batch_check_ignore(base_path: str, rels: list[str]) -> set[str]:
    """One ``git check-ignore --stdin`` for ALL paths → set of ignored paths.
    Batched (one subprocess) instead of one spawn per file — the difference
    between ~instant and hundreds of FUSE spawns on a large candidate set."""
    if not rels:
        return set()
    rc, out, _ = _git(base_path, 'check-ignore', '--stdin',
                      _stdin='\n'.join(rels))
    # rc is 0 (some ignored) or 1 (none ignored); both are normal.
    return {line.replace('\\', '/') for line in out.splitlines() if line}


def _build_conv_version_map(base_path: str, conv_id: str) -> dict:
    """SINGLE pass over ``snapshots.jsonl`` → {rel: latest_version} for exactly
    this conv_id. O(log) once — NOT O(files × log). The whole point of the
    2026-07-11 rewrite: the old per-file scan made plan_commit time out on the
    real 142 MB / 2000+-snapshot store."""
    from lib.file_history.store import iter_snapshots
    latest: dict = {}
    for s in iter_snapshots(base_path):  # oldest → newest
        if (s.get('convId') or '') != conv_id:
            continue
        for rel, v in (s.get('files') or {}).items():
            latest[rel] = v
    return latest


def _classify(base_path: str, conv_id: str, rel: str, *,
              version_map: dict, ignored_set: set[str]) -> tuple[str, str]:
    """Return ``(bucket, reason)`` for one candidate path. Pure lookups — the
    snapshot scan (``version_map``) and check-ignore (``ignored_set``) are done
    ONCE by the caller, never per file. bucket ∈ {'clean','ignored','contaminated'}."""
    from lib.file_history.store import read_blob
    # (b) generated-bundle / git-ignored → never in an agent commit set.
    if _GENERATED_BUNDLE_RE.search(rel):
        return 'ignored', 'generated bundle (release-only)'
    if rel in ignored_set:
        return 'ignored', 'git-ignored'

    latest_v = version_map.get(rel)
    abs_p = os.path.join(os.path.abspath(base_path), rel)
    exists = os.path.exists(abs_p)

    if latest_v is None:
        return 'contaminated', 'unattributed — this conversation has no recorded edit'
    if int(latest_v) == 0:  # deletion recorded by this conversation
        if not exists:
            return 'clean', 'deletion recorded by this conversation'
        return 'contaminated', 'recorded deleted but present on disk'
    blob = read_blob(base_path, rel, int(latest_v))
    if blob is None:
        return 'contaminated', 'unverifiable — no backup blob to prove authorship'
    if not exists:
        return 'contaminated', 'recorded with content but absent on disk'
    try:
        with open(abs_p, 'rb') as f:
            disk = f.read()
    except OSError as e:
        logger.warning('[Commit] cannot read %s for byte-identity check: %s', rel, e)
        return 'contaminated', f'unreadable: {e}'
    if disk == blob:
        return 'clean', 'byte-identical to this conversation\'s last recorded write'
    return 'contaminated', 'foreign hunks present (working tree diverges from ' \
                           'this conversation\'s recorded write)'


def plan_commit(project_path: str, conv_id: str, *,
                files: list[str] | None = None) -> dict:
    """Analyse what THIS conversation can safely commit. Pure — no mutation.

    ``files`` is REQUIRED — the agent must DECLARE the paths it edited this
    turn. There is intentionally NO file-history-derived default: a ``convId``
    records a conversation's whole LIFETIME (thousands of paths), so deriving
    "my work" from it is unsound (measured 543/564 false-clean + a full-log
    timeout on the real tree). The tool's value is the contamination GATE, not
    file discovery — the agent knows exactly what it wrote.

    Returns ``{ok, clean, contaminated, ignored, candidates, error?}`` where
    ``clean`` is the subset provably attributable to this conversation
    (byte-identical to its own last recorded write); ``contaminated`` /
    ``ignored`` are lists of ``{path, reason, numstat}``.
    """
    out: dict = {'ok': False, 'clean': [], 'contaminated': [], 'ignored': [],
                 'candidates': []}
    if not project_path:
        out['error'] = 'no project'
        return out
    if not files:
        out['error'] = ('no files declared — project_commit requires an explicit '
                        'files=[...] list of the paths YOU edited this turn. It '
                        'does NOT auto-discover your work (a conversation\'s '
                        'file-history spans its whole lifetime, not this turn).')
        return out
    from lib.conversations.project_feed import normalize_project_path
    base = normalize_project_path(project_path)
    if not _is_git_repo(base):
        out['error'] = 'not a git repository'
        return out

    # NB: strip only a literal leading './' prefix — NOT lstrip('./'), which is
    # a CHARACTER SET and would mangle every dotfile ('.gitignore' →
    # 'gitignore'), so the path would never match its file-history record.
    candidates = sorted(dict.fromkeys(
        re.sub(r'^(?:\./)+', '', str(f).replace('\\', '/'))
        for f in files if f))
    out['candidates'] = list(candidates)

    # Do the two expensive scans ONCE, not per-file.
    version_map = _build_conv_version_map(base, conv_id)
    ignored_set = _batch_check_ignore(base, candidates)
    numstats = _batch_numstat(base, candidates)

    for rel in candidates:
        bucket, reason = _classify(base, conv_id, rel,
                                   version_map=version_map, ignored_set=ignored_set)
        if bucket == 'clean':
            out['clean'].append(rel)
        else:
            out[bucket].append({'path': rel, 'reason': reason,
                                 'numstat': numstats.get(rel, '')})
    out['ok'] = True
    return out


def _agent_author() -> str:
    """git ``--author`` string for an agent commit. Overridable via
    ``TOFU_AGENT_GIT_AUTHOR`` ("Name <email>"); defaults to a Tofu-agent
    identity so a seam-made commit is attributable to the agent, NOT the human
    whose git config happens to be active."""
    return (os.environ.get('TOFU_AGENT_GIT_AUTHOR')
            or 'Tofu Agent <tofu-agent@localhost>').strip()


def do_commit(project_path: str, conv_id: str, message: str, *,
              files: list[str] | None = None,
              author: str | None = None) -> dict:
    """Commit ONLY this conversation's provably-clean set. The fixed sequence:

    1. ``plan_commit`` → clean set (excludes contaminated + ignored).
    2. Snapshot pre-existing staged paths; unstage everything (keeps worktree).
    3. ``git add -- <clean set>`` (explicit pathspec — never ``-A``).
    4. Verify the index holds EXACTLY the clean set (catches an accidental sweep).
    5. ``git commit -m <message>`` with NO pathspec → commits the index only.
    6. Verify the commit's file list == the clean set.
    7. Restore the pre-existing staged paths.

    Returns ``{ok, committed, excluded, commitSha?, error?, plan}``.
    """
    if not (message or '').strip():
        return {'ok': False, 'error': 'empty commit message'}
    plan = plan_commit(project_path, conv_id, files=files)
    if not plan.get('ok'):
        return {'ok': False, 'error': plan.get('error', 'plan failed'), 'plan': plan}
    clean = plan['clean']
    excluded = plan['contaminated'] + plan['ignored']
    if not clean:
        return {'ok': False, 'error': 'nothing clean to commit (all candidates '
                'excluded as contaminated or ignored)', 'plan': plan,
                'committed': [], 'excluded': excluded}

    from lib.conversations.project_feed import normalize_project_path
    base = normalize_project_path(project_path)

    # (2) Record pre-existing index so we can restore it, then unstage all.
    rc, staged_out, _ = _git(base, 'diff', '--cached', '--name-only')
    staged_before = [p for p in staged_out.splitlines() if p] if rc == 0 else []
    if staged_before:
        # Unstage only the pre-existing entries (keep working tree intact).
        _git(base, 'reset', '-q', 'HEAD', '--', *staged_before)

    # (3) Stage EXACTLY the clean set by explicit pathspec.
    rc, _, err = _git(base, 'add', '--', *clean)
    if rc != 0:
        _restage(base, staged_before)
        return {'ok': False, 'error': f'git add failed: {err.strip()}',
                'plan': plan, 'committed': [], 'excluded': excluded}

    # (4) Verify the index holds exactly the clean set — no more, no less.
    rc, idx_out, _ = _git(base, 'diff', '--cached', '--name-only')
    staged_now = sorted(p for p in idx_out.splitlines() if p)
    if staged_now != sorted(clean):
        extra = sorted(set(staged_now) - set(clean))
        # Roll back our staging, restore prior index, refuse.
        if staged_now:
            _git(base, 'reset', '-q', 'HEAD', '--', *staged_now)
        _restage(base, staged_before)
        return {'ok': False, 'error': f'index verification failed — staged set '
                f'{staged_now} != clean set {sorted(clean)} (extra: {extra})',
                'plan': plan, 'committed': [], 'excluded': excluded}

    # (5) Commit the INDEX with NO pathspec (the pathspec-override trap).
    #     --author attributes the commit to the AGENT, not the human whose git
    #     config is active on the host (the committer stays the host identity,
    #     which is standard — author is the authorship field consumers show).
    author_str = (author or _agent_author()).strip()
    rc, _, err = _git(base, 'commit', '-m', message, '--author', author_str)
    if rc != 0:
        _git(base, 'reset', '-q', 'HEAD', '--', *clean)
        _restage(base, staged_before)
        return {'ok': False, 'error': f'git commit failed: {err.strip()}',
                'plan': plan, 'committed': [], 'excluded': excluded}

    # (6) Verify the commit touched exactly the clean set.
    rc, sha_out, _ = _git(base, 'rev-parse', 'HEAD')
    sha = sha_out.strip()[:12] if rc == 0 else ''
    rc, files_out, _ = _git(base, 'show', '--pretty=format:', '--name-only', 'HEAD')
    committed_files = sorted(p for p in files_out.splitlines() if p)
    verified = committed_files == sorted(clean)
    if not verified:
        logger.error('[Commit] post-commit verify mismatch: committed=%s clean=%s',
                     committed_files, sorted(clean))

    # (7) Restore any pre-existing staged entries we set aside.
    _restage(base, staged_before)

    audit_log('project_commit', project_path=base, conv_id=conv_id,
              commit=sha, committed=len(clean), excluded=len(excluded),
              verified=verified, author=author_str)
    logger.info('[Commit] conv=%s committed %d file(s) sha=%s (excluded %d) verified=%s',
                (conv_id or '-')[:8], len(clean), sha, len(excluded), verified)
    return {'ok': True, 'commitSha': sha, 'committed': clean,
            'excluded': excluded, 'verified': verified, 'plan': plan}


def _restage(base_path: str, paths: list[str]) -> None:
    """Re-stage paths that were staged before we ran (best-effort)."""
    if not paths:
        return
    rc, _, err = _git(base_path, 'add', '--', *paths)
    if rc != 0:
        logger.warning('[Commit] could not restore prior staged set %s: %s',
                       paths, err.strip())


def _fmt_excluded(excluded: list[dict]) -> str:
    lines = []
    for e in excluded:
        ns = f' ({e["numstat"]})' if e.get('numstat') else ''
        lines.append(f'  • {e["path"]}{ns} — {e["reason"]}')
    return '\n'.join(lines)


def _stash_result(fn_args: dict, payload: dict) -> None:
    """Attach a STRUCTURED commit descriptor onto the tool args so the round's
    ``_post_build`` hook (lib/tasks_pkg/handlers/misc.py) can surface it as
    ``meta['commitResult']`` — the frontend renders a rich card off this, never
    by re-parsing the human-readable string this function returns. Best-effort:
    a non-dict ``fn_args`` (never happens for a real tool call) is ignored."""
    try:
        fn_args['_commitResult'] = payload
    except (TypeError, AttributeError) as e:
        logger.debug('[Commit] could not stash structured result: %s', e)


def execute_commit_tool(fn_args: dict, *, current_conv_id: str = '',
                        project_path: str = '') -> str:
    """Agent-tool entry point → human-readable result string.

    In addition to the returned string (what the LLM reads), this stashes a
    structured ``_commitResult`` descriptor on ``fn_args`` so the frontend can
    render an explicit commit card (mode, committed/would-commit paths, held-
    back files with reasons, sha, verify state) instead of a vague one-liner.
    """
    try:
        if not project_path:
            _stash_result(fn_args, {'mode': 'plan', 'ok': False,
                                    'error': 'not in project mode',
                                    'clean': [], 'committed': [], 'excluded': []})
            return ('Error: project_commit is only available in project mode '
                    '(open a project first).')
        files = fn_args.get('files') or None
        if files is not None and not isinstance(files, list):
            files = [str(files)]
        message = (fn_args.get('message') or '').strip()
        dry_run = bool(fn_args.get('dry_run'))

        if dry_run or not message:
            plan = plan_commit(project_path, current_conv_id, files=files)
            if not plan.get('ok'):
                _stash_result(fn_args, {'mode': 'plan', 'ok': False,
                                        'error': plan.get('error', 'unknown'),
                                        'clean': [], 'committed': [], 'excluded': []})
                return f'Cannot plan commit: {plan.get("error", "unknown")}.'
            excl = plan['contaminated'] + plan['ignored']
            _stash_result(fn_args, {
                'mode': 'plan', 'ok': True,
                'clean': list(plan['clean']), 'committed': [], 'excluded': excl,
                'candidatesN': len(plan.get('candidates') or []),
                'needsMessage': not message,
            })
            parts = [f'Commit plan for this conversation '
                     f'({len(plan["clean"])} clean, '
                     f'{len(plan["contaminated"])} contaminated, '
                     f'{len(plan["ignored"])} ignored):']
            if plan['clean']:
                parts.append('\nWould commit (provably yours, byte-identical):')
                parts += [f'  • {p}' for p in plan['clean']]
            if excl:
                parts.append('\nExcluded (NOT committed):')
                parts.append(_fmt_excluded(excl))
            if not message:
                parts.append('\nProvide a `message` to actually commit the clean set.')
            return '\n'.join(parts)

        res = do_commit(project_path, current_conv_id, message, files=files,
                        author=fn_args.get('author') or None)
        if not res.get('ok'):
            excl = res.get('excluded') or []
            _stash_result(fn_args, {'mode': 'commit', 'ok': False,
                                    'error': res.get('error', 'unknown'),
                                    'clean': [], 'committed': [], 'excluded': excl})
            body = f'Commit not made: {res.get("error", "unknown")}.'
            if excl:
                body += '\nExcluded:\n' + _fmt_excluded(excl)
            return body
        excl = res.get('excluded') or []
        _stash_result(fn_args, {
            'mode': 'commit', 'ok': True,
            'committed': list(res['committed']), 'clean': list(res['committed']),
            'excluded': excl, 'commitSha': res.get('commitSha', ''),
            'verified': bool(res.get('verified')),
        })
        parts = [f'Committed {len(res["committed"])} file(s) as {res["commitSha"]}'
                 + ('' if res.get('verified') else ' (⚠ post-commit verify mismatch — check `git show HEAD`)')
                 + ':']
        parts += [f'  • {p}' for p in res['committed']]
        if excl:
            parts.append('\nHeld back (commit yours after the sibling lands, '
                         'or coordinate):')
            parts.append(_fmt_excluded(excl))
        return '\n'.join(parts)
    except Exception as e:
        logger.warning('[Commit] execute_commit_tool failed: %s', e, exc_info=True)
        return f'Error executing project_commit: {e}'


__all__ = ['plan_commit', 'do_commit', 'execute_commit_tool']
