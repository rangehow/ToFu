"""lib.conversations.project_worktree — per-conversation git worktree isolation.

Pillar of the Project Brain scale-out (design: ``docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md``).
This module makes authorship **structural** instead of inferred: each active
conversation gets its OWN git worktree + branch, so "is this file mine to
commit?" is answered by the git DAG (the branch), never by ``project_commit``'s
byte-identity guess (which cannot distinguish my hunk from a sibling's that
predated my first touch — the false-clean trap). Landing becomes a real merge
into a dedicated integration branch under compare-and-swap, and a genuine
collision surfaces as an ordinary, finite merge conflict rather than a
permanent land-time jail.

Scope of THIS module (build-order step 1, ``docs`` §8):
  * lifecycle primitives — ``ensure_worktree`` / ``sync_worktree`` /
    ``land_worktree`` / ``release_worktree`` / ``gc_worktrees``;
  * the CAS-with-retry integration-ref land primitive (§5.1), CONTENT-verified
    via a REAL 3-way merge in a throwaway detached land-worktree, RE-GATED on
    the merge-result tree inside the CAS critical section;
  * a soft-lease GC registry mirroring the board's at-read-time expiry model
    (no reaper thread; a crashed conv's worktree is reclaimable after one TTL,
    and GC NEVER deletes a branch with unmerged commits).

Rollout seam (§6): everything gates on ``TOFU_WORKTREE_ISOLATION`` (default
``inproc`` = OFF). With isolation OFF this module makes ZERO changes to the
project — a single-box / desktop install is byte-identical to today, and the
legacy ``project_commit`` path stays in force. Only ``on`` activates worktrees.

Environment constraints this module honors (measured on the real FUSE mount,
``docs`` §7.1; git 2.11.0):
  1. NO ``merge-tree --write-tree`` and NO ``worktree remove`` on git 2.11 —
     the land computes a real merged tree via ``git merge`` in a throwaway
     detached land-worktree, then CAS ``update-ref <ref> <new> <old>``; GC uses
     ``rm -rf`` + ``git worktree prune``.
  2. The shared ``.git`` gets ``core.logallrefupdates=false`` (only when
     isolation is enabled) — per-ref reflogs are the ONLY artifact that garbles
     under concurrent FUSE appends.
  3. Integration-ref land is CAS-with-retry, NEVER blind (a blind ``update-ref``
     loses 7/8 concurrent merges silently).
  4. Every git call carries ``--no-optional-locks -c core.fsmonitor=false``
     (fsmonitor is unreliable on network/FUSE mounts — cross-checked against
     OpenCode's worktree hardening) and rm retries slow FUSE unlinks.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

from lib.agent_artifacts import WORKTREES_DIR
from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)

# ── Tunables (design §2, §5.1) ──────────────────────────────────────────────
DEFAULT_LEASE_TTL_MS = 30 * 60 * 1000     # 30 min soft lease, mirrors the board
MAX_LAND_RETRIES = 50                     # §5.1 / Q5 — CAS rounds before exhaust
_LAND_BACKOFF_BASE_S = 0.05               # jittered backoff on a lost CAS
_LAND_BACKOFF_MAX_S = 0.75
_GIT_TIMEOUT = 120
_TEST_TIMEOUT = 900
_RM_RETRIES = 5                           # FUSE unlink can be slow/locked
_RM_RETRY_DELAY_S = 0.1

# FUSE-hardening flags applied to EVERY git invocation (constraint 4).
# ``core.fsmonitor=false`` (a -c config override) is universally accepted;
# ``--no-optional-locks`` was only ADDED in git 2.15, so on the 2.11 baseline it
# aborts with "Unknown option" (rc=129) and would break every call. Detect it
# once and include it only when the installed git supports it — the hardening is
# best-effort and the fsmonitor override alone covers the FUSE lock hazard.
def _detect_no_optional_locks() -> bool:
    try:
        p = subprocess.run(['git', '--no-optional-locks', 'rev-parse', '--git-dir'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        # rc 128 (not a repo) still means the FLAG parsed; 129 = unknown option.
        return p.returncode != 129
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug('[Worktree] no-optional-locks probe failed, assuming unsupported: %s', e)
        return False


_HAS_NO_OPTIONAL_LOCKS = _detect_no_optional_locks()
_GIT_HARDEN = (('--no-optional-locks',) if _HAS_NO_OPTIONAL_LOCKS else ()) \
    + ('-c', 'core.fsmonitor=false')

_DEFAULT_INTEGRATION_BRANCH = 'tofu/integration'
_LEASE_REGISTRY_FILE = 'registry.json'    # under <project>/.tofu_worktrees/


# ═══════════════════════════════════════════════════════════════════════════
#  Rollout seam
# ═══════════════════════════════════════════════════════════════════════════

def isolation_mode() -> str:
    """Return the active worktree-isolation mode, read at CALL time.

    ``TOFU_WORKTREE_ISOLATION``:
      * ``inproc`` (default) — OFF. Byte-identical to today; the primary
        checkout is the working tree and ``project_commit`` is the land path.
      * ``on`` — worktree isolation active.

    Any unrecognised value logs a WARN and falls back to ``inproc`` (fail-open
    to the release-safe default). Mirrors ``rate_limit_store.get_store()``.
    """
    desired = (getenv_compat('TOFU_WORKTREE_ISOLATION') or 'inproc').strip().lower()
    if desired not in ('inproc', 'on'):
        logger.warning('[Worktree] Unknown TOFU_WORKTREE_ISOLATION=%r — '
                       'defaulting to inproc (OFF)', desired)
        return 'inproc'
    return desired


def is_isolation_enabled() -> bool:
    """True iff worktree isolation is turned on (``TOFU_WORKTREE_ISOLATION=on``)."""
    return isolation_mode() == 'on'


def integration_branch() -> str:
    """The dedicated integration branch name (env ``TOFU_WORKTREE_INTEGRATION_BRANCH``).

    RATIFIED (Q1): a DEDICATED branch autonomous merges land into; the human
    fast-forwards it into their own build branch at their cadence. Autonomous
    landing NEVER writes the human's trunk.
    """
    return (getenv_compat('TOFU_WORKTREE_INTEGRATION_BRANCH')
            or _DEFAULT_INTEGRATION_BRANCH).strip() or _DEFAULT_INTEGRATION_BRANCH


# ═══════════════════════════════════════════════════════════════════════════
#  Path / name helpers
# ═══════════════════════════════════════════════════════════════════════════

def _sanitize_conv_id(conv_id: str) -> str:
    """A conv_id safe to embed in a git ref name / directory name. git ref
    names forbid a range of characters; conv_ids are alphanumeric in practice,
    but sanitize defensively (never let a crafted id escape the namespace)."""
    return re.sub(r'[^A-Za-z0-9._-]', '-', str(conv_id or 'anon'))[:64] or 'anon'


def conv_branch(conv_id: str) -> str:
    """The per-conversation branch name: ``tofu/conv/<sanitized_conv_id>``."""
    return f'tofu/conv/{_sanitize_conv_id(conv_id)}'


def _norm_base(project_path: str) -> str:
    """Canonical project-path storage key (same seam every project-brain read /
    write funnels through). Best-effort; falls back to the raw path."""
    try:
        from lib.conversations.project_feed import normalize_project_path
        return normalize_project_path(project_path)
    except Exception as e:
        logger.debug('[Worktree] normalize_project_path failed: %s', e)
        return project_path


def worktrees_root(project_path: str) -> str:
    """Absolute path of the per-project worktree state dir (``.tofu_worktrees/``).

    A ``.tofu*``-prefixed name so every existing artifact consumer (gitignore,
    export sanitizer, self-update skip-lists) already excludes it (§3.6)."""
    return os.path.join(os.path.abspath(_norm_base(project_path)), WORKTREES_DIR)


def worktree_path(project_path: str, conv_id: str) -> str:
    """Absolute path of one conversation's worktree checkout."""
    return os.path.join(worktrees_root(project_path), _sanitize_conv_id(conv_id))


# ═══════════════════════════════════════════════════════════════════════════
#  Git plumbing (FUSE-hardened; never raises)
# ═══════════════════════════════════════════════════════════════════════════

def _git(cwd: str, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run a hardened git command; return ``(rc, stdout, stderr)``.

    Never raises for a non-zero exit — only a genuine spawn failure / timeout
    yields ``rc=-1``. The hardening flags (``--no-optional-locks``,
    ``core.fsmonitor=false``) go in front of the subcommand (constraint 4)."""
    try:
        p = subprocess.run(
            ['git', *_GIT_HARDEN, *args], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return (p.returncode,
                p.stdout.decode('utf-8', 'replace'),
                p.stderr.decode('utf-8', 'replace'))
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('[Worktree] git %s failed to run: %s', ' '.join(args), e)
        return -1, '', str(e)


def _is_git_repo(base_path: str) -> bool:
    rc, out, _ = _git(base_path, 'rev-parse', '--is-inside-work-tree')
    return rc == 0 and out.strip() == 'true'


def _rev_parse(base_path: str, ref: str) -> str:
    rc, out, _ = _git(base_path, 'rev-parse', '--verify', '--quiet', ref)
    return out.strip() if rc == 0 else ''


def _ref_exists(base_path: str, ref: str) -> bool:
    rc, _, _ = _git(base_path, 'show-ref', '--verify', '--quiet', ref)
    return rc == 0


def _is_ancestor(base_path: str, maybe_ancestor: str, descendant: str) -> bool:
    rc, _, _ = _git(base_path, 'merge-base', '--is-ancestor',
                    maybe_ancestor, descendant)
    return rc == 0


def _rmtree_retry(path: str) -> None:
    """rmtree with retries — FUSE unlinks can transiently fail/lock (OpenCode
    uses the same maxRetries pattern on network mounts)."""
    for attempt in range(_RM_RETRIES):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError as e:
            logger.debug('[Worktree] rmtree %s already absent, nothing to remove: %s', path, e)
            return
        except OSError as e:
            if attempt == _RM_RETRIES - 1:
                logger.warning('[Worktree] rmtree %s failed after %d tries: %s',
                               path, _RM_RETRIES, e)
                shutil.rmtree(path, ignore_errors=True)
                return
            time.sleep(_RM_RETRY_DELAY_S)


def _prune_worktree_dir(base_path: str, wt: str) -> None:
    """Tear down a linked worktree dir. git 2.11 lacks ``worktree remove`` — try
    it first (newer git), then fall back to ``rm -rf`` + ``worktree prune``
    (constraint 1). Never raises."""
    rc, _, _ = _git(base_path, 'worktree', 'remove', '--force', wt)
    if rc != 0:
        _rmtree_retry(wt)
    _git(base_path, 'worktree', 'prune')


# ═══════════════════════════════════════════════════════════════════════════
#  Soft-lease registry (mirrors the board's at-read-time expiry; no reaper)
# ═══════════════════════════════════════════════════════════════════════════

def _registry_path(project_path: str) -> str:
    return os.path.join(worktrees_root(project_path), _LEASE_REGISTRY_FILE)


def _read_registry(project_path: str) -> dict:
    """Read the worktree-lease registry. Best-effort — a missing / corrupt file
    reads as empty."""
    try:
        from lib.json_store import read_json
        data = read_json(_registry_path(project_path), default={})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug('[Worktree] registry read failed: %s', e)
        return {}


def _update_registry(project_path: str, mutate) -> dict:
    """Atomically read-modify-write the registry under the json_store per-path
    lock. ``mutate(dict)`` edits in place; the mutated dict is persisted.

    NOTE: ``update_json_atomic`` skips the write when its mutator returns
    ``None``, so the wrapper below RETURNS the (in-place-mutated) dict — an
    empty registry after a ``pop`` is a legitimate value that MUST persist."""
    def _wrapped(cur):
        cur = cur if isinstance(cur, dict) else {}
        mutate(cur)
        return cur

    try:
        from lib.json_store import update_json_atomic
        result = update_json_atomic(_registry_path(project_path), _wrapped, default={})
        return result if isinstance(result, dict) else {}
    except Exception as e:
        logger.warning('[Worktree] registry update failed: %s', e)
        # Fall back to a best-effort non-atomic path so a lease is still tracked
        data = _read_registry(project_path)
        try:
            mutate(data)
            from lib.json_store import write_json_atomic
            write_json_atomic(_registry_path(project_path), data)
        except Exception as e2:
            logger.warning('[Worktree] registry fallback write failed: %s', e2)
        return data


# ═══════════════════════════════════════════════════════════════════════════
#  Integration branch bootstrap
# ═══════════════════════════════════════════════════════════════════════════

def ensure_integration_setup(project_path: str) -> dict:
    """Idempotently prepare the shared ``.git`` for worktree isolation.

    * Sets ``core.logallrefupdates=false`` (constraint 2) — ONLY here, so a
      single-box install with isolation OFF never has its reflogs disabled.
    * Creates the dedicated integration branch off the current HEAD if absent
      (RATIFIED Q1); never moves it if it already exists.
    * Ensures the worktree state dir exists.

    Returns ``{ok, integration, created_branch, error?}``. Never raises.
    """
    base = _norm_base(project_path)
    out: dict = {'ok': False, 'integration': integration_branch(),
                 'created_branch': False}
    if not base or not _is_git_repo(base):
        out['error'] = 'not a git repository'
        return out
    try:
        os.makedirs(worktrees_root(project_path), exist_ok=True)
    except OSError as e:
        out['error'] = f'cannot create worktree root: {e}'
        logger.warning('[Worktree] %s', out['error'])
        return out

    # Reflogs off on the shared repo (harmless-noise elimination under
    # concurrent FUSE appends). Idempotent config set.
    rc, _, err = _git(base, 'config', 'core.logallrefupdates', 'false')
    if rc != 0:
        logger.warning('[Worktree] could not set core.logallrefupdates: %s', err.strip())

    ib = integration_branch()
    if not _ref_exists(base, f'refs/heads/{ib}'):
        head = _rev_parse(base, 'HEAD')
        if not head:
            out['error'] = 'HEAD has no commit — cannot seed integration branch'
            logger.warning('[Worktree] %s', out['error'])
            return out
        rc, _, err = _git(base, 'branch', ib, head)
        if rc != 0:
            out['error'] = f'branch create failed: {err.strip()[:200]}'
            logger.warning('[Worktree] %s', out['error'])
            return out
        out['created_branch'] = True
        audit_log('worktree_integration_seed', project_path=base, branch=ib, head=head[:12])
        logger.info('[Worktree] seeded integration branch %s at %s', ib, head[:12])
    out['ok'] = True
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

def ensure_worktree(project_path: str, conv_id: str, *,
                    ttl_ms: int = DEFAULT_LEASE_TTL_MS) -> dict:
    """Create (or reuse) this conversation's worktree + branch; return its path.

    Idempotent: if the worktree already exists its lease is REFRESHED (the
    every-turn call keeps a live holder's reservation alive at zero cost) and
    the existing path returned. Otherwise a new branch ``tofu/conv/<id>`` is
    created off the integration HEAD and checked out into a fresh worktree.

    No-op (``{ok:False, disabled:True}``) when isolation is OFF — the caller
    keeps using the primary checkout.

    Returns ``{ok, path?, branch?, created?, disabled?, error?}``. Never raises.
    """
    if not is_isolation_enabled():
        return {'ok': False, 'disabled': True}
    base = _norm_base(project_path)
    if not base or not _is_git_repo(base):
        return {'ok': False, 'error': 'not a git repository'}

    setup = ensure_integration_setup(project_path)
    if not setup.get('ok'):
        return {'ok': False, 'error': setup.get('error', 'integration setup failed')}

    branch = conv_branch(conv_id)
    wt = worktree_path(project_path, conv_id)
    ib = integration_branch()
    now = now_ms()
    lease = now + max(60_000, int(ttl_ms or DEFAULT_LEASE_TTL_MS))

    # Already present (its .git link resolves) → refresh lease, reuse.
    if os.path.isdir(wt) and _is_git_repo(wt):
        _touch_lease(project_path, conv_id, wt, branch, lease)
        return {'ok': True, 'path': wt, 'branch': branch, 'created': False}

    # A stale dir with no valid worktree link — clean it before recreating.
    if os.path.exists(wt):
        logger.info('[Worktree] pruning stale worktree dir %s', wt)
        _prune_worktree_dir(base, wt)

    try:
        os.makedirs(os.path.dirname(wt), exist_ok=True)
    except OSError as e:
        logger.debug('[Worktree] worktree parent mkdir failed, using fallback: %s', e)
        return {'ok': False, 'error': f'cannot create worktree parent: {e}'}

    # Create the branch off integration HEAD if it doesn't yet exist, then add
    # the worktree. If the branch already exists (a prior run) attach to it.
    integ_head = _rev_parse(base, f'refs/heads/{ib}')
    if _ref_exists(base, f'refs/heads/{branch}'):
        rc, _, err = _git(base, 'worktree', 'add', wt, branch)
    else:
        rc, _, err = _git(base, 'worktree', 'add', '-b', branch, wt,
                          integ_head or ib)
    if rc != 0:
        logger.warning('[Worktree] worktree add failed for %s: %s', conv_id, err.strip())
        return {'ok': False, 'error': f'worktree add failed: {err.strip()[:200]}'}

    _touch_lease(project_path, conv_id, wt, branch, lease)
    audit_log('worktree_create', project_path=base, conv_id=conv_id,
              branch=branch, path=wt)
    logger.info('[Worktree] created worktree conv=%s branch=%s', conv_id[:12], branch)
    return {'ok': True, 'path': wt, 'branch': branch, 'created': True}


def _touch_lease(project_path: str, conv_id: str, wt: str, branch: str,
                 lease_expires_at: int) -> None:
    key = _sanitize_conv_id(conv_id)

    def _mut(reg: dict) -> None:
        entry = reg.get(key) or {}
        entry.update({'conv_id': conv_id, 'path': wt, 'branch': branch,
                      'lease_expires_at': int(lease_expires_at)})
        entry.setdefault('created_at', now_ms())
        reg[key] = entry

    _update_registry(project_path, _mut)


def refresh_lease(project_path: str, conv_id: str, *,
                  ttl_ms: int = DEFAULT_LEASE_TTL_MS) -> bool:
    """Extend a live worktree's soft lease (call each turn a long task keeps
    working). Returns True if a registry entry was refreshed."""
    if not is_isolation_enabled():
        return False
    wt = worktree_path(project_path, conv_id)
    if not (os.path.isdir(wt) and _is_git_repo(wt)):
        return False
    _touch_lease(project_path, conv_id, wt, conv_branch(conv_id),
                 now_ms() + max(60_000, int(ttl_ms or DEFAULT_LEASE_TTL_MS)))
    return True


def sync_worktree(project_path: str, conv_id: str) -> dict:
    """Rebase the conversation branch onto the latest integration HEAD.

    Fast-forward when possible; on a real conflict, ABORT the rebase and report
    it (the conflict is resolved inside the conv's own worktree — never a silent
    park, never a clobber of integration).

    Returns ``{ok, conflict?, fast_forward?, error?}``. Never raises.
    """
    if not is_isolation_enabled():
        return {'ok': False, 'disabled': True}
    base = _norm_base(project_path)
    wt = worktree_path(project_path, conv_id)
    if not (os.path.isdir(wt) and _is_git_repo(wt)):
        return {'ok': False, 'error': 'worktree missing — call ensure_worktree first'}
    ib = integration_branch()
    integ = _rev_parse(base, f'refs/heads/{ib}')
    if not integ:
        return {'ok': False, 'error': f'integration branch {ib} missing'}

    before = _rev_parse(wt, 'HEAD')
    # If the conv branch already CONTAINS integration (e.g. it was reconciled
    # via resolve_worktree_conflict — a merge of integration into the branch),
    # there is nothing to rebase: replaying the branch onto integration would
    # re-hit the very conflict the merge already resolved. Short-circuit so the
    # recovery loop converges (the merge-resolve → re-land fast-forward path).
    if before and _is_ancestor(base, integ, before):
        return {'ok': True, 'fast_forward': True, 'already_current': True}
    rc, _, err = _git(wt, 'rebase', ib)
    if rc != 0:
        # Conflict (or other rebase failure) — abort so the worktree is left
        # clean, and surface the conflict for in-worktree resolution.
        _git(wt, 'rebase', '--abort')
        logger.info('[Worktree] sync conflict conv=%s: %s', conv_id[:12], err.strip()[:160])
        return {'ok': False, 'conflict': True, 'error': err.strip()[:200]}
    after = _rev_parse(wt, 'HEAD')
    return {'ok': True, 'fast_forward': (before == after)}


def release_worktree(project_path: str, conv_id: str, *,
                     force: bool = False) -> dict:
    """GC one conversation's worktree at task/conversation end.

    NEVER loses work: a branch with commits not yet reachable from integration
    is KEPT (the worktree is pruned but the branch stays as
    "orphaned — awaiting human or re-dispatch") unless ``force=True``. A branch
    fully merged into integration is deleted along with its worktree.

    Returns ``{ok, pruned?, branch_deleted?, kept_unmerged?, error?}``.
    """
    if not is_isolation_enabled():
        return {'ok': False, 'disabled': True}
    base = _norm_base(project_path)
    wt = worktree_path(project_path, conv_id)
    branch = conv_branch(conv_id)
    ib = integration_branch()
    out: dict = {'ok': False, 'pruned': False, 'branch_deleted': False,
                 'kept_unmerged': False}

    branch_tip = _rev_parse(base, f'refs/heads/{branch}')
    merged = bool(branch_tip) and _is_ancestor(base, branch_tip, ib)

    if os.path.exists(wt):
        _prune_worktree_dir(base, wt)
        out['pruned'] = True

    if branch_tip and (merged or force):
        rc, _, err = _git(base, 'branch', '-D', branch)
        if rc == 0:
            out['branch_deleted'] = True
        else:
            logger.debug('[Worktree] branch delete %s: %s', branch, err.strip())
    elif branch_tip and not merged:
        out['kept_unmerged'] = True
        logger.info('[Worktree] kept unmerged branch %s (has work not in %s)',
                    branch, ib)

    # Drop the lease registry entry regardless (the worktree is gone).
    key = _sanitize_conv_id(conv_id)

    def _mut(reg: dict) -> None:
        reg.pop(key, None)

    _update_registry(project_path, _mut)
    out['ok'] = True
    audit_log('worktree_release', project_path=base, conv_id=conv_id,
              branch=branch, branch_deleted=out['branch_deleted'],
              kept_unmerged=out['kept_unmerged'])
    return out


def gc_worktrees(project_path: str, *, now: int | None = None) -> dict:
    """Reclaim worktrees whose soft lease has expired (piggyback on the sweep
    tick — no reaper thread). A lease older than its TTL means the owning
    conversation is gone / crashed. An expired-lease worktree is released, but —
    exactly like ``release_worktree`` — a branch with UNMERGED commits is kept
    (never lose work), only the (reclaimable) checkout is pruned.

    Returns ``{ok, reclaimed[], kept_unmerged[]}``. Never raises.
    """
    out: dict = {'ok': False, 'reclaimed': [], 'kept_unmerged': []}
    if not is_isolation_enabled():
        out['disabled'] = True
        return out
    now = int(now if now is not None else now_ms())
    reg = _read_registry(project_path)
    for key, entry in list(reg.items()):
        try:
            lease = int((entry or {}).get('lease_expires_at') or 0)
        except (TypeError, ValueError) as e:
            logger.debug('[Worktree] lease_expires_at parse failed, defaulting: %s', e)
            lease = 0
        if lease and lease <= now:
            conv_id = (entry or {}).get('conv_id') or key
            res = release_worktree(project_path, conv_id)
            if res.get('kept_unmerged'):
                out['kept_unmerged'].append(conv_id)
            elif res.get('ok'):
                out['reclaimed'].append(conv_id)
    out['ok'] = True
    if out['reclaimed'] or out['kept_unmerged']:
        logger.info('[Worktree] GC reclaimed=%d kept_unmerged=%d proj=%.40r',
                    len(out['reclaimed']), len(out['kept_unmerged']), project_path)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Merge-result acceptance gate (run the declared tests on the merged tree)
# ═══════════════════════════════════════════════════════════════════════════

def _gate_tests(worktree: str, test_paths: list[str]) -> tuple[bool, str]:
    """Run the declared pytest paths inside ``worktree`` with a throwaway DB and
    the same safe env ``run_acceptance_gate`` uses. Returns ``(green, summary)``.

    No declared tests → ``(True, '')`` (the caller decides whether that is
    acceptable). This is the load-bearing merge-result check (§5 step 4): a
    textually-clean but semantically-broken merge (Scenario C) fails here
    because the merged tree's tests import the broken reference and error out."""
    if not test_paths:
        return True, ''
    env = dict(os.environ)
    env['TOFU_MLOCK'] = '0'
    env.pop('TOFU_REQUIRE_PG', None)
    env['TOFU_DB_PATH'] = os.path.join(worktree, '.tofu_gate.db')
    try:
        p = subprocess.run(
            [sys.executable, '-m', 'pytest', '-p', 'no:napari', '-q', *test_paths],
            cwd=worktree, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_TEST_TIMEOUT, check=False,
        )
        out = p.stdout.decode('utf-8', 'replace') + p.stderr.decode('utf-8', 'replace')
        lines = [ln for ln in out.splitlines() if ln.strip()]
        summary = '\n'.join(lines[-20:])
        green = (p.returncode == 0)
        if green and 'no tests ran' in summary.lower():
            green = False
        return green, summary
    except subprocess.TimeoutExpired:
        logger.warning('[Worktree] merge-result gate timed out')
        return False, 'gate timed out'
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('[Worktree] merge-result gate failed to run: %s', e)
        return False, str(e)


# ═══════════════════════════════════════════════════════════════════════════
#  Land — CAS-with-retry real 3-way merge into the integration ref (§5.1)
# ═══════════════════════════════════════════════════════════════════════════

def _agent_author() -> str:
    """``--author`` identity for agent-authored merges (the existing convention).
    The human remains committer; the merge is attributable to the agent."""
    return (getenv_compat('TOFU_AGENT_GIT_AUTHOR')
            or 'Tofu Agent <tofu-agent@localhost>')


def land_worktree(project_path: str, conv_id: str, *,
                  test_paths: list[str] | None = None,
                  message: str = '', author: str | None = None) -> dict:
    """Land the conversation's branch into the integration branch (replaces
    ``project_commit.do_commit`` in ``on`` mode).

    Flow (design §5 / §5.1):
      1. ``sync_worktree`` — rebase conv branch onto integration HEAD; a
         conflict returns ``conflict`` (resolve in the conv worktree, retry).
      2. PRE-FLIGHT gate the conv branch (fail fast; necessary, NOT sufficient).
      3. CAS-serialized REAL 3-way merge into integration, RE-GATING the
         merge-result tree inside the CAS critical section before publishing the
         ref. Success = the merge-result GATES GREEN (not merely content-present).
         A red merge-result / real conflict routes to in-worktree resolution and
         is NEVER published.

    ``test_paths`` is the declared pytest set gated pre-merge AND on the
    merge-result. ``message`` overrides the merge commit message.

    Returns ``{ok, sha?, conflict?, preflight_red?, merge_result_red?,
    exhausted?, retries, testSummary?, error?}``. Never raises.
    """
    out: dict = {'ok': False, 'retries': 0}
    if not is_isolation_enabled():
        out['disabled'] = True
        out['error'] = 'worktree isolation disabled — use project_commit (inproc)'
        return out
    base = _norm_base(project_path)
    if not base or not _is_git_repo(base):
        out['error'] = 'not a git repository'
        return out
    wt = worktree_path(project_path, conv_id)
    if not (os.path.isdir(wt) and _is_git_repo(wt)):
        out['error'] = 'worktree missing — call ensure_worktree first'
        return out
    branch = conv_branch(conv_id)
    ib = integration_branch()
    test_paths = list(test_paths or [])

    # 1. Sync (rebase conv branch onto integration HEAD).
    sync = sync_worktree(project_path, conv_id)
    if not sync.get('ok'):
        out['conflict'] = bool(sync.get('conflict'))
        out['error'] = sync.get('error', 'sync failed')
        return out

    # 2. Pre-flight gate the conv branch in isolation (fail fast).
    green, summary = _gate_tests(wt, test_paths)
    out['testSummary'] = summary
    if not green:
        out['preflight_red'] = True
        out['error'] = 'pre-flight tests red on conv branch'
        logger.info('[Worktree] land pre-flight RED conv=%s', conv_id[:12])
        return out

    branch_tip = _rev_parse(base, f'refs/heads/{branch}')
    if not branch_tip:
        out['error'] = f'conv branch {branch} has no commits'
        return out

    start = time.time()
    started_ms = now_ms()
    # 3. CAS-merge loop (constraint 3: CAS-with-retry, never blind).
    for attempt in range(1, MAX_LAND_RETRIES + 1):
        out['retries'] = attempt
        old = _rev_parse(base, f'refs/heads/{ib}')
        if not old:
            out['error'] = f'integration branch {ib} missing'
            return out

        if _is_ancestor(base, old, branch_tip):
            # Trivial fast-forward: conv tip already contains OLD's content; no
            # divergent lander, so Scenario C is impossible — no re-merge needed.
            new = branch_tip
        else:
            # True divergence → REAL 3-way merge in a throwaway detached
            # land-worktree at OLD, then GATE the merge-result (§5 step 4).
            merged = _merge_and_gate(base, old, branch, test_paths, message, author)
            if merged.get('conflict'):
                out['conflict'] = True
                out['error'] = merged.get('error', 'merge conflict')
                logger.info('[Worktree] land merge CONFLICT conv=%s', conv_id[:12])
                return out
            if merged.get('merge_result_red'):
                out['merge_result_red'] = True
                out['testSummary'] = merged.get('summary', summary)
                out['error'] = 'merge-result tests red (integration would break)'
                logger.info('[Worktree] land merge-result RED conv=%s', conv_id[:12])
                return out
            new = merged.get('sha', '')
            if not new:
                out['error'] = merged.get('error', 'merge produced no commit')
                return out

        # CAS: publish ONLY if integration is still at OLD.
        rc, _, err = _git(base, 'update-ref', f'refs/heads/{ib}', new, old)
        if rc == 0:
            out['ok'] = True
            out['sha'] = new
            dur = time.time() - start
            audit_log('worktree_land', project_path=base, conv_id=conv_id,
                      branch=branch, integration=ib, sha=new[:12],
                      retries=attempt, duration_s=round(dur, 3),
                      started_ms=started_ms)
            logger.info('[Worktree] LANDED conv=%s → %s@%s retries=%d %.2fs',
                        conv_id[:12], ib, new[:12], attempt, dur)
            return out
        # Lost the CAS race — integration moved. Re-read OLD, RE-MERGE and
        # RE-GATE against the new tip (never re-CAS a stale/ungated tree).
        logger.debug('[Worktree] CAS lost attempt=%d conv=%s: %s',
                     attempt, conv_id[:12], err.strip()[:120])
        time.sleep(min(_LAND_BACKOFF_MAX_S,
                       _LAND_BACKOFF_BASE_S * attempt + random.uniform(0, _LAND_BACKOFF_BASE_S)))

    out['exhausted'] = True
    out['error'] = f'land exhausted after {MAX_LAND_RETRIES} CAS rounds'
    logger.warning('[Worktree] land EXHAUSTED conv=%s after %d rounds',
                   conv_id[:12], MAX_LAND_RETRIES)
    return out


def _merge_and_gate(base_path: str, old: str, branch: str,
                    test_paths: list[str], message: str,
                    author: str | None) -> dict:
    """Do a REAL 3-way merge of ``branch`` into a throwaway detached
    land-worktree checked out at ``old``, then GATE the merge-result tree.

    Returns ``{sha?, conflict?, merge_result_red?, summary?, error?}``. The
    scratch worktree is ALWAYS torn down (detached + pruned) so it never
    collides with a live conv edit. Never raises.
    """
    lw = tempfile.mkdtemp(prefix='tofu_land_')
    try:
        rc, _, err = _git(base_path, 'worktree', 'add', '--detach', lw, old)
        if rc != 0:
            return {'error': f'land-worktree add failed: {err.strip()[:200]}'}

        # Deterministic committer/author for the merge commit.
        msg = message or f'Land {branch} into integration'
        merge_args = ['merge', '--no-edit', '-m', msg, branch]
        rc, _, err = _git(lw, *merge_args)
        if rc != 0:
            # A genuine same-file conflict falls out here — abort + report.
            _git(lw, 'merge', '--abort')
            return {'conflict': True, 'error': err.strip()[:200]}

        # Stamp the agent author on the merge commit (committer stays host git).
        _git(lw, 'commit', '--amend', '--no-edit',
             f'--author={author or _agent_author()}')

        # *** GATE THE MERGE-RESULT *** (content-preserved != integration-correct)
        green, summary = _gate_tests(lw, test_paths)
        if not green:
            return {'merge_result_red': True, 'summary': summary}

        new = _rev_parse(lw, 'HEAD')
        if not new:
            return {'error': 'merge-result HEAD unresolved'}
        return {'sha': new, 'summary': summary}
    finally:
        _prune_worktree_dir(base_path, lw)


# ═══════════════════════════════════════════════════════════════════════════
#  Tool-scoping seam (build-order step 3, §3.1/§3.2)
# ═══════════════════════════════════════════════════════════════════════════

def scoped_base_path(project_path: str, conv_id: str) -> str:
    """Resolve the base path project FILE TOOLS (and ``run_command`` cwd) should
    operate against for this conversation.

    This is the ONE seam build-order step 3 (§3.1/§3.2) needs: the project file
    tools thread ``project_path`` as an explicit parameter (they do NOT depend
    on ``os.getcwd()``), so worktree-scoping is a path-resolution change, not a
    ``chdir``. Under isolation this returns the conv's OWN worktree checkout
    (creating it on first use via :func:`ensure_worktree`); every read / write /
    grep / apply_diff / ``run_command`` cwd then flows against that worktree
    with no further change.

    Rollout seam (§6): when isolation is OFF (default) this returns
    ``project_path`` UNCHANGED — the caller sees byte-identical behavior, so a
    single-box install is untouched. It also returns ``project_path`` unchanged
    on ANY failure (not a git repo, worktree-add error): fail-open to the shared
    checkout so a worktree hiccup degrades to today's behavior rather than
    breaking the task. NOTE: this scopes ONLY the tool base; the Project-Brain
    coordination surfaces (presence / feed / board / modifications) keep using
    the ORIGINAL ``project_path`` so cross-conversation coordination stays keyed
    on the one real project, not fragmented per worktree.

    Returns the absolute worktree path, or ``project_path`` unchanged.
    """
    if not project_path or not conv_id or not is_isolation_enabled():
        return project_path
    try:
        res = ensure_worktree(project_path, conv_id)
        if res.get('ok') and res.get('path'):
            return res['path']
        logger.warning('[Worktree] scoped_base_path fell back to primary for '
                       'conv=%s: %s', conv_id[:12], res.get('error', '?'))
    except Exception as e:
        logger.warning('[Worktree] scoped_base_path errored for conv=%s: %s — '
                       'using primary checkout', conv_id[:12], e)
    return project_path


def commit_worktree(project_path: str, conv_id: str, message: str, *,
                    author: str | None = None) -> dict:
    """Commit ALL of this conversation's working changes onto its OWN branch.

    Inside an isolated worktree there is NOTHING to contaminate — the checkout
    contains ONLY this conversation's edits by construction — so a plain
    ``git add -A`` is SAFE here (the false-clean trap that ``project_commit``'s
    byte-identity gate defends against simply cannot occur in a per-conv
    worktree). This is the step-4 companion to :func:`land_worktree`: land
    merges the BRANCH, so the working-tree edits must first be committed onto
    it. A no-op ("nothing to commit") is a benign success.

    Returns ``{ok, committed?, sha?, nothing?, disabled?, error?}``. Never raises.
    """
    out: dict = {'ok': False}
    if not is_isolation_enabled():
        out['disabled'] = True
        return out
    wt = worktree_path(project_path, conv_id)
    if not (os.path.isdir(wt) and _is_git_repo(wt)):
        out['error'] = 'worktree missing — call ensure_worktree first'
        return out
    rc, dirty, _ = _git(wt, 'status', '--porcelain')
    if rc == 0 and not dirty.strip():
        out['ok'] = True
        out['nothing'] = True
        return out
    rc, _, err = _git(wt, 'add', '-A')
    if rc != 0:
        out['error'] = f'git add failed: {err.strip()[:200]}'
        return out
    author_str = (author or _agent_author())
    rc, _, err = _git(wt, 'commit', '-m', message or 'Worktree work',
                      '--author', author_str)
    if rc != 0:
        out['error'] = f'git commit failed: {err.strip()[:200]}'
        return out
    sha = _rev_parse(wt, 'HEAD')
    out['ok'] = True
    out['committed'] = True
    out['sha'] = sha[:12]
    audit_log('worktree_commit', project_path=_norm_base(project_path),
              conv_id=conv_id, branch=conv_branch(conv_id), sha=sha[:12])
    logger.info('[Worktree] committed conv=%s onto %s @ %s',
                conv_id[:12], conv_branch(conv_id), sha[:12])
    return out


def execute_land_tool(fn_args: dict, *, current_conv_id: str = '',
                      project_path: str = '') -> str:
    """Agent-tool entry point (isolation ``on`` mode) → human-readable string.

    Replaces ``project_commit`` when ``TOFU_WORKTREE_ISOLATION=on``: commit the
    conversation's worktree edits onto its branch, then CAS-merge that branch
    into the integration branch (:func:`land_worktree`), gating the merge-result.
    A conflict / red merge-result is REPORTED (resolve in the worktree), never
    forced onto the integration ref.
    """
    try:
        if not project_path:
            return ('Error: worktree land is only available in project mode '
                    '(open a project first).')
        message = (fn_args.get('message') or '').strip()
        if not message:
            return ('Provide a `message` describing the change to land it into '
                    'the integration branch.')
        test_paths = fn_args.get('test_paths') or []
        if test_paths and not isinstance(test_paths, list):
            test_paths = [str(test_paths)]

        commit = commit_worktree(project_path, current_conv_id, message)
        if not commit.get('ok'):
            return f'Land aborted — could not commit worktree: {commit.get("error", "unknown")}.'

        res = land_worktree(project_path, current_conv_id,
                            test_paths=test_paths, message=message)
        if res.get('ok'):
            return (f'Landed into {integration_branch()} @ {res["sha"][:12]} '
                    f'(after {res.get("retries", 1)} CAS round(s)). The human '
                    f'fast-forwards {integration_branch()} into their build '
                    f'branch at their own cadence.')
        if res.get('conflict'):
            return (f'Land held — merge conflict against {integration_branch()}. '
                    f'Resolve it in your worktree (sync_worktree rebases onto the '
                    f'latest integration HEAD), then land again. Nothing was '
                    f'published to the integration ref. Detail: {res.get("error", "")[:200]}')
        if res.get('merge_result_red'):
            return (f'Land held — the merge-result tests are RED (integration '
                    f'would break). Fix on your branch and land again; the ref '
                    f'was NOT moved.\n{res.get("testSummary", "")[-600:]}')
        if res.get('preflight_red'):
            return (f'Land held — your branch\'s own tests are RED. Fix them '
                    f'first.\n{res.get("testSummary", "")[-600:]}')
        if res.get('exhausted'):
            return (f'Land exhausted after {MAX_LAND_RETRIES} CAS rounds under '
                    f'heavy contention — retry shortly.')
        return f'Land failed: {res.get("error", "unknown")}.'
    except Exception as e:
        logger.warning('[Worktree] execute_land_tool failed: %s', e, exc_info=True)
        return f'Error executing worktree land: {e}'


def resolve_worktree_conflict(project_path: str, conv_id: str) -> dict:
    """Bring the latest integration HEAD INTO the conversation's worktree so a
    held land can be resolved and RE-LANDED — the recovery half of the land
    loop (design §5.2).

    Why a MERGE, not the rebase ``sync_worktree`` does: ``sync_worktree`` does
    ``git rebase integration`` and, on conflict, ABORTS — discarding the
    conflict state, so the conversation has nothing to resolve and re-landing
    just re-hits the same conflict forever (the "held → re-land → held" strand).
    This instead MERGES integration into the conv branch, leaving standard
    conflict markers in the worktree files. The conversation then resolves them
    with its normal edit tools and lands again: once the merge is committed,
    integration is an ANCESTOR of the conv branch, so the next
    :func:`land_worktree` is a trivial fast-forward → the loop CONVERGES.

    Any uncommitted worktree edits are committed onto the branch first (so they
    are not lost in the merge). A clean fast-forward (integration merges with no
    conflict) auto-commits and reports ``resolved`` with no markers.

    Returns ``{ok, conflict?, files?, already_current?, committed_pending?,
    disabled?, error?}``. Never raises.
    """
    out: dict = {'ok': False}
    if not is_isolation_enabled():
        out['disabled'] = True
        out['error'] = 'worktree isolation disabled'
        return out
    base = _norm_base(project_path)
    if not base or not _is_git_repo(base):
        out['error'] = 'not a git repository'
        return out
    wt = worktree_path(project_path, conv_id)
    if not (os.path.isdir(wt) and _is_git_repo(wt)):
        out['error'] = 'worktree missing — call ensure_worktree first'
        return out
    ib = integration_branch()
    integ = _rev_parse(base, f'refs/heads/{ib}')
    if not integ:
        out['error'] = f'integration branch {ib} missing'
        return out

    # Commit any pending worktree edits onto the branch so the merge can't lose
    # them (commit_worktree is a benign no-op when the tree is clean).
    commit = commit_worktree(project_path, conv_id,
                             'Worktree work (pre-sync commit)')
    if not commit.get('ok'):
        out['error'] = f'could not commit pending work: {commit.get("error", "unknown")}'
        return out
    out['committed_pending'] = bool(commit.get('committed'))

    branch = conv_branch(conv_id)
    branch_tip = _rev_parse(base, f'refs/heads/{branch}')
    # Already contains integration → nothing to reconcile.
    if branch_tip and _is_ancestor(base, integ, branch_tip):
        out['ok'] = True
        out['already_current'] = True
        return out

    # Merge integration INTO the conv worktree branch (markers on conflict).
    rc, _, err = _git(wt, 'merge', '--no-edit', ib)
    if rc == 0:
        out['ok'] = True
        out['conflict'] = False
        logger.info('[Worktree] resolve_worktree_conflict merged %s into '
                    'conv=%s cleanly', ib, conv_id[:12])
        return out
    # Conflict → leave the markers in place for in-worktree resolution.
    rc2, names, _ = _git(wt, 'diff', '--name-only', '--diff-filter=U')
    files = [f for f in (names or '').splitlines() if f.strip()]
    out['ok'] = True   # the SYNC succeeded; the conflict is now resolvable
    out['conflict'] = True
    out['files'] = files
    logger.info('[Worktree] resolve_worktree_conflict conv=%s left %d '
                'conflicted file(s) for resolution: %s',
                conv_id[:12], len(files), ', '.join(files[:8]))
    return out


def execute_sync_tool(fn_args: dict, *, current_conv_id: str = '',
                      project_path: str = '') -> str:
    """Agent-tool entry point (isolation ``on`` mode) → human-readable string.

    The recovery companion to :func:`execute_land_tool`: when a land is HELD on
    a merge conflict, call this to pull the latest integration HEAD into the
    worktree. A clean merge means you can land again immediately; a conflict
    leaves standard ``<<<<<<<`` markers in the named files — edit them with your
    normal file tools to resolve, then land again (it will fast-forward).
    """
    try:
        if not project_path:
            return ('Error: worktree sync is only available in project mode '
                    '(open a project first).')
        if not is_isolation_enabled():
            return ('Worktree isolation is off — there is no integration branch '
                    'to sync from; use project_commit on the shared checkout.')
        res = resolve_worktree_conflict(project_path, current_conv_id)
        if not res.get('ok'):
            return f'Sync failed: {res.get("error", "unknown")}.'
        if res.get('already_current'):
            return (f'Already up to date with {integration_branch()} — nothing '
                    f'to reconcile. If a prior land was held, just land again.')
        if not res.get('conflict'):
            return (f'Synced {integration_branch()} into your worktree cleanly '
                    f'(no conflict). Land again — it will fast-forward.')
        files = res.get('files') or []
        listing = '\n'.join(f'  - {f}' for f in files[:20])
        return (f'Synced {integration_branch()} into your worktree — {len(files)} '
                f'file(s) have merge conflicts you must resolve with your edit '
                f'tools (look for <<<<<<< / ======= / >>>>>>> markers), then '
                f'land again (it will fast-forward):\n{listing}')
    except Exception as e:
        logger.warning('[Worktree] execute_sync_tool failed: %s', e, exc_info=True)
        return f'Error executing worktree sync: {e}'


def reset_for_test() -> None:
    """Test-only hook (parity with ``rate_limit_store.reset_for_test``). This
    module holds no memoized process state — env + filesystem are the truth — so
    this is a no-op placeholder kept for a uniform test-harness contract."""
    return None


__all__ = [
    'isolation_mode', 'is_isolation_enabled', 'integration_branch',
    'conv_branch', 'worktrees_root', 'worktree_path',
    'ensure_integration_setup', 'ensure_worktree', 'refresh_lease',
    'sync_worktree', 'release_worktree', 'gc_worktrees', 'land_worktree',
    'scoped_base_path', 'commit_worktree', 'execute_land_tool',
    'resolve_worktree_conflict', 'execute_sync_tool',
    'reset_for_test', 'DEFAULT_LEASE_TTL_MS', 'MAX_LAND_RETRIES',
]
