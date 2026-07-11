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


def main():
    import pytest as _pt
    _pt.main([__file__, '-v'])


if __name__ == '__main__':
    main()
