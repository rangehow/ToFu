"""Tests for lib.conversations.project_worktree — per-conversation git worktree
isolation lifecycle + the CAS-merge integration-ref land primitive.

These pin the design's load-bearing invariants
(docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md), the ones the owner caught on paper:

  * OFF by default — TOFU_WORKTREE_ISOLATION=inproc makes EVERY entry point a
    no-op, so a single-box install is byte-identical to today (§6).
  * A land does a REAL 3-way merge — CONTENT-verified: every distinct-file
    lander's content is present in the integration tree, never last-tree-wins
    (§5.1 / the discarded-content data-loss bug the owner caught).
  * The acceptance gate runs on the MERGE-RESULT tree, not just pre-merge:
    Scenario C — A drops foo(), B adds a call to it in another file — merges
    textually-clean but is semantically broken, so the merge-result gate must
    RED it and land must NOT publish the ref (§5).
  * A genuine same-file conflict is reported, not clobbered.
  * GC / release never deletes a branch with unmerged commits (never lose work).

All git runs against a real throwaway repo under tmp_path (mirrors
tests/test_project_acceptance_gate.py); worktree ops are the actual git 2.11
plumbing on the local fs.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from lib.conversations import project_worktree as pw

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _init_repo(path):
    _git(path, 'init', '-q')
    _git(path, 'config', 'user.email', 'wt@test')
    _git(path, 'config', 'user.name', 'wt')
    # A deterministic default branch name across git versions.
    return path


def _commit_all(path, msg):
    _git(path, 'add', '-A')
    _git(path, 'commit', '-q', '-m', msg)


def _seed(tmp_path):
    """A repo with a base commit + a tiny package so tests can import symbols."""
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'base.txt').write_text('base\n')
    (tmp_path / 'lib_x.py').write_text('def foo():\n    return 1\n')
    (tmp_path / 'test_x.py').write_text(
        'import lib_x\n\n\ndef test_foo():\n    assert lib_x.foo() == 1\n')
    _commit_all(repo, 'init')
    return repo


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv('TOFU_WORKTREE_ISOLATION', 'on')
    pw.reset_for_test()
    yield
    pw.reset_for_test()


# ─────────────────────────────────────────────────────────────────────────
#  Rollout seam — OFF by default
# ─────────────────────────────────────────────────────────────────────────

def test_isolation_off_by_default(monkeypatch):
    monkeypatch.delenv('TOFU_WORKTREE_ISOLATION', raising=False)
    assert pw.isolation_mode() == 'inproc'
    assert pw.is_isolation_enabled() is False


def test_unknown_mode_falls_back_to_inproc(monkeypatch):
    monkeypatch.setenv('TOFU_WORKTREE_ISOLATION', 'garbage')
    assert pw.isolation_mode() == 'inproc'


def test_all_entrypoints_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_WORKTREE_ISOLATION', raising=False)
    repo = _seed(tmp_path)
    assert pw.ensure_worktree(repo, 'c1').get('disabled') is True
    assert pw.sync_worktree(repo, 'c1').get('disabled') is True
    assert pw.land_worktree(repo, 'c1').get('disabled') is True
    assert pw.release_worktree(repo, 'c1').get('disabled') is True
    # No artifacts created — byte-identical-to-today guarantee.
    assert not os.path.exists(pw.worktrees_root(repo))


# ─────────────────────────────────────────────────────────────────────────
#  Setup + lifecycle
# ─────────────────────────────────────────────────────────────────────────

def test_ensure_integration_setup_seeds_branch(tmp_path, on):
    repo = _seed(tmp_path)
    res = pw.ensure_integration_setup(repo)
    assert res['ok'] is True
    assert res['created_branch'] is True
    # reflogs disabled on the shared repo (constraint 2)
    cfg = _git(repo, 'config', '--get', 'core.logallrefupdates').stdout.strip()
    assert cfg == 'false'
    # idempotent — second call doesn't recreate
    assert pw.ensure_integration_setup(repo)['created_branch'] is False


def test_ensure_worktree_creates_branch_and_dir(tmp_path, on):
    repo = _seed(tmp_path)
    res = pw.ensure_worktree(repo, 'conv_alpha')
    assert res['ok'] is True and res['created'] is True
    assert os.path.isdir(res['path'])
    assert res['branch'] == 'tofu/conv/conv_alpha'
    # branch exists
    branches = _git(repo, 'branch', '--list', 'tofu/conv/conv_alpha').stdout
    assert 'tofu/conv/conv_alpha' in branches
    # idempotent reuse
    res2 = pw.ensure_worktree(repo, 'conv_alpha')
    assert res2['created'] is False and res2['path'] == res['path']


def test_worktree_edits_are_isolated(tmp_path, on):
    repo = _seed(tmp_path)
    a = pw.ensure_worktree(repo, 'A')['path']
    b = pw.ensure_worktree(repo, 'B')['path']
    (open(os.path.join(a, 'a_only.txt'), 'w')).write('A\n')
    _git(a, 'add', '-A'); _git(a, 'commit', '-q', '-m', 'A work')
    # B's tree never sees A's uncommitted/committed file
    assert not os.path.exists(os.path.join(b, 'a_only.txt'))
    # primary checkout untouched too
    assert not os.path.exists(os.path.join(repo, 'a_only.txt'))


# ─────────────────────────────────────────────────────────────────────────
#  Land — CAS 3-way merge, CONTENT-verified
# ─────────────────────────────────────────────────────────────────────────

def test_land_fast_forward(tmp_path, on):
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wt, 'f_a.txt'), 'w')).write('a\n')
    _git(wt, 'add', '-A'); _git(wt, 'commit', '-q', '-m', 'add f_a')
    res = pw.land_worktree(repo, 'A', test_paths=[])
    assert res['ok'] is True and res['sha']
    # integration now has the file
    ib = pw.integration_branch()
    tree = _git(repo, 'ls-tree', '-r', '--name-only', ib).stdout
    assert 'f_a.txt' in tree and 'base.txt' in tree


def test_two_distinct_file_landers_both_preserved(tmp_path, on):
    """CONTENT-verified merge: two divergent landers (different files) BOTH end
    up in the integration tree — never last-tree-wins (the discarded-content
    bug). The second lander diverges from integration so it exercises the REAL
    3-way merge path, not fast-forward."""
    repo = _seed(tmp_path)
    wa = pw.ensure_worktree(repo, 'A')['path']
    wb = pw.ensure_worktree(repo, 'B')['path']
    # Both branched off the SAME integration HEAD → B will diverge after A lands.
    (open(os.path.join(wa, 'f_a.txt'), 'w')).write('a\n')
    _git(wa, 'add', '-A'); _git(wa, 'commit', '-q', '-m', 'A: f_a')
    (open(os.path.join(wb, 'f_b.txt'), 'w')).write('b\n')
    _git(wb, 'add', '-A'); _git(wb, 'commit', '-q', '-m', 'B: f_b')

    assert pw.land_worktree(repo, 'A', test_paths=[])['ok'] is True
    resb = pw.land_worktree(repo, 'B', test_paths=[])
    assert resb['ok'] is True

    ib = pw.integration_branch()
    tree = _git(repo, 'ls-tree', '-r', '--name-only', ib).stdout
    # BOTH landers' content + base present — the content-verification the owner
    # demanded (present, not merely reachable).
    assert 'f_a.txt' in tree, tree
    assert 'f_b.txt' in tree, tree
    assert 'base.txt' in tree, tree


def test_same_file_conflict_is_reported_not_clobbered(tmp_path, on):
    """Two branches editing the SAME line → one lands, the other REPORTS a
    conflict (not silently overwritten). Integration holds exactly the winner."""
    repo = _seed(tmp_path)
    wa = pw.ensure_worktree(repo, 'A')['path']
    wb = pw.ensure_worktree(repo, 'B')['path']
    (open(os.path.join(wa, 'base.txt'), 'w')).write('A-change\n')
    _git(wa, 'add', '-A'); _git(wa, 'commit', '-q', '-m', 'A edits base')
    (open(os.path.join(wb, 'base.txt'), 'w')).write('B-change\n')
    _git(wb, 'add', '-A'); _git(wb, 'commit', '-q', '-m', 'B edits base')

    assert pw.land_worktree(repo, 'A', test_paths=[])['ok'] is True
    resb = pw.land_worktree(repo, 'B', test_paths=[])
    assert resb['ok'] is False
    assert resb.get('conflict') is True
    # integration unchanged content-wise beyond A's winner
    ib = pw.integration_branch()
    content = _git(repo, 'show', f'{ib}:base.txt').stdout
    assert content.strip() == 'A-change'


# ─────────────────────────────────────────────────────────────────────────
#  The load-bearing gate: MERGE-RESULT correctness (Scenario C)
# ─────────────────────────────────────────────────────────────────────────

def test_scenario_c_broken_merge_never_published(tmp_path, on):
    """SAFETY property (owner catch §5): A drops foo(); B adds a call to foo()
    in ANOTHER file. Each branch is green ALONE and the diffs don't overlap, so
    a naive textual merge succeeds — but the combined tree is semantically
    broken. Land MUST refuse B and MUST NOT publish a broken integration.

    Which gate catches it is an implementation detail: because sync rebases B
    onto A's landed integration FIRST, B's PRE-FLIGHT gate runs on the combined
    (broken) tree and reds it; the merge-result gate (tested directly in
    test_merge_and_gate_reds_broken_merge) is the belt-and-suspenders for the
    CAS-race window where integration advances after sync. Either way the ref
    is protected — that is the invariant this test pins."""
    repo = _seed(tmp_path)
    ib = pw.integration_branch()
    wa = pw.ensure_worktree(repo, 'A')['path']
    wb = pw.ensure_worktree(repo, 'B')['path']
    integ_before = _git(repo, 'rev-parse', ib).stdout.strip()

    # A: remove foo from lib_x (and its test) — green alone (nothing calls foo on A).
    (open(os.path.join(wa, 'lib_x.py'), 'w')).write('def bar():\n    return 2\n')
    (open(os.path.join(wa, 'test_x.py'), 'w')).write(
        'import lib_x\n\n\ndef test_bar():\n    assert lib_x.bar() == 2\n')
    _git(wa, 'add', '-A'); _git(wa, 'commit', '-q', '-m', 'A: drop foo')

    # B: add a NEW file that calls foo — green alone (foo still exists on B).
    (open(os.path.join(wb, 'consumer.py'), 'w')).write(
        'import lib_x\n\n\ndef use():\n    return lib_x.foo()\n')
    (open(os.path.join(wb, 'test_consumer.py'), 'w')).write(
        'import consumer\n\n\ndef test_use():\n    assert consumer.use() == 1\n')
    _git(wb, 'add', '-A'); _git(wb, 'commit', '-q', '-m', 'B: call foo in new file')

    resa = pw.land_worktree(repo, 'A', test_paths=['test_x.py'])
    assert resa['ok'] is True, resa

    resb = pw.land_worktree(repo, 'B', test_paths=['test_consumer.py'])
    assert resb['ok'] is False, resb
    # caught by either gate — both are correct refusals
    assert resb.get('merge_result_red') or resb.get('preflight_red'), resb

    # THE ref holds A's landing, never a broken merge; consumer.py not published.
    integ_after = _git(repo, 'rev-parse', ib).stdout.strip()
    assert integ_after != integ_before  # A did land
    tree = _git(repo, 'ls-tree', '-r', '--name-only', ib).stdout
    assert 'consumer.py' not in tree, tree


def test_merge_and_gate_reds_broken_merge(tmp_path, on):
    """Directly exercise the MERGE-RESULT gate (the CAS-race path): merge a
    branch that calls foo() into an OLD integration where foo() was dropped, in
    two DIFFERENT files (textually clean merge, semantically broken). The gate
    must return merge_result_red and produce NO published sha."""
    repo = _seed(tmp_path)
    # Build an integration tip where foo is dropped (simulating A already landed).
    wa = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wa, 'lib_x.py'), 'w')).write('def bar():\n    return 2\n')
    (open(os.path.join(wa, 'test_x.py'), 'w')).write('def test_bar():\n    assert True\n')
    _git(wa, 'add', '-A'); _git(wa, 'commit', '-q', '-m', 'A: drop foo')
    assert pw.land_worktree(repo, 'A', test_paths=[])['ok'] is True
    ib = pw.integration_branch()
    old = _git(repo, 'rev-parse', ib).stdout.strip()

    # B branch (off original base, foo present) adds a consumer calling foo.
    wb = pw.ensure_worktree(repo, 'B')['path']
    (open(os.path.join(wb, 'consumer.py'), 'w')).write(
        'import lib_x\n\n\ndef use():\n    return lib_x.foo()\n')
    (open(os.path.join(wb, 'test_consumer.py'), 'w')).write(
        'import consumer\n\n\ndef test_use():\n    assert consumer.use() == 1\n')
    _git(wb, 'add', '-A'); _git(wb, 'commit', '-q', '-m', 'B: consumer')

    res = pw._merge_and_gate(repo, old, pw.conv_branch('B'),
                             ['test_consumer.py'], '', None)
    assert res.get('merge_result_red') is True, res
    assert not res.get('sha'), res
    # ref untouched by a direct _merge_and_gate call (it never CASes)
    assert _git(repo, 'rev-parse', ib).stdout.strip() == old


def test_preflight_red_blocks_before_merge(tmp_path, on):
    """A branch whose OWN declared tests fail is rejected pre-merge (fail fast),
    never reaching the merge/CAS section."""
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wt, 'test_x.py'), 'w')).write(
        'def test_boom():\n    assert False\n')
    _git(wt, 'add', '-A'); _git(wt, 'commit', '-q', '-m', 'A: red test')
    res = pw.land_worktree(repo, 'A', test_paths=['test_x.py'])
    assert res['ok'] is False
    assert res.get('preflight_red') is True


# ─────────────────────────────────────────────────────────────────────────
#  GC / release — never lose work
# ─────────────────────────────────────────────────────────────────────────

def test_release_deletes_merged_branch(tmp_path, on):
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wt, 'f_a.txt'), 'w')).write('a\n')
    _git(wt, 'add', '-A'); _git(wt, 'commit', '-q', '-m', 'A work')
    assert pw.land_worktree(repo, 'A', test_paths=[])['ok'] is True
    res = pw.release_worktree(repo, 'A')
    assert res['ok'] is True
    assert res['pruned'] is True
    assert res['branch_deleted'] is True
    assert res['kept_unmerged'] is False
    assert 'tofu/conv/A' not in _git(repo, 'branch', '--list', 'tofu/conv/A').stdout


def test_release_keeps_unmerged_branch(tmp_path, on):
    """A branch with commits NOT in integration is kept on release (never lose
    work) — only the checkout is pruned."""
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wt, 'unlanded.txt'), 'w')).write('x\n')
    _git(wt, 'add', '-A'); _git(wt, 'commit', '-q', '-m', 'A: never landed')
    res = pw.release_worktree(repo, 'A')  # NOT landed first
    assert res['ok'] is True
    assert res['pruned'] is True
    assert res['branch_deleted'] is False
    assert res['kept_unmerged'] is True
    # branch still present with its commit
    assert 'tofu/conv/A' in _git(repo, 'branch', '--list', 'tofu/conv/A').stdout


def test_gc_reclaims_expired_lease(tmp_path, on):
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wt, 'f_a.txt'), 'w')).write('a\n')
    _git(wt, 'add', '-A'); _git(wt, 'commit', '-q', '-m', 'A work')
    pw.land_worktree(repo, 'A', test_paths=[])  # merged → deletable
    # Force the lease into the past.
    from lib.timeutil import now_ms
    res = pw.gc_worktrees(repo, now=now_ms() + pw.DEFAULT_LEASE_TTL_MS + 1_000)
    assert res['ok'] is True
    assert 'A' in res['reclaimed']
    assert not os.path.isdir(wt)


def test_gc_keeps_unmerged_on_expiry(tmp_path, on):
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    (open(os.path.join(wt, 'unlanded.txt'), 'w')).write('x\n')
    _git(wt, 'add', '-A'); _git(wt, 'commit', '-q', '-m', 'A unlanded')
    from lib.timeutil import now_ms
    res = pw.gc_worktrees(repo, now=now_ms() + pw.DEFAULT_LEASE_TTL_MS + 1_000)
    assert res['ok'] is True
    assert 'A' in res['kept_unmerged']
    assert 'A' not in res['reclaimed']


# ─────────────────────────────────────────────────────────────────────────
#  Land verb — commit_worktree + execute_land_tool (step 4 wiring)
# ─────────────────────────────────────────────────────────────────────────

def test_commit_worktree_commits_all_edits(tmp_path, on):
    """Inside an isolated worktree git add -A is safe; commit_worktree turns the
    working-tree edits into a branch commit (no false-clean gate needed)."""
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    with open(os.path.join(wt, 'new.py'), 'w') as f:
        f.write('x = 1\n')
    res = pw.commit_worktree(repo, 'A', 'add new.py')
    assert res['ok'] is True and res.get('committed') is True and res.get('sha')
    # the file is committed on the conv branch, worktree now clean
    assert _git(wt, 'status', '--porcelain').stdout.strip() == ''


def test_commit_worktree_nothing_to_commit(tmp_path, on):
    repo = _seed(tmp_path)
    pw.ensure_worktree(repo, 'A')
    res = pw.commit_worktree(repo, 'A', 'noop')
    assert res['ok'] is True and res.get('nothing') is True


def test_execute_land_tool_commits_then_lands(tmp_path, on):
    """The on-mode land verb: edit in the worktree, then execute_land_tool
    commits + CAS-merges into integration. The change reaches the integration
    tree without any manual git."""
    repo = _seed(tmp_path)
    wt = pw.ensure_worktree(repo, 'A')['path']
    with open(os.path.join(wt, 'landed.py'), 'w') as f:
        f.write('y = 2\n')
    msg = pw.execute_land_tool({'message': 'land landed.py'},
                               current_conv_id='A', project_path=repo)
    assert 'Landed into' in msg, msg
    ib = pw.integration_branch()
    tree = _git(repo, 'ls-tree', '-r', '--name-only', ib).stdout
    assert 'landed.py' in tree, tree


def test_execute_land_tool_requires_message(tmp_path, on):
    repo = _seed(tmp_path)
    pw.ensure_worktree(repo, 'A')
    msg = pw.execute_land_tool({}, current_conv_id='A', project_path=repo)
    assert 'message' in msg.lower()


def test_execute_land_tool_conflict_reported_not_forced(tmp_path, on):
    """Two convs edit the same line; the second land is REPORTED as held, the
    integration ref is not force-moved to the loser."""
    repo = _seed(tmp_path)
    wa = pw.ensure_worktree(repo, 'A')['path']
    wb = pw.ensure_worktree(repo, 'B')['path']
    with open(os.path.join(wa, 'base.txt'), 'w') as f:
        f.write('A-line\n')
    with open(os.path.join(wb, 'base.txt'), 'w') as f:
        f.write('B-line\n')
    assert 'Landed into' in pw.execute_land_tool(
        {'message': 'A'}, current_conv_id='A', project_path=repo)
    msg_b = pw.execute_land_tool({'message': 'B'}, current_conv_id='B', project_path=repo)
    assert 'held' in msg_b.lower() or 'conflict' in msg_b.lower(), msg_b
    ib = pw.integration_branch()
    assert _git(repo, 'show', f'{ib}:base.txt').stdout.strip() == 'A-line'


def main():
    import pytest as _pt
    _pt.main([__file__, '-v'])


if __name__ == '__main__':
    main()
