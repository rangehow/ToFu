"""tests/test_project_ready_marker.py — the ready-to-land board marker
(kind='ready') + overlap serialization + autonomous landing loop.

This is step 3 of the continuous-atomic-slice-landing north star: a slice that
is GREEN on the codified acceptance gate (lib/conversations/project_acceptance)
auto-posts a board marker; the brain then auto-lands the maximal DISJOINT set
of pending markers via project_commit — with any two file-set-overlapping
markers BOTH held for human authorization, and a re-gate before each landing so
a marker whose HEAD moved (no longer self-consistent) is never landed blind.

No schema change: a marker is a project_tasks row with kind='ready', reusing
existing columns (dispatch_target=at_ref, block_reason=JSON descriptor). It is
DENYLISTED from select_dispatchable exactly like kind='lease'.

Acceptance criteria (owner, all RED-first):
  1. green gate → auto-post kind='ready'; select_dispatchable never dispatches it.
  2. overlap serialization: A∩B≠∅, C disjoint → C landable, A+B held.
  3. autonomous commit invokes project_commit.do_commit (agent author); a marker
     whose re-gate goes stale (no longer ok) is NOT landed.
  4. self-healing: if do_commit excludes files as contaminated (a sibling raced
     into the slice), the marker is NOT marked landed → re-gates next sweep.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


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


# ════════════════════════════════════════════════════════════════════
#  Criterion 1 — green gate → auto-post; never dispatched as an epic
# ════════════════════════════════════════════════════════════════════

def test_gate_and_post_posts_marker_when_green(flask_app, monkeypatch):
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_ready as pr
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': [],
        'testSummary': '3 passed'})
    with flask_app.app_context():
        res = pr.gate_and_post('/r/1', 'cA', files=['lib/x.py'],
                               test_paths=['tests/test_x.py'], at_ref='HEAD')
        markers = pr.read_ready_markers('/r/1')
    assert res['posted'] is True and res['markerId']
    assert len(markers) == 1
    m = markers[0]
    assert m['files'] == ['lib/x.py'] and m['testPaths'] == ['tests/test_x.py']
    assert m['green'] is True and m['selfConsistent'] is True
    assert m['conv'] == 'cA'


def test_gate_and_post_does_not_post_when_not_ok(flask_app, monkeypatch):
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_ready as pr
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': False, 'green': True, 'selfConsistent': False,
        'orphans': [{'symbol': 'foo', 'referencedBy': ['other.py']}]})
    with flask_app.app_context():
        res = pr.gate_and_post('/r/2', 'cA', files=['lib/x.py'],
                               test_paths=['tests/test_x.py'])
        markers = pr.read_ready_markers('/r/2')
    assert res['posted'] is False
    assert markers == []


def test_ready_marker_never_dispatched(flask_app, monkeypatch):
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_ready as pr
    from lib.conversations.project_dispatch import select_dispatchable
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    with flask_app.app_context():
        pr.gate_and_post('/r/3', 'cA', files=['lib/x.py'],
                         test_paths=['t.py'])
        cands = [c['id'] for c in select_dispatchable('/r/3')]
        markers = pr.read_ready_markers('/r/3')
    assert markers, 'marker should exist'
    assert markers[0]['id'] not in cands, \
        'a ready marker must NEVER be dispatched as an epic'


def test_NC_ready_dispatch_skip_is_load_bearing(flask_app):
    """Byte-revert the kind=='ready' skip in select_dispatchable → a ready
    marker (status open, no lease) LEAKS into the dispatch candidate set,
    reproducing the defect (the brain would spawn a billed kickoff on a marker
    that is not work)."""
    def run():
        import lib.conversations.project_dispatch as pd
        import lib.conversations.project_ready as pr
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncr'")
            get_thread_db(DOMAIN_CHAT).commit()
            mid = pr.post_ready_marker(
                '/ncr', 'cA', files=['lib/x.py'], test_paths=['t.py'],
                at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
            cands = [c['id'] for c in pd.select_dispatchable('/ncr')]
        assert mid in cands, \
            'NC: with the ready-skip removed, a ready marker must LEAK into ' \
            'the dispatch candidate set'

    _patch_restore(
        _DISPATCH_SRC,
        "        if t.get('kind') in ('lease', 'ready'):\n            continue\n",
        "        if t.get('kind') == 'lease':  # NC (ready-skip removed)\n            continue\n",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  Criterion 2 — overlap serialization
# ════════════════════════════════════════════════════════════════════

def test_landable_is_maximal_disjoint_set(flask_app):
    """A∩B≠∅ (share lib/shared.py), C disjoint → C landable; A+B held."""
    import lib.conversations.project_ready as pr
    with flask_app.app_context():
        pr.post_ready_marker('/r/4', 'cA', files=['lib/shared.py', 'lib/a.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/r/4', 'cB', files=['lib/shared.py', 'lib/b.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/r/4', 'cC', files=['lib/c.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        land = pr.landable_markers('/r/4')
        held = pr.held_markers('/r/4')
    land_convs = {m['conv'] for m in land}
    held_convs = {m['conv'] for m in held}
    assert land_convs == {'cC'}, 'only the disjoint slice C is landable'
    assert held_convs == {'cA', 'cB'}, 'the overlapping A+B are both held'


def test_all_disjoint_all_landable(flask_app):
    import lib.conversations.project_ready as pr
    with flask_app.app_context():
        pr.post_ready_marker('/r/5', 'cA', files=['lib/a.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/r/5', 'cB', files=['lib/b.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        land = pr.landable_markers('/r/5')
    assert {m['conv'] for m in land} == {'cA', 'cB'}


# ════════════════════════════════════════════════════════════════════
#  Criterion 3 — autonomous commit + re-gate-on-stale-HEAD
# ════════════════════════════════════════════════════════════════════

def test_auto_land_invokes_do_commit_for_disjoint_green(flask_app, monkeypatch):
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_ready as pr

    # re-gate is green + consistent
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    calls = []

    def _fake_commit(project_path, conv_id, message, *, files=None, author=None):
        calls.append({'conv': conv_id, 'files': files, 'author': author})
        return {'ok': True, 'commitSha': 'deadbeef1234', 'committed': list(files),
                'excluded': [], 'verified': True}
    monkeypatch.setattr(pc, 'do_commit', _fake_commit)

    with flask_app.app_context():
        pr.post_ready_marker('/r/6', 'cA', files=['lib/a.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        res = pr.auto_land_ready('/r/6')
        remaining = pr.read_ready_markers('/r/6')
    assert calls, 'auto_land must invoke project_commit.do_commit'
    assert calls[0]['files'] == ['lib/a.py']
    # agent author (do_commit defaults to Tofu Agent; we pass None to let it)
    assert 'cA' in res['landed'] or any('lib/a.py' in c['files'] for c in calls)
    assert remaining == [], 'a successfully landed marker is removed'


def test_auto_land_regates_and_skips_stale_marker(flask_app, monkeypatch):
    """Criterion 3: a marker whose HEAD moved so it is no longer self-consistent
    is RE-GATED before landing and NOT landed (never landed blind)."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_ready as pr

    # re-gate now FAILS (stale HEAD: an orphan appeared)
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': False, 'green': True, 'selfConsistent': False,
        'orphans': [{'symbol': 'gone', 'referencedBy': ['z.py']}]})
    committed = []
    monkeypatch.setattr(pc, 'do_commit', lambda *a, **k: committed.append(1) or {'ok': True})

    with flask_app.app_context():
        pr.post_ready_marker('/r/7', 'cA', files=['lib/a.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        res = pr.auto_land_ready('/r/7')
        remaining = pr.read_ready_markers('/r/7')
    assert not committed, 'a stale marker must NOT be committed'
    assert 'cA' in res['skipped'] or res['landed'] == []
    assert remaining, 'a skipped-stale marker stays for the next sweep'


# ════════════════════════════════════════════════════════════════════
#  Criterion 4 — self-healing on contamination race
# ════════════════════════════════════════════════════════════════════

def test_auto_land_does_not_mark_landed_on_contamination(flask_app, monkeypatch):
    """A sibling raced into the slice's files after the gate ran → do_commit
    excludes them as contaminated (nothing clean). The marker must NOT be
    marked landed → it re-gates next sweep. No billed churn (do_commit is a
    no-op commit here, not a loop)."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_ready as pr

    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    # do_commit refuses: all declared files contaminated (sibling raced in).
    monkeypatch.setattr(pc, 'do_commit', lambda *a, **k: {
        'ok': False, 'error': 'nothing clean to commit',
        'committed': [], 'excluded': [{'path': 'lib/a.py', 'reason': 'foreign hunks'}]})

    with flask_app.app_context():
        pr.post_ready_marker('/r/8', 'cA', files=['lib/a.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        res = pr.auto_land_ready('/r/8')
        remaining = pr.read_ready_markers('/r/8')
    assert 'cA' not in res['landed'], 'a contaminated slice is NOT marked landed'
    assert remaining, 'the marker stays so it re-gates next sweep (self-healing)'


def test_auto_land_lands_disjoint_holds_overlapping(flask_app, monkeypatch):
    """End-to-end criterion 2+3: three markers (A∩B, C disjoint) → auto_land
    commits ONLY C, holds A+B for human authorization."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_ready as pr

    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    committed_files = []

    def _fake_commit(project_path, conv_id, message, *, files=None, author=None):
        committed_files.append(tuple(files))
        return {'ok': True, 'commitSha': 'abc123', 'committed': list(files),
                'excluded': [], 'verified': True}
    monkeypatch.setattr(pc, 'do_commit', _fake_commit)

    with flask_app.app_context():
        pr.post_ready_marker('/r/9', 'cA', files=['lib/shared.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/r/9', 'cB', files=['lib/shared.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/r/9', 'cC', files=['lib/c.py'], test_paths=['t.py'],
                             at_ref='HEAD', gate_result={'green': True, 'selfConsistent': True})
        res = pr.auto_land_ready('/r/9')
        remaining_convs = {m['conv'] for m in pr.read_ready_markers('/r/9')}
    assert committed_files == [('lib/c.py',)], 'only the disjoint slice C commits'
    assert 'cC' in res['landed']
    assert set(res['held']) == {'cA', 'cB'}
    assert remaining_convs == {'cA', 'cB'}, 'the overlapping pair stays held'


# ════════════════════════════════════════════════════════════════════
#  Producer trigger — an agent declaring its slice done posts a marker
#  (closes the entry gap: auto_land_ready has nothing to consume unless
#  SOMETHING produces markers; a human calling gate_and_post by hand is the
#  same inert ritual the loop's exit eliminated).
# ════════════════════════════════════════════════════════════════════

def test_ready_land_tool_posts_marker_on_green(flask_app, monkeypatch):
    """The producer: an agent invokes project_ready_land declaring its slice
    (files + test_paths); a green gate posts a marker WITHOUT a human."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_ready as pr
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': [],
        'testSummary': '5 passed'})
    with flask_app.app_context():
        out = pr.execute_ready_land_tool(
            {'files': ['lib/x.py'], 'test_paths': ['tests/test_x.py']},
            current_conv_id='cA', project_path='/prod/1')
        markers = pr.read_ready_markers('/prod/1')
    assert isinstance(out, str) and 'ready' in out.lower()
    assert len(markers) == 1 and markers[0]['conv'] == 'cA'
    assert markers[0]['files'] == ['lib/x.py']


def test_ready_land_tool_reports_gate_failure_no_marker(flask_app, monkeypatch):
    """A non-ok gate posts NO marker and the tool explains why (orphan/red)."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_ready as pr
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': False, 'green': False, 'selfConsistent': True, 'orphans': [],
        'testSummary': '1 failed'})
    with flask_app.app_context():
        out = pr.execute_ready_land_tool(
            {'files': ['lib/x.py'], 'test_paths': ['t.py']},
            current_conv_id='cA', project_path='/prod/2')
        markers = pr.read_ready_markers('/prod/2')
    assert markers == []
    assert 'not' in out.lower() or 'fail' in out.lower()


def test_ready_land_tool_requires_files_and_tests(flask_app):
    """A declare-error (no files or no test_paths) is a clear message, not a
    gate run — mirrors project_commit's required-files discipline."""
    import lib.conversations.project_ready as pr
    with flask_app.app_context():
        o1 = pr.execute_ready_land_tool({'test_paths': ['t.py']},
                                        current_conv_id='cA', project_path='/prod/3')
        o2 = pr.execute_ready_land_tool({'files': ['lib/x.py']},
                                        current_conv_id='cA', project_path='/prod/3')
        markers = pr.read_ready_markers('/prod/3')
    assert 'files' in o1.lower()
    assert 'test' in o2.lower()
    assert markers == []


def test_ready_land_real_gate_posts_on_green_not_on_orphan(flask_app, tmp_path):
    """REAL-TREE producer proof (owner's hard bar): with the GENUINE acceptance
    gate (NO stub) against a real temp git repo, a green slice auto-posts a
    marker and an orphan slice posts nothing. This is what proves finishing
    green work produces a marker without a human."""
    import subprocess
    import lib.conversations.project_ready as pr

    def _git(d, *a):
        subprocess.run(['git', *a], cwd=d, check=True, capture_output=True, text=True)

    def _repo(name):
        d = str(tmp_path / name)
        os.makedirs(d, exist_ok=True)
        _git(d, 'init', '-q'); _git(d, 'config', 'user.email', 'x@y')
        _git(d, 'config', 'user.name', 'x')
        (tmp_path / name / 'a.py').write_text('def a():\n    return 1\n')
        (tmp_path / name / 'b.py').write_text('def b():\n    return 2\n')
        (tmp_path / name / 'test_a.py').write_text('def test_a():\n    assert True\n')
        _git(d, 'add', '-A'); _git(d, 'commit', '-q', '-m', 'base')
        return d

    with flask_app.app_context():
        # GREEN: additive edit, test passes, no orphan → marker posted.
        d = _repo('green')
        (tmp_path / 'green' / 'a.py').write_text(
            'def a():\n    return 1\n\n\ndef a2():\n    return 9\n')
        out = pr.execute_ready_land_tool(
            {'files': ['a.py'], 'test_paths': ['test_a.py']},
            current_conv_id='realA', project_path=d)
        green_markers = pr.read_ready_markers(d)

        # ORPHAN: remove b() while a committed caller imports it → NO marker.
        d2 = _repo('orphan')
        (tmp_path / 'orphan' / 'caller.py').write_text(
            'from b import b\n\n\ndef use():\n    return b()\n')
        _git(d2, 'add', 'caller.py'); _git(d2, 'commit', '-q', '-m', 'caller')
        (tmp_path / 'orphan' / 'b.py').write_text('def other():\n    return 0\n')
        out2 = pr.execute_ready_land_tool(
            {'files': ['b.py'], 'test_paths': ['test_a.py']},
            current_conv_id='realB', project_path=d2)
        orphan_markers = pr.read_ready_markers(d2)

    assert len(green_markers) == 1 and green_markers[0]['conv'] == 'realA', \
        'a genuinely-green slice must auto-post a marker (real gate)'
    assert 'ready' in out.lower()
    assert orphan_markers == [], 'an orphan slice must post NO marker (real gate)'
    assert 'split-brain' in out2.lower() or 'orphan' in out2.lower()


def test_ready_land_routes_through_execute_board_tool(flask_app, monkeypatch):
    """The clean router: execute_board_tool dispatches project_ready_land to the
    producer (proves the wiring in the uncontested project_board.py works — the
    agent-visible SPEC in conversation.py is the only deferred piece)."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_ready as pr
    from lib.conversations.project_board import execute_board_tool
    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    with flask_app.app_context():
        out = execute_board_tool(
            'project_ready_land',
            {'files': ['lib/y.py'], 'test_paths': ['t.py']},
            current_conv_id='cB', project_path='/prod/4')
        markers = pr.read_ready_markers('/prod/4')
    assert 'Unknown board tool' not in out
    assert len(markers) == 1 and markers[0]['conv'] == 'cB'


# ════════════════════════════════════════════════════════════════════
#  Criterion (arm the heartbeat) — sweep_dispatch turns the loop itself
# ════════════════════════════════════════════════════════════════════

def test_sweep_dispatch_auto_lands_green_marker(flask_app, monkeypatch):
    """The autonomy half: a green ready marker on a project is AUTO-LANDED by
    one sweep_dispatch pass (the 30s heartbeat seam), without a human calling
    auto_land_ready by hand. This is what makes the built loop actually turn."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_dispatch as pd
    import lib.conversations.project_ready as pr

    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    committed = []

    def _fake_commit(project_path, conv_id, message, *, files=None, author=None):
        committed.append((conv_id, tuple(files)))
        return {'ok': True, 'commitSha': 'sha_sweep', 'committed': list(files),
                'excluded': [], 'verified': True}
    monkeypatch.setattr(pc, 'do_commit', _fake_commit)

    with flask_app.app_context():
        pr.post_ready_marker('/sweep/1', 'cA', files=['lib/only.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pd.sweep_dispatch('/sweep/1')          # ← the heartbeat pass
        remaining = pr.read_ready_markers('/sweep/1')
    assert committed == [('cA', ('lib/only.py',))], \
        'sweep_dispatch must auto-land the green marker via project_commit'
    assert remaining == [], 'the landed marker is cleared by the sweep'


def test_sweep_holds_overlapping_markers(flask_app, monkeypatch):
    """One sweep pass lands only the disjoint slice and leaves the overlapping
    pair HELD (they persist across the sweep for human authorization)."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_dispatch as pd
    import lib.conversations.project_ready as pr

    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    committed = []
    monkeypatch.setattr(pc, 'do_commit', lambda project_path, conv_id, message, *, files=None, author=None: (
        committed.append(tuple(files)) or {'ok': True, 'commitSha': 's', 'committed': list(files), 'excluded': []}))

    with flask_app.app_context():
        pr.post_ready_marker('/sweep/2', 'cA', files=['lib/shared.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/sweep/2', 'cB', files=['lib/shared.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/sweep/2', 'cC', files=['lib/c.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pd.sweep_dispatch('/sweep/2')
        remaining = {m['conv'] for m in pr.read_ready_markers('/sweep/2')}
    assert committed == [('lib/c.py',)], 'only the disjoint slice C is landed by the sweep'
    assert remaining == {'cA', 'cB'}, 'the overlapping pair stays held across the sweep'


def test_NC_sweep_autoland_is_load_bearing(flask_app, monkeypatch):
    """Byte-revert the auto_land_ready call in sweep_dispatch → a green marker
    is NOT landed by the sweep (reproduces the inert-loop defect: the loop is
    built but nothing turns it)."""
    import lib.conversations.project_acceptance as pa
    import lib.conversations.project_commit as pc
    import lib.conversations.project_ready as pr

    monkeypatch.setattr(pa, 'run_acceptance_gate', lambda *a, **k: {
        'ok': True, 'green': True, 'selfConsistent': True, 'orphans': []})
    committed = []
    monkeypatch.setattr(pc, 'do_commit', lambda *a, **k: committed.append(1) or {
        'ok': True, 'commitSha': 's', 'committed': list(k.get('files') or []), 'excluded': []})

    def run():
        import lib.conversations.project_dispatch as pd
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncsweep'")
            get_thread_db(DOMAIN_CHAT).commit()
            pr.post_ready_marker('/ncsweep', 'cA', files=['lib/x.py'],
                                 test_paths=['t.py'], at_ref='HEAD',
                                 gate_result={'green': True, 'selfConsistent': True})
            pd.sweep_dispatch('/ncsweep')
        assert not committed, \
            'NC: with the auto_land call removed, the sweep must NOT land the ' \
            'marker (the loop is inert — this is the defect the wiring fixes)'

    _patch_restore(
        _DISPATCH_SRC,
        "    try:\n        _auto_land_ready_markers(project_path)\n    except Exception as e:\n"
        "        logger.debug('[Dispatch] auto-land pass skipped proj=%.40r: %s', project_path, e)\n",
        "    pass  # NC (auto-land wiring removed)\n",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  Perceptibility — the Landing section of the always-surfaced board block
#  (render_board_block is the human-facing text surface; no contested files)
# ════════════════════════════════════════════════════════════════════

def test_board_block_renders_landing_section(flask_app):
    """A pending-ready marker appears in a 'Landing' section of the board block,
    with its files + conv — so a human perceives the queue in the surface that
    is re-read every turn."""
    import lib.conversations.project_ready as pr
    from lib.conversations.project_board import render_board_block
    with flask_app.app_context():
        pr.post_ready_marker('/rb/1', 'cA', files=['lib/x.py', 'lib/y.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        block = render_board_block('/rb/1', current_conv_id='cREADER')
    assert 'Landing' in block, 'the board block must show a Landing section'
    assert 'lib/x.py' in block and 'lib/y.py' in block, 'slice files must be shown'
    assert 'cA' in block, 'the owning conversation must be shown'


def test_board_block_landing_marks_held_overlap(flask_app):
    """The held-for-overlap cluster (the part a human must SEQUENCE) is called
    out distinctly from a cleanly-landable slice."""
    import lib.conversations.project_ready as pr
    from lib.conversations.project_board import render_board_block
    with flask_app.app_context():
        # A∩B share lib/shared.py → both held; C disjoint → landable.
        pr.post_ready_marker('/rb/2', 'cA', files=['lib/shared.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/rb/2', 'cB', files=['lib/shared.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        pr.post_ready_marker('/rb/2', 'cC', files=['lib/c.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        block = render_board_block('/rb/2', current_conv_id='cREADER')
    low = block.lower()
    assert 'landing' in low
    # the overlap must be surfaced as needing human sequencing
    assert 'overlap' in low or 'sequence' in low or 'authoriz' in low, \
        'the held-for-overlap cluster must be flagged for the human to sequence'
    assert 'lib/shared.py' in block


def test_board_block_ready_marker_not_in_open_lane(flask_app):
    """A ready marker (kind='ready', status open) must NOT render in the Open
    lane (where it would read as a claimable epic) — it is partitioned into the
    Landing section only."""
    import lib.conversations.project_ready as pr
    from lib.conversations.project_board import post_task, render_board_block
    with flask_app.app_context():
        # a real open epic + a ready marker on the same board
        post_task('/rb/3', 'cX', 'A genuine open epic to work')
        pr.post_ready_marker('/rb/3', 'cA', files=['lib/z.py'],
                             test_paths=['t.py'], at_ref='HEAD',
                             gate_result={'green': True, 'selfConsistent': True})
        block = render_board_block('/rb/3', current_conv_id='cREADER')
    # the epic shows in Open; the marker's file must NOT appear as an Open item
    assert 'A genuine open epic to work' in block
    open_idx = block.find('Open (unclaimed')
    landing_idx = block.find('Landing')
    assert open_idx != -1 and landing_idx != -1
    # 'lib/z.py' (the ready marker) must appear only AFTER the Open section
    # header — i.e. within Landing, never as an Open bullet.
    z_idx = block.find('lib/z.py')
    assert z_idx > landing_idx, 'the ready marker must render in Landing, not Open'


def test_board_block_no_landing_section_when_no_markers(flask_app):
    """No ready markers → no Landing section (no prompt weight for an empty
    queue), but a normal board still renders."""
    import lib.conversations.project_board as pb
    with flask_app.app_context():
        pb.post_task('/rb/4', 'cX', 'Some open epic')
        block = pb.render_board_block('/rb/4', current_conv_id='cREADER')
    assert 'Some open epic' in block
    assert 'Landing' not in block


def main():
    import pytest as _pt
    _pt.main([__file__, '-v'])


if __name__ == '__main__':
    main()
