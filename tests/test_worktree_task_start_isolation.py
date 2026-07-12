"""End-to-end acceptance for worktree isolation's TASK-START seam.

The load-bearing gap the owner flagged: the tool-EXECUTION seam
(handlers/project.py) was scoped, but the task's top-level ``projectPath`` was
never resolved to the conv's worktree at spawn. This suite pins the fix in
``lib.project_mod.scanner.ensure_project_state`` — under
``TOFU_WORKTREE_ISOLATION=on`` a conv's PRIMARY root is registered as its OWN
git worktree, so the system-prompt file tree AND every path resolution see the
isolated checkout from the first turn.

It then proves the DEADLOCK-KILLING loop end-to-end at the seam level: two
sibling conversations both attach the SAME project, each edits the SAME file in
its OWN worktree, and both land via the ``project_commit`` → ``execute_land_tool``
CAS-merge path — asserting the integration branch holds the first lander and the
genuine conflict on the second is REPORTED, not clobbered.

Real git 2.11 plumbing against a throwaway repo (mirrors
tests/test_project_worktree.py). No mocks of the merge/gate.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from lib.conversations import project_worktree as pw
from lib.project_mod import config as pconfig
from lib.project_mod import scanner

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────
#  Helpers (mirror test_project_worktree.py)
# ─────────────────────────────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _seed(tmp_path):
    repo = str(tmp_path)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'wt@test')
    _git(repo, 'config', 'user.name', 'wt')
    (tmp_path / 'base.txt').write_text('base\n')
    (tmp_path / 'shared.py').write_text('VALUE = 0\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'init')
    return repo


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv('TOFU_WORKTREE_ISOLATION', 'on')
    pw.reset_for_test()
    yield
    pw.reset_for_test()


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv('TOFU_WORKTREE_ISOLATION', raising=False)
    pw.reset_for_test()
    yield
    pw.reset_for_test()


def _clear_conv(conv_id):
    try:
        pconfig.clear_conv_state(conv_id)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
#  OFF — byte-identical: the conv primary is the shared checkout, no worktree
# ─────────────────────────────────────────────────────────────────────────

def test_off_registers_primary_checkout_not_worktree(tmp_path, off):
    repo = _seed(tmp_path)
    conv = 'conv_off_1'
    _clear_conv(conv)
    assert scanner.ensure_project_state(repo, conv_id=conv) is True
    roots = pconfig.get_conv_roots(conv)
    paths = {rs['path'] for rs in roots.values()}
    # Primary is the shared checkout; NO worktree dir created (single-box).
    assert os.path.abspath(repo) in paths, paths
    assert not os.path.exists(pw.worktrees_root(repo))
    _clear_conv(conv)


# ─────────────────────────────────────────────────────────────────────────
#  ON — the task-start seam: conv primary IS the conv's own worktree
# ─────────────────────────────────────────────────────────────────────────

def test_on_registers_conv_worktree_as_primary(tmp_path, on):
    repo = _seed(tmp_path)
    conv = 'conv_on_1'
    _clear_conv(conv)
    assert scanner.ensure_project_state(repo, conv_id=conv) is True
    roots = pconfig.get_conv_roots(conv)
    paths = {rs['path'] for rs in roots.values()}
    wt = pw.worktree_path(repo, conv)
    # The registered primary is the conv's worktree, NOT the shared checkout.
    assert os.path.abspath(wt) in {os.path.abspath(p) for p in paths}, (paths, wt)
    assert os.path.abspath(repo) not in {os.path.abspath(p) for p in paths}, paths
    assert os.path.isdir(wt)
    _clear_conv(conv)


def test_on_two_convs_get_distinct_worktrees(tmp_path, on):
    repo = _seed(tmp_path)
    _clear_conv('cA'); _clear_conv('cB')
    scanner.ensure_project_state(repo, conv_id='cA')
    scanner.ensure_project_state(repo, conv_id='cB')
    a = {os.path.abspath(rs['path']) for rs in pconfig.get_conv_roots('cA').values()}
    b = {os.path.abspath(rs['path']) for rs in pconfig.get_conv_roots('cB').values()}
    assert a and b and a.isdisjoint(b), (a, b)
    _clear_conv('cA'); _clear_conv('cB')


# ─────────────────────────────────────────────────────────────────────────
#  THE DEADLOCK-KILLING LOOP — two convs, same file, both land via CAS-merge
# ─────────────────────────────────────────────────────────────────────────

def test_two_convs_same_file_land_loop(tmp_path, on):
    """Full acceptance: two sibling convs attach the SAME project; the
    task-start seam gives each its own worktree; each edits the SAME file; both
    land via execute_land_tool. First lands; the second's genuine conflict is
    REPORTED (held), not clobbered; the integration branch holds the winner."""
    repo = _seed(tmp_path)
    _clear_conv('cA'); _clear_conv('cB')

    # Task start for both convs (the seam under test).
    scanner.ensure_project_state(repo, conv_id='cA')
    scanner.ensure_project_state(repo, conv_id='cB')

    wt_a = pw.worktree_path(repo, 'cA')
    wt_b = pw.worktree_path(repo, 'cB')
    # Each conv's registered primary must be its OWN worktree — confirming the
    # edits below land in isolated checkouts, not the shared tree.
    assert os.path.isdir(wt_a) and os.path.isdir(wt_b) and wt_a != wt_b

    # Both edit the SAME file, each in its own worktree (isolated).
    with open(os.path.join(wt_a, 'shared.py'), 'w') as f:
        f.write('VALUE = 111\n')
    with open(os.path.join(wt_b, 'shared.py'), 'w') as f:
        f.write('VALUE = 222\n')
    # A's edit never leaks into B's worktree or the primary checkout.
    assert open(os.path.join(wt_b, 'shared.py')).read() == 'VALUE = 222\n'
    assert open(os.path.join(repo, 'shared.py')).read() == 'VALUE = 0\n'

    # Both land via the on-mode land verb (project_commit → execute_land_tool).
    msg_a = pw.execute_land_tool({'message': 'A sets 111'},
                                 current_conv_id='cA', project_path=repo)
    assert 'Landed into' in msg_a, msg_a

    msg_b = pw.execute_land_tool({'message': 'B sets 222'},
                                 current_conv_id='cB', project_path=repo)
    # Genuine same-line conflict → reported, not force-landed.
    assert ('held' in msg_b.lower() or 'conflict' in msg_b.lower()), msg_b

    # Integration holds A's winner; B's change was NOT clobbered in.
    ib = pw.integration_branch()
    landed = _git(repo, 'show', f'{ib}:shared.py').stdout.strip()
    assert landed == 'VALUE = 111', landed
    _clear_conv('cA'); _clear_conv('cB')


def test_two_convs_distinct_files_both_land(tmp_path, on):
    """Non-conflicting counterpart: two convs edit DIFFERENT files; both land
    and the integration tree contains BOTH — the concurrency the deadlock was
    blocking now flows through without collision."""
    repo = _seed(tmp_path)
    _clear_conv('dA'); _clear_conv('dB')
    scanner.ensure_project_state(repo, conv_id='dA')
    scanner.ensure_project_state(repo, conv_id='dB')
    wt_a = pw.worktree_path(repo, 'dA')
    wt_b = pw.worktree_path(repo, 'dB')

    with open(os.path.join(wt_a, 'a_feature.py'), 'w') as f:
        f.write('A = 1\n')
    with open(os.path.join(wt_b, 'b_feature.py'), 'w') as f:
        f.write('B = 2\n')

    assert 'Landed into' in pw.execute_land_tool(
        {'message': 'A feature'}, current_conv_id='dA', project_path=repo)
    assert 'Landed into' in pw.execute_land_tool(
        {'message': 'B feature'}, current_conv_id='dB', project_path=repo)

    ib = pw.integration_branch()
    tree = _git(repo, 'ls-tree', '-r', '--name-only', ib).stdout
    assert 'a_feature.py' in tree and 'b_feature.py' in tree, tree
    _clear_conv('dA'); _clear_conv('dB')


# ─────────────────────────────────────────────────────────────────────────
#  HELD-LAND RECOVERY — the conflicted conv must be able to escape, not strand
# ─────────────────────────────────────────────────────────────────────────

def _setup_held_land(repo):
    """Drive two convs to the state where cB's land is HELD on a conflict.
    Returns cB's worktree path."""
    _clear_conv('rA'); _clear_conv('rB')
    scanner.ensure_project_state(repo, conv_id='rA')
    scanner.ensure_project_state(repo, conv_id='rB')
    wt_a = pw.worktree_path(repo, 'rA')
    wt_b = pw.worktree_path(repo, 'rB')
    with open(os.path.join(wt_a, 'shared.py'), 'w') as f:
        f.write('VALUE = 111\n')
    with open(os.path.join(wt_b, 'shared.py'), 'w') as f:
        f.write('VALUE = 222\n')
    assert 'Landed into' in pw.execute_land_tool(
        {'message': 'A'}, current_conv_id='rA', project_path=repo)
    held = pw.execute_land_tool({'message': 'B'}, current_conv_id='rB',
                                project_path=repo)
    assert 'held' in held.lower(), held
    return wt_b


def test_held_land_recovers_via_sync_then_reland(tmp_path, on):
    """The recovery loop CONVERGES: after a held land, project_sync pulls
    integration into the conv worktree (conflict markers), the conv resolves +
    re-lands, and the land now fast-forwards. Without this the conv strands."""
    repo = _seed(tmp_path)
    wt_b = _setup_held_land(repo)

    # 1. Sync → integration merged into cB's worktree, conflict markers present.
    sync_msg = pw.execute_sync_tool({}, current_conv_id='rB', project_path=repo)
    assert 'conflict' in sync_msg.lower() and 'shared.py' in sync_msg, sync_msg
    body = open(os.path.join(wt_b, 'shared.py')).read()
    assert '<<<<<<<' in body and '>>>>>>>' in body, body

    # 2. The conv resolves the markers with a normal edit (keep both intents).
    with open(os.path.join(wt_b, 'shared.py'), 'w') as f:
        f.write('VALUE = 111  # reconciled with integration\n')

    # 3. Re-land → now a fast-forward, CONVERGES.
    relanded = pw.execute_land_tool({'message': 'B reconciled'},
                                    current_conv_id='rB', project_path=repo)
    assert 'Landed into' in relanded, relanded
    ib = pw.integration_branch()
    final = _git(repo, 'show', f'{ib}:shared.py').stdout.strip()
    assert final == 'VALUE = 111  # reconciled with integration', final
    _clear_conv('rA'); _clear_conv('rB')


def test_NC_reland_without_sync_does_not_converge(tmp_path, on):
    """Load-bearing NC: WITHOUT project_sync, re-landing (even after editing the
    worktree) re-hits the same conflict — proving the sync recovery seam is
    what makes the loop converge, not an incidental pass."""
    repo = _seed(tmp_path)
    wt_b = _setup_held_land(repo)

    # Edit the worktree to "reconcile" but DO NOT sync — re-land must still hold.
    with open(os.path.join(wt_b, 'shared.py'), 'w') as f:
        f.write('VALUE = 111\n')
    again = pw.execute_land_tool({'message': 'B retry no-sync'},
                                 current_conv_id='rB', project_path=repo)
    assert 'held' in again.lower(), (
        'without sync the re-land should STILL be held (rebase re-hits the '
        f'original conflict); got: {again}')
    _clear_conv('rA'); _clear_conv('rB')


def test_sync_clean_when_no_conflict(tmp_path, on):
    """project_sync on a non-conflicting divergence merges cleanly (no markers),
    so the conv can immediately re-land."""
    repo = _seed(tmp_path)
    _clear_conv('sA'); _clear_conv('sB')
    scanner.ensure_project_state(repo, conv_id='sA')
    scanner.ensure_project_state(repo, conv_id='sB')
    # A lands a DIFFERENT file → sB is behind integration but not conflicting.
    with open(os.path.join(pw.worktree_path(repo, 'sA'), 'other.py'), 'w') as f:
        f.write('A = 1\n')
    assert 'Landed into' in pw.execute_land_tool(
        {'message': 'A other'}, current_conv_id='sA', project_path=repo)
    with open(os.path.join(pw.worktree_path(repo, 'sB'), 'mine.py'), 'w') as f:
        f.write('B = 2\n')
    msg = pw.execute_sync_tool({}, current_conv_id='sB', project_path=repo)
    assert 'cleanly' in msg.lower() or 'up to date' in msg.lower(), msg
    assert 'Landed into' in pw.execute_land_tool(
        {'message': 'B mine'}, current_conv_id='sB', project_path=repo)
    _clear_conv('sA'); _clear_conv('sB')


def main():
    import pytest as _pt
    _pt.main([__file__, '-v'])


if __name__ == '__main__':
    main()
