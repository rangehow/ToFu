"""tests/test_project_board_wait_on_path.py — the wait-on-path primitive.

The dominant "stuck epic" failure mode on this project is "conversation X must
commit file Y before my epic can proceed" (3 of 4 stuck epics; this very
session is a 4th). ``depends_on`` only chains epic->epic, so the worker's only
recourse was a bare ``block``. wait-on-path lets an epic declare a dependency on
a PATH, resolved as the INVERSE READ of the existing path-lease: hold the epic
while a DIFFERENT conversation holds a LIVE lease on that path; release
automatically when that lease expires/releases.

Design doc: docs/PROJECT_BRAIN_WAIT_ON_PATH.md.

THIS suite covers the MECHANISM ONLY (owner asked to review before dispatch
wiring): the pure resolver ``_paths_waited_but_held``, the ``set_wait_paths``
setter, the ``_row_to_task`` nullable-safe field, and reset on complete/reopen.
It does NOT assert ``select_dispatchable`` integration — that lands after the
owner sees the mechanism.

Load-bearing invariants under test (mirror the cooldown's bar):
  • SELF-EXPIRING: a path held by a live lease holds the epic; once that lease
    expires (at read time, no reaper), the path is no longer "held" → the wait
    resolves. NO git-clean check (that couldn't self-expire).
  • FAIL-OPEN: empty list / missing column / a path nobody leases / the epic's
    OWN lease → NOT waiting (never strands).
  • NO third namespace: the resolver reads the SAME kind='lease' rows.
  • RESET on complete + reopen.

NC (load-bearing): revert the resolver's "different conversation" guard → an
epic's OWN lease on a path would wrongly hold the epic (self-deadlock).
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
    """This suite exercises the SHARED-TREE (isolation=off) wait-on-path model:
    a ``[sibling] path=`` block IS created and derives wait_paths. Under worktree
    isolation that same block is declined at creation (block_task's guard), so
    pin isolation OFF here regardless of the ambient TOFU_WORKTREE_ISOLATION so
    these mechanism tests are deterministic. The isolation-on behaviour has its
    own coverage (the block-guard decline tests)."""
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
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _row(flask_app, project_path, task_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        r = db.execute('SELECT wait_paths FROM project_tasks WHERE id=? AND project_path=?',
                       (task_id, project_path)).fetchone()
    return dict(r) if r else None


def _legacy_row(**over):
    """A project_tasks row mapping PREDATING the wait_paths column (no
    'wait_paths' key) — the pre-migration shape a defensive read must survive."""
    row = {
        'id': 'pt_legacy', 'title': 'legacy epic', 'status': 'open',
        'owner_conv_id': '', 'lease_expires_at': 0, 'created_by_conv': 'cA',
        'depends_on': '[]', 'dispatched': 0, 'kind': 'epic',
        'blocked_until': 0, 'block_count': 0, 'block_reason': '',
        'created_at': 0, 'updated_at': 0,
    }
    row.update(over)
    return row


# ════════════════════════════════════════════════════════════════════
#  schema: the column exists and reads nullable-safe
# ════════════════════════════════════════════════════════════════════

def test_row_exposes_wait_paths_field(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/w/1', 'cA', 'epic')['id']
        board = read_board('/w/1')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == []


def test_pre_migration_row_reads_empty_wait_paths():
    from lib.conversations.project_board import _row_to_task
    t = _row_to_task(_legacy_row(), now_ms=1_000_000)
    assert t['wait_paths'] == []


# ════════════════════════════════════════════════════════════════════
#  set_wait_paths — set / replace / clear
# ════════════════════════════════════════════════════════════════════

def test_set_wait_paths_persists(flask_app):
    from lib.conversations.project_board import post_task, read_board, set_wait_paths
    with flask_app.app_context():
        tid = post_task('/w/2', 'cA', 'epic')['id']
        res = set_wait_paths('/w/2', 'cA', tid, ['static/js/paper/report.js', 'lib/x.py'])
        board = read_board('/w/2')
    assert res['ok']
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == ['static/js/paper/report.js', 'lib/x.py']


def test_set_wait_paths_empty_clears(flask_app):
    from lib.conversations.project_board import post_task, read_board, set_wait_paths
    with flask_app.app_context():
        tid = post_task('/w/3', 'cA', 'epic')['id']
        set_wait_paths('/w/3', 'cA', tid, ['lib/x.py'])
        set_wait_paths('/w/3', 'cA', tid, [])
        board = read_board('/w/3')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == []


# ════════════════════════════════════════════════════════════════════
#  _paths_waited_but_held — the pure resolver (the heart)
# ════════════════════════════════════════════════════════════════════

def test_resolver_empty_when_no_wait():
    from lib.conversations.project_board import _paths_waited_but_held
    epic = {'id': 'e1', 'created_by_conv': 'cA', 'wait_paths': []}
    assert _paths_waited_but_held(epic, [], now_ms=1000) == []


def test_resolver_holds_when_path_held_by_other_live_lease():
    from lib.conversations.project_board import _paths_waited_but_held
    now = 1_000_000
    epic = {'id': 'e1', 'created_by_conv': 'cA', 'wait_paths': ['lib/x.py']}
    lease = {'id': 'pt_l', 'kind': 'lease', 'title': 'lib/x.py',
             'owner_conv_id': 'cB', 'status': 'claimed',
             'lease_expires_at': now + 60_000}
    held = _paths_waited_but_held(epic, [lease], now_ms=now)
    assert held == ['lib/x.py']


def test_resolver_releases_when_lease_expired():
    """SELF-EXPIRY: an EXPIRED lease no longer holds the path — the wait clears
    at read time, with no reaper. This is the whole reason it's lease-based and
    not git-clean-based."""
    from lib.conversations.project_board import _paths_waited_but_held
    now = 1_000_000
    epic = {'id': 'e1', 'created_by_conv': 'cA', 'wait_paths': ['lib/x.py']}
    expired = {'id': 'pt_l', 'kind': 'lease', 'title': 'lib/x.py',
               'owner_conv_id': 'cB', 'status': 'claimed',
               'lease_expires_at': now - 1}  # already lapsed
    assert _paths_waited_but_held(epic, [expired], now_ms=now) == []


def test_resolver_fail_open_when_nobody_leases_path():
    from lib.conversations.project_board import _paths_waited_but_held
    epic = {'id': 'e1', 'created_by_conv': 'cA', 'wait_paths': ['lib/x.py']}
    # a lease on a DIFFERENT path doesn't hold this epic
    other = {'id': 'pt_l', 'kind': 'lease', 'title': 'lib/other.py',
             'owner_conv_id': 'cB', 'status': 'claimed',
             'lease_expires_at': 9_999_999_999_999}
    assert _paths_waited_but_held(epic, [other], now_ms=1000) == []


def test_resolver_ignores_own_lease():
    """An epic waiting on a path IT ITSELF holds a lease on must NOT be held —
    that would be a self-deadlock. Only a DIFFERENT conversation's live lease
    counts."""
    from lib.conversations.project_board import _paths_waited_but_held
    now = 1_000_000
    epic = {'id': 'e1', 'created_by_conv': 'cA', 'wait_paths': ['lib/x.py']}
    own = {'id': 'pt_l', 'kind': 'lease', 'title': 'lib/x.py',
           'owner_conv_id': 'cA', 'status': 'claimed',
           'lease_expires_at': now + 60_000}
    assert _paths_waited_but_held(epic, [own], now_ms=now) == []


def test_resolver_partial_subset():
    from lib.conversations.project_board import _paths_waited_but_held
    now = 1_000_000
    epic = {'id': 'e1', 'created_by_conv': 'cA',
            'wait_paths': ['lib/a.py', 'lib/b.py']}
    leases = [
        {'id': 'l1', 'kind': 'lease', 'title': 'lib/a.py', 'owner_conv_id': 'cB',
         'status': 'claimed', 'lease_expires_at': now + 60_000},
        {'id': 'l2', 'kind': 'lease', 'title': 'lib/b.py', 'owner_conv_id': 'cB',
         'status': 'claimed', 'lease_expires_at': now - 1},  # expired
    ]
    assert _paths_waited_but_held(epic, leases, now_ms=now) == ['lib/a.py']


# ════════════════════════════════════════════════════════════════════
#  reset on complete + reopen
# ════════════════════════════════════════════════════════════════════

def test_complete_clears_wait_paths(flask_app):
    from lib.conversations.project_board import (
        complete_task, post_task, read_board, set_wait_paths,
    )
    with flask_app.app_context():
        tid = post_task('/w/4', 'cA', 'epic')['id']
        set_wait_paths('/w/4', 'cA', tid, ['lib/x.py'])
        complete_task('/w/4', 'cA', tid)
        board = read_board('/w/4')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == []


def test_reopen_clears_wait_paths(flask_app):
    from lib.conversations.project_board import (
        post_task, read_board, reopen_task, set_wait_paths,
    )
    with flask_app.app_context():
        tid = post_task('/w/5', 'cA', 'epic')['id']
        # claim so reopen has something to revive (claimed -> open)
        from lib.conversations.project_board import claim_task
        claim_task('/w/5', 'cA', tid)
        set_wait_paths('/w/5', 'cA', tid, ['lib/x.py'])
        reopen_task('/w/5', 'human', tid)
        board = read_board('/w/5')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == []


# ════════════════════════════════════════════════════════════════════
#  end-to-end via real lease rows (resolver reads the SAME rows)
# ════════════════════════════════════════════════════════════════════

def test_end_to_end_live_lease_holds_then_releases(flask_app):
    from lib.conversations.project_board import (
        _paths_waited_but_held, claim_lease, post_task, read_board,
        release_lease, set_wait_paths,
    )
    with flask_app.app_context():
        tid = post_task('/w/6', 'cA', 'epic waiting on report.js')['id']
        set_wait_paths('/w/6', 'cA', tid, ['static/js/paper/report.js'])
        # sibling cB holds the path
        claim_lease('/w/6', 'cB', 'static/js/paper/report.js')
        board = read_board('/w/6')
        import time
        now = int(time.time() * 1000)
        epic = [x for x in board['tasks'] if x['id'] == tid][0]
        held = _paths_waited_but_held(epic, board['tasks'], now_ms=now)
        assert held == ['static/js/paper/report.js'], 'live sibling lease must hold the epic'
        # sibling releases -> resolver clears
        release_lease('/w/6', 'cB', 'static/js/paper/report.js')
        board2 = read_board('/w/6')
        now2 = int(time.time() * 1000)
        epic2 = [x for x in board2['tasks'] if x['id'] == tid][0]
        assert _paths_waited_but_held(epic2, board2['tasks'], now_ms=now2) == []


# ════════════════════════════════════════════════════════════════════
#  PARSE CONTRACT — [sibling] block reason auto-populates wait_paths
#  from a STRUCTURED path= token (prose is NEVER scraped)
# ════════════════════════════════════════════════════════════════════
#
#  Contract (shown here so the test IS the spec):
#    • The ONLY path source is a structured token `path=<p1>,<p2>,...` in the
#      reason. Comma-separated; whitespace around each path is trimmed; the
#      token value ends at the first whitespace run (so trailing prose after a
#      space is NOT consumed).
#    • Paths are extracted ONLY when the reason also carries the `[sibling]`
#      class tag. A `[human-gated]` (or untagged) reason yields NO paths — a
#      human-gated block must never auto-hold on a path (it can't self-resolve
#      from a lease).
#    • Free-text mentions of a path WITHOUT the `path=` token yield NOTHING —
#      a worker's prose can neither accidentally populate nor be required to
#      populate the wait. Explicit-token-only = robust.

def test_parse_requires_sibling_tag():
    from lib.conversations.project_board import _parse_sibling_wait_paths
    assert _parse_sibling_wait_paths('[sibling] path=lib/x.py') == ['lib/x.py']
    # human-gated must NOT auto-hold on a path
    assert _parse_sibling_wait_paths('[human-gated] path=lib/x.py') == []
    # untagged
    assert _parse_sibling_wait_paths('waiting path=lib/x.py') == []


def test_parse_multiple_comma_separated():
    from lib.conversations.project_board import _parse_sibling_wait_paths
    got = _parse_sibling_wait_paths('[sibling] path=lib/x.py,static/js/report.js blocked on cB')
    assert got == ['lib/x.py', 'static/js/report.js']


def test_parse_no_token_yields_nothing():
    """Free-text scraping is FORBIDDEN — a reason that merely names a file in
    prose (no path= token) must populate NO wait paths."""
    from lib.conversations.project_board import _parse_sibling_wait_paths
    assert _parse_sibling_wait_paths('[sibling] waiting for lib/x.py to land') == []
    assert _parse_sibling_wait_paths('[sibling] the report.js refactor must commit') == []


def test_parse_dedupes():
    """The token value is comma-separated with NO internal spaces (a space ends
    the token — that's how trailing prose is excluded). Duplicates are dropped,
    order preserved."""
    from lib.conversations.project_board import _parse_sibling_wait_paths
    got = _parse_sibling_wait_paths('[sibling] path=lib/x.py,lib/x.py,lib/y.py blocked')
    assert got == ['lib/x.py', 'lib/y.py']


def test_parse_space_ends_token_excludes_trailing_prose():
    """A space terminates the path= value so following prose is never consumed
    into the last path (the anti-scraping guard)."""
    from lib.conversations.project_board import _parse_sibling_wait_paths
    got = _parse_sibling_wait_paths('[sibling] path=lib/x.py waiting for lib/y.py to land')
    assert got == ['lib/x.py'], 'prose after the space must not be captured'


# ════════════════════════════════════════════════════════════════════
#  block_task auto-populates wait_paths from a [sibling] path= reason
# ════════════════════════════════════════════════════════════════════

def test_block_sibling_path_populates_wait_paths(flask_app):
    from lib.conversations.project_board import block_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/w/7', 'cA', 'epic')['id']
        block_task('/w/7', 'cA', tid, '[sibling] path=static/js/paper/report.js')
        board = read_board('/w/7')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == ['static/js/paper/report.js']
    # cooldown STILL stamped (both mechanisms live)
    assert t['block_count'] == 1 and t['blocked_until'] > 0


def test_block_human_gated_does_not_populate_wait_paths(flask_app):
    from lib.conversations.project_board import block_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/w/8', 'cA', 'epic')['id']
        block_task('/w/8', 'cA', tid, '[human-gated] §10 infra path=lib/x.py')
        board = read_board('/w/8')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['wait_paths'] == [], 'human-gated block must NOT auto-hold on a path'
    assert t['block_count'] == 1  # cooldown still applies


# ════════════════════════════════════════════════════════════════════
#  NC — the "different conversation" guard is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_resolver_other_conv_guard_is_load_bearing():
    def run():
        import lib.conversations.project_board as pb
        now = 1_000_000
        epic = {'id': 'e1', 'created_by_conv': 'cA', 'wait_paths': ['lib/x.py']}
        own = {'id': 'pt_l', 'kind': 'lease', 'title': 'lib/x.py',
               'owner_conv_id': 'cA', 'status': 'claimed',
               'lease_expires_at': now + 60_000}
        held = pb._paths_waited_but_held(epic, [own], now_ms=now)
        assert held == ['lib/x.py'], \
            'NC: with the different-conversation guard removed, an epic would ' \
            'be held by its OWN lease (self-deadlock)'

    _patch_restore(
        _BOARD_SRC,
        "        if owner and owner != epic_conv:",
        "        if owner:  # NC (different-conv guard removed)",
        run,
    )
