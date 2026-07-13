"""tests/test_project_board_class_aware_backoff.py — Tier-2 bleeding-control.

Interim corrections to the CURRENT (shared-tree) board model while the
worktree-isolation redesign (docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md) is built.
They stop the reported "autonomous promotion always blocked by collaboration"
oscillation without removing the shared tree.

Two faults, two fixes:

(a) CLASS-AWARE BACKOFF — ``_block_cooldown_ms`` was class-agnostic: a transient
    ``[sibling]`` block (auto-resolves the instant a sibling commits) escalated
    the SAME exponential curve as a ``[human-gated]`` block, ratcheting an epic
    toward a 24 h sleep on ordinary collaboration churn. Fix: a ``[sibling]``
    block tracks the LEASE clock (flat ``SIBLING_BLOCK_COOLDOWN_MS`` ==
    ``DEFAULT_LEASE_TTL_MS``, NO escalation); the exponential curve is reserved
    strictly for ``[human-gated]`` / untagged.

(b) EVENT-DRIVEN wait_paths — after a sibling RELEASED a contested path, the
    waiting epic still slept out its (now separate) cooldown, so the recovery
    latency exceeded the actual blocker. Fix: ``release_lease`` WAKES every epic
    whose ``wait_paths`` contains the released resource by clearing its
    ``blocked_until`` → the epic is dispatchable on the next sweep, not one
    cooldown later. Lease rows carry ``wait_paths='[]'`` so they are never woken;
    the crash path stays covered because the sibling cooldown now equals the
    lease TTL (aligned expiry).

Load-bearing negative control:
  • NC — byte-revert the ``release_lease`` wake call → an epic waiting on a
    released path stays cooldown-suppressed (reproduces the recovery-latency gap).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _shared_tree_mode(monkeypatch):
    """This suite exercises the SHARED-TREE (isolation=off) board model (see the
    module docstring: 'CURRENT (shared-tree) board model'): a ``[sibling] path=``
    block IS created and derives wait_paths + cooldown. Under worktree isolation
    that block is declined at creation (block_task's guard), so pin isolation OFF
    here regardless of the ambient TOFU_WORKTREE_ISOLATION so these tests are
    deterministic. The isolation-on decline has its own coverage."""
    monkeypatch.setattr(
        'lib.conversations.project_worktree.is_isolation_enabled',
        lambda: False)


@pytest.fixture(autouse=True)
def _clean(flask_app):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks')
        db.execute('DELETE FROM project_events')
        db.execute('DELETE FROM message_queue')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _row(flask_app, project_path, task_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        r = db.execute(
            'SELECT blocked_until, block_count, block_reason, wait_paths, status '
            'FROM project_tasks WHERE id=? AND project_path=?',
            (task_id, project_path)).fetchone()
    return dict(r) if r else None


# ════════════════════════════════════════════════════════════════════
#  (a) _block_cooldown_ms — the class-aware pure helper
# ════════════════════════════════════════════════════════════════════

def test_cooldown_ms_class_param_defaults_to_human():
    """Backward compat: the single-arg call keeps the exponential human curve."""
    from lib.conversations.project_board import (
        BLOCK_COOLDOWN_BASE_MS, _block_cooldown_ms,
    )
    assert _block_cooldown_ms(0) == 0
    assert _block_cooldown_ms(1) == BLOCK_COOLDOWN_BASE_MS
    assert _block_cooldown_ms(1) == _block_cooldown_ms(1, 'human')


def test_sibling_cooldown_is_flat_lease_clock():
    """A [sibling] block tracks the lease clock: flat == DEFAULT_LEASE_TTL_MS,
    NO escalation regardless of block_count."""
    from lib.conversations.project_board import (
        DEFAULT_LEASE_TTL_MS, SIBLING_BLOCK_COOLDOWN_MS, _block_cooldown_ms,
    )
    assert SIBLING_BLOCK_COOLDOWN_MS == DEFAULT_LEASE_TTL_MS
    sib = [_block_cooldown_ms(n, 'sibling') for n in range(1, 9)]
    assert all(v == SIBLING_BLOCK_COOLDOWN_MS for v in sib), \
        'sibling cooldown must be flat (no escalation)'


def test_human_curve_still_escalates_and_dominates_sibling():
    """Regression: the human curve is unchanged AND, past the first block,
    escalates strictly above the flat sibling clock — proof the sibling class
    can never ratchet toward the 24 h cap."""
    from lib.conversations.project_board import _block_cooldown_ms
    human = [_block_cooldown_ms(n, 'human') for n in range(1, 6)]
    for a, b in zip(human, human[1:]):
        assert b >= a
    # by the 2nd block the human curve exceeds the flat sibling clock
    assert _block_cooldown_ms(2, 'human') > _block_cooldown_ms(2, 'sibling')


# ════════════════════════════════════════════════════════════════════
#  (a) block_task — class derived from the [sibling] tag
# ════════════════════════════════════════════════════════════════════

def test_sibling_block_does_not_escalate(flask_app):
    from lib.conversations.project_board import (
        SIBLING_BLOCK_COOLDOWN_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/caw/1', 'cA', 'epic blocked by a sibling')['id']
        block_task('/caw/1', 'cA', tid, '[sibling] path=lib/x.py waiting on commit')
        before2 = _now_ms()
        block_task('/caw/1', 'cA', tid, '[sibling] path=lib/x.py still waiting')
    row = _row(flask_app, '/caw/1', tid)
    # 2nd sibling block still schedules a retry ~one lease clock out, NOT escalated
    assert row['block_count'] == 2
    assert row['blocked_until'] <= before2 + SIBLING_BLOCK_COOLDOWN_MS + 5_000, \
        'a repeated [sibling] block must NOT escalate beyond the lease clock'


def test_human_gated_block_still_escalates(flask_app):
    from lib.conversations.project_board import (
        SIBLING_BLOCK_COOLDOWN_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/caw/2', 'cA', 'human-gated epic')['id']
        block_task('/caw/2', 'cA', tid, '[human-gated] §10 sign-off 1')
        before2 = _now_ms()
        block_task('/caw/2', 'cA', tid, '[human-gated] §10 sign-off 2')
    row = _row(flask_app, '/caw/2', tid)
    assert row['block_count'] == 2
    # the 2nd human block escalates well past the flat sibling clock
    assert row['blocked_until'] > before2 + SIBLING_BLOCK_COOLDOWN_MS, \
        'a [human-gated] block must still escalate exponentially'


def test_untagged_block_treated_as_human(flask_app):
    """Conservative default: an untagged reason uses the human curve (a genuine
    unknown gate should escalate, not be treated as transient)."""
    from lib.conversations.project_board import (
        SIBLING_BLOCK_COOLDOWN_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/caw/3', 'cA', 'untagged block')['id']
        block_task('/caw/3', 'cA', tid, 'gate 1')
        before2 = _now_ms()
        block_task('/caw/3', 'cA', tid, 'gate 2')
    row = _row(flask_app, '/caw/3', tid)
    assert row['blocked_until'] > before2 + SIBLING_BLOCK_COOLDOWN_MS


# ════════════════════════════════════════════════════════════════════
#  (b) release_lease wakes wait-path waiters (event-driven recovery)
# ════════════════════════════════════════════════════════════════════

def _blocked_and_held(flask_app, project_path, path, *, holder='cB', owner='cA'):
    """Set up: sibling ``holder`` leases ``path``; an epic owned by ``owner`` is
    blocked [sibling] on that path (→ wait_paths + future cooldown). Returns tid."""
    from lib.conversations.project_board import block_task, claim_lease, post_task
    with flask_app.app_context():
        claim_lease(project_path, holder, path)
        tid = post_task(project_path, owner, f'epic waiting on {path}')['id']
        block_task(project_path, owner, tid, f'[sibling] path={path} waiting on commit')
    return tid


def test_release_lease_wakes_wait_path_waiter(flask_app):
    from lib.conversations.project_board import release_lease
    from lib.conversations.project_dispatch import select_dispatchable
    tid = _blocked_and_held(flask_app, '/caw/4', 'lib/x.py')
    with flask_app.app_context():
        # held on BOTH gates (live lease + cooldown) → not dispatchable
        assert tid not in [c['id'] for c in select_dispatchable('/caw/4')]
        release_lease('/caw/4', 'cB', 'lib/x.py')
        cands = [c['id'] for c in select_dispatchable('/caw/4')]
    row = _row(flask_app, '/caw/4', tid)
    assert row['blocked_until'] == 0, \
        'releasing the awaited path must clear the waiting epic cooldown'
    assert tid in cands, \
        'the epic must be dispatchable immediately on release, not one cooldown later'


def test_release_only_wakes_epics_waiting_on_that_path(flask_app):
    from lib.conversations.project_board import release_lease
    tid_x = _blocked_and_held(flask_app, '/caw/5', 'lib/x.py', owner='cA')
    tid_y = _blocked_and_held(flask_app, '/caw/5', 'lib/y.py', holder='cC', owner='cA')
    with flask_app.app_context():
        release_lease('/caw/5', 'cB', 'lib/x.py')
    row_x = _row(flask_app, '/caw/5', tid_x)
    row_y = _row(flask_app, '/caw/5', tid_y)
    assert row_x['blocked_until'] == 0, 'the x.py waiter is woken'
    assert row_y['blocked_until'] > 0, 'the y.py waiter is NOT woken (wrong path)'


def test_release_nonwaited_path_is_noop(flask_app):
    from lib.conversations.project_board import claim_lease, release_lease
    tid = _blocked_and_held(flask_app, '/caw/6', 'lib/x.py')
    with flask_app.app_context():
        # a lease on an unrelated path, released → must not touch the x.py waiter
        claim_lease('/caw/6', 'cB', 'lib/unrelated.py')
        release_lease('/caw/6', 'cB', 'lib/unrelated.py')
    row = _row(flask_app, '/caw/6', tid)
    assert row['blocked_until'] > 0, \
        'releasing an unrelated path must not wake the x.py waiter'


# ════════════════════════════════════════════════════════════════════
#  NC — the release_lease wake call is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_release_wake_is_load_bearing(flask_app):
    def run():
        import lib.conversations.project_board as pb
        from lib.conversations.project_dispatch import select_dispatchable
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncw'")
            get_thread_db(DOMAIN_CHAT).commit()
            pb.claim_lease('/ncw', 'cB', 'lib/x.py')
            tid = pb.post_task('/ncw', 'cA', 'epic waiting on x.py')['id']
            pb.block_task('/ncw', 'cA', tid, '[sibling] path=lib/x.py waiting')
            pb.release_lease('/ncw', 'cB', 'lib/x.py')
            row = get_thread_db(DOMAIN_CHAT).execute(
                'SELECT blocked_until FROM project_tasks WHERE id=? AND project_path=?',
                (tid, '/ncw')).fetchone()
        assert int(row['blocked_until'] or 0) > 0, \
            'NC: with the release wake removed, a released-path waiter stays ' \
            'cooldown-suppressed (reproduces the recovery-latency gap)'

    _patch_restore(
        _BOARD_SRC,
        '        _wake_wait_path_waiters(db, project_path, resource, now)\n',
        '        pass  # NC (release wake disabled)\n',
        run,
    )
