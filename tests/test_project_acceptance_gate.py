"""Tests for lib.conversations.project_acceptance — the codified fresh-worktree
acceptance gate and its load-bearing orphaned-caller detector.

The detector exists because the byte-identity staging proofs in project_commit
(zero-signature grep + --stat) prove "no foreign ADDITIONS entered my commit"
but CANNOT prove "no symbol my slice REMOVED left a caller orphaned at HEAD" —
exactly the split-brain the 379240e engine-core commit hit (it removed
``defer_task`` from project_board.py while two non-slice files still imported
it). These tests pin that the detector catches that class of bug.

The detector is deliberately worktree-free: after a commit, every NON-slice
file stays at ``at_ref``, so grepping the ``at_ref`` tree (excluding the slice
paths) for any module-level symbol the slice removed predicts precisely which
callers the landed HEAD would orphan.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from lib.conversations import project_acceptance as pa


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _init_repo(path):
    _git(path, 'init', '-q')
    _git(path, 'config', 'user.email', 'gate@test')
    _git(path, 'config', 'user.name', 'gate')
    return path


def _commit_all(path, msg):
    _git(path, 'add', '-A')
    _git(path, 'commit', '-q', '-m', msg)


# ─────────────────────────────────────────────────────────────────────────
#  detect_orphaned_callers — the RED-first core
# ─────────────────────────────────────────────────────────────────────────

def test_detects_orphan_when_removed_symbol_still_called(tmp_path):
    """The 379240e reproduction: a slice removes a module-level symbol, but a
    NON-slice file at HEAD still references it → split-brain must be flagged."""
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'producer.py').write_text(
        'def helper():\n    return 1\n\n\ndef keep():\n    return 2\n')
    (tmp_path / 'caller.py').write_text(
        'from producer import helper\n\n\ndef use():\n    return helper()\n')
    _commit_all(repo, 'init')

    # Working tree: remove `helper` from producer (orphaning caller); keep `keep`.
    (tmp_path / 'producer.py').write_text('def keep():\n    return 2\n')

    res = pa.detect_orphaned_callers(repo, ['producer.py'], at_ref='HEAD')
    assert res['ok'] is True
    assert res['selfConsistent'] is False
    assert 'helper' in res['removedSymbols']
    syms = {o['symbol'] for o in res['orphans']}
    assert 'helper' in syms
    refs = {r for o in res['orphans'] for r in o['referencedBy']}
    assert any('caller.py' in r for r in refs)


def test_clean_when_caller_removed_in_same_slice(tmp_path):
    """If the slice removes the symbol AND its caller together (the amended
    bd3cbfe fix), the landed HEAD is self-consistent → no orphan."""
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'producer.py').write_text(
        'def helper():\n    return 1\n\n\ndef keep():\n    return 2\n')
    (tmp_path / 'caller.py').write_text(
        'from producer import helper\n\n\ndef use():\n    return helper()\n')
    _commit_all(repo, 'init')

    (tmp_path / 'producer.py').write_text('def keep():\n    return 2\n')
    (tmp_path / 'caller.py').write_text('def use():\n    return 2\n')

    res = pa.detect_orphaned_callers(
        repo, ['producer.py', 'caller.py'], at_ref='HEAD')
    assert res['ok'] is True
    assert res['selfConsistent'] is True
    assert res['orphans'] == []


def test_additive_new_file_has_no_removed_symbols(tmp_path):
    """A slice that only ADDS a new file (absent at at_ref) removes nothing →
    trivially self-consistent (mirrors the 4 new test files in bd3cbfe)."""
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'producer.py').write_text('def helper():\n    return 1\n')
    _commit_all(repo, 'init')
    (tmp_path / 'brand_new.py').write_text('def added():\n    return 9\n')

    res = pa.detect_orphaned_callers(repo, ['brand_new.py'], at_ref='HEAD')
    assert res['ok'] is True
    assert res['selfConsistent'] is True
    assert res['removedSymbols'] == []


def test_self_contained_removal_is_clean(tmp_path):
    """Removing a symbol that NOTHING else references is self-consistent — and
    this proves the slice-exclude works (producer's own removed def must not be
    matched against producer itself)."""
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'producer.py').write_text(
        'def helper():\n    return 1\n\n\ndef keep():\n    return 2\n')
    (tmp_path / 'other.py').write_text('def unrelated():\n    return 3\n')
    _commit_all(repo, 'init')
    (tmp_path / 'producer.py').write_text('def keep():\n    return 2\n')

    res = pa.detect_orphaned_callers(repo, ['producer.py'], at_ref='HEAD')
    assert res['ok'] is True
    assert res['selfConsistent'] is True


def test_deleted_slice_file_orphans_all_importers(tmp_path):
    """Deleting a whole module in the slice orphans every importer of any of
    its symbols."""
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'producer.py').write_text('def helper():\n    return 1\n')
    (tmp_path / 'caller.py').write_text(
        'from producer import helper\n\n\ndef use():\n    return helper()\n')
    _commit_all(repo, 'init')
    (tmp_path / 'producer.py').unlink()

    res = pa.detect_orphaned_callers(repo, ['producer.py'], at_ref='HEAD')
    assert res['ok'] is True
    assert res['selfConsistent'] is False
    assert 'helper' in res['removedSymbols']


# ─────────────────────────────────────────────────────────────────────────
#  run_acceptance_gate — integration (worktree + overlay + tests + scan)
# ─────────────────────────────────────────────────────────────────────────

def _write_smoke_repo(tmp_path):
    repo = _init_repo(str(tmp_path))
    (tmp_path / 'producer.py').write_text(
        'def helper():\n    return 1\n\n\ndef keep():\n    return 2\n')
    (tmp_path / 'caller.py').write_text(
        'from producer import helper\n\n\ndef use():\n    return helper()\n')
    (tmp_path / 'test_smoke.py').write_text(
        'def test_ok():\n    assert True\n')
    _commit_all(repo, 'init')
    return repo


def test_gate_green_when_tests_pass_and_consistent(tmp_path):
    """A slice that edits producer WITHOUT removing referenced symbols and whose
    tests pass → gate green + self-consistent + ok."""
    repo = _write_smoke_repo(tmp_path)
    # additive edit: add a new function, keep helper
    (tmp_path / 'producer.py').write_text(
        'def helper():\n    return 1\n\n\ndef keep():\n    return 2\n\n\n'
        'def extra():\n    return 3\n')
    res = pa.run_acceptance_gate(
        repo, files=['producer.py'], test_paths=['test_smoke.py'])
    assert res['ok'] is True
    assert res['green'] is True
    assert res['selfConsistent'] is True


def test_gate_fails_on_orphan_even_when_tests_pass(tmp_path):
    """THE point: tests can be green while HEAD is split-brained. The gate must
    return ok=False when the orphan scan fails, regardless of the test result."""
    repo = _write_smoke_repo(tmp_path)
    # remove helper (orphans caller.py) but test_smoke still passes
    (tmp_path / 'producer.py').write_text('def keep():\n    return 2\n')
    res = pa.run_acceptance_gate(
        repo, files=['producer.py'], test_paths=['test_smoke.py'])
    assert res['green'] is True          # tests pass
    assert res['selfConsistent'] is False
    assert res['ok'] is False            # ok gated on BOTH
    syms = {o['symbol'] for o in res['orphans']}
    assert 'helper' in syms


def test_gate_red_when_tests_fail(tmp_path):
    """A failing declared test → not green → not ok (even if consistent)."""
    repo = _write_smoke_repo(tmp_path)
    (tmp_path / 'test_smoke.py').write_text(
        'def test_ok():\n    assert False\n')
    # commit the failing test so it exists at HEAD; slice re-applies wt version
    _commit_all(repo, 'failing test')
    res = pa.run_acceptance_gate(
        repo, files=['producer.py'], test_paths=['test_smoke.py'])
    assert res['green'] is False
    assert res['ok'] is False


def main():
    import pytest as _pt
    _pt.main([__file__, '-v'])


if __name__ == '__main__':
    main()
