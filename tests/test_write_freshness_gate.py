"""Tests for the write-freshness gate (shared-HEAD overwrite guard).

``lib/write_freshness.py`` (token store) +
``lib/tasks_pkg/handlers/_write_freshness_gate.py`` (task-aware check).

The canonical clobber: conversation A reads a file at T0, thinks for many
rounds, and writes at T2 — discarding sibling B's committed T1 change. The
gate refuses A's write because A's recorded fingerprint no longer matches
the disk, and tells A to re-read. Fail-open everywhere else: no token →
allow; file vanished → allow; check error → allow.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    """Isolate the process-global token store + force the gate on."""
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    from lib import write_freshness
    write_freshness._reset_for_tests()
    yield
    write_freshness._reset_for_tests()


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    target = proj / 'a.py'
    target.write_text('def foo():\n    return 1\n')
    return {'project_path': str(proj), 'target_rel': 'a.py',
            'target_abs': str(target)}


def _make_task(conv_id='convA', task_id='t1'):
    return {'id': task_id, 'convId': conv_id, 'messages': [], 'toolRounds': []}


def _external_change(abs_path, extra='# sibling was here\n'):
    """Simulate a sibling/process writing the file OUTSIDE this conv's tools."""
    with open(abs_path, 'a', encoding='utf-8') as f:
        f.write(extra)


@pytest.mark.unit
def test_unchanged_after_record_is_allowed(workspace):
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    task = _make_task()
    write_freshness.record('convA', workspace['target_abs'])
    err = check_write_freshness(
        task, 'write_file',
        {'path': workspace['target_rel'], 'content': 'x = 1\n'},
        workspace['project_path'])
    assert err is None


@pytest.mark.unit
def test_the_clobber_scenario_is_refused(workspace):
    """Money test: A reads (token), B writes through the real tool (its own
    token), A's stale write_file is refused and names the file."""
    from lib.project_mod.write_tools import tool_write_file
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    # A reads the file (handler would record exactly this).
    write_freshness.record('convA', workspace['target_abs'])
    # B writes through the REAL write path.
    r = tool_write_file(workspace['project_path'], workspace['target_rel'],
                        'def foo():\n    return 2  # B\n', conv_id='convB')
    assert r['ok'], r
    # A now tries to write from its stale memory.
    err = check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'def foo():\n    return 3  # A\n'},
        workspace['project_path'])
    assert err is not None
    assert 'changed on disk' in err
    assert workspace['target_rel'] in err
    assert 'read_files' in err


@pytest.mark.unit
def test_own_write_refreshes_own_token(workspace):
    """After A itself writes, A's subsequent write is NOT stale."""
    from lib.project_mod.write_tools import tool_write_file
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    write_freshness.record('convA', workspace['target_abs'])
    r = tool_write_file(workspace['project_path'], workspace['target_rel'],
                        'def foo():\n    return 2\n', conv_id='convA')
    assert r['ok'], r
    err = check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'def foo():\n    return 3\n'},
        workspace['project_path'])
    assert err is None


@pytest.mark.unit
def test_no_token_allows_blind_write(workspace):
    """Fail-open: a conv that never read/wrote the file may still write_file
    (the read-before-edit gate owns the must-read-first axis, not us)."""
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    err = check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'x\n'},
        workspace['project_path'])
    assert err is None


@pytest.mark.unit
def test_tokens_are_per_conversation(workspace):
    """A's token does not make B stale: B never recorded, B may write."""
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    write_freshness.record('convA', workspace['target_abs'])
    _external_change(workspace['target_abs'])
    err = check_write_freshness(
        _make_task(conv_id='convB'), 'write_file',
        {'path': workspace['target_rel'], 'content': 'x\n'},
        workspace['project_path'])
    assert err is None


@pytest.mark.unit
def test_vanished_file_allows_creation_but_reborn_is_stale(workspace):
    """Deliberate semantics: a CURRENTLY-vanished file → allow (creation;
    nothing to clobber). But the token is NOT dropped on that observation —
    if the file comes back with content this conv never saw, its knowledge
    is stale and the write is refused."""
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    write_freshness.record('convA', workspace['target_abs'])
    os.unlink(workspace['target_abs'])
    err = check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'x = 1\n'},
        workspace['project_path'])
    assert err is None
    # Reborn with unknown content → stale → refused (re-read first).
    with open(workspace['target_abs'], 'w') as f:
        f.write('reborn — sibling content\n')
    err = check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'y\n'},
        workspace['project_path'])
    assert err is not None
    assert 'changed on disk' in err


@pytest.mark.unit
def test_mtime_only_touch_is_stale(workspace):
    """Fingerprint semantics pin: mtime_ns alone moving (same content/size)
    still reads as stale — the safe direction for shared-tree writes."""
    from lib import write_freshness
    write_freshness.record('convA', workspace['target_abs'])
    st = os.stat(workspace['target_abs'])
    os.utime(workspace['target_abs'], ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert write_freshness.is_stale('convA', workspace['target_abs']) is True


@pytest.mark.unit
def test_batch_partition_skips_only_stale_edit(workspace):
    from lib.tasks_pkg.handlers._write_freshness_gate import partition_stale_edits
    from lib import write_freshness
    other = os.path.join(workspace['project_path'], 'b.py')
    with open(other, 'w') as f:
        f.write('def bar():\n    return 2\n')
    write_freshness.record('convA', workspace['target_abs'])
    write_freshness.record('convA', other)
    _external_change(other)  # only b.py went stale
    skip, raw = partition_stale_edits(
        _make_task(),
        {'edits': [
            {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 9'},
            {'path': 'b.py', 'search': 'return 2', 'replace': 'return 8'},
        ]},
        workspace['project_path'])
    assert skip == [1]
    assert raw == ['b.py']


@pytest.mark.unit
def test_gate_disabled_via_env(workspace, monkeypatch):
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    monkeypatch.setenv('TOFU_WRITE_FRESHNESS_GATE', '0')
    write_freshness.record('convA', workspace['target_abs'])
    _external_change(workspace['target_abs'])
    err = check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'x\n'},
        workspace['project_path'])
    assert err is None


@pytest.mark.unit
def test_record_read_paths_after_successful_read(workspace):
    """The handler's post-read seam: tokens recorded, later external change
    flips staleness for THIS conv only."""
    from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths
    from lib import write_freshness
    n = record_read_paths(
        _make_task(), {'reads': [{'path': workspace['target_rel']}]},
        workspace['project_path'], '=== a.py ===\n  1: def foo():\n')
    assert n == 1
    assert write_freshness.is_stale('convA', workspace['target_abs']) is False
    _external_change(workspace['target_abs'])
    assert write_freshness.is_stale('convA', workspace['target_abs']) is True
    assert write_freshness.is_stale('convB', workspace['target_abs']) is False


@pytest.mark.unit
def test_record_read_paths_skips_error_result(workspace):
    from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths
    from lib import write_freshness
    n = record_read_paths(
        _make_task(), {'path': workspace['target_rel']},
        workspace['project_path'], 'Error: File not found: a.py')
    assert n == 0
    _external_change(workspace['target_abs'])
    assert write_freshness.is_stale('convA', workspace['target_abs']) is False


@pytest.mark.unit
def test_ops_write_records_token(workspace):
    """The write-side seam in _ops.py: a real tool_write_file records the
    post-write fingerprint under the writer's conv key."""
    from lib.project_mod.write_tools import tool_write_file
    from lib import write_freshness
    r = tool_write_file(workspace['project_path'], workspace['target_rel'],
                        'v = 1\n', conv_id='convA')
    assert r['ok'], r
    assert write_freshness.is_stale('convA', workspace['target_abs']) is False
    _external_change(workspace['target_abs'])
    assert write_freshness.is_stale('convA', workspace['target_abs']) is True


@pytest.mark.unit
def test_neuter_fingerprint_constant_kills_detection(workspace):
    """NEUTER: with the fingerprint comparator amputated (constant value),
    the clobber scenario no longer refuses — proving the refusal is driven
    by the fingerprint comparison, not by incidental state."""
    from lib.project_mod.write_tools import tool_write_file
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    from lib import write_freshness
    write_freshness.record('convA', workspace['target_abs'])
    r = tool_write_file(workspace['project_path'], workspace['target_rel'],
                        'def foo():\n    return 2  # B\n', conv_id='convB')
    assert r['ok'], r
    # Sanity: with a working comparator the refusal fires (money test).
    assert check_write_freshness(
        _make_task(), 'write_file',
        {'path': workspace['target_rel'], 'content': 'z\n'},
        workspace['project_path']) is not None
    # Amputate: every stat reads as the same constant fingerprint.
    import lib.write_freshness as wf
    wf._reset_for_tests()
    original = wf._fingerprint
    wf._fingerprint = lambda p: (0, 0) if os.path.isfile(p) else None
    try:
        write_freshness.record('convA', workspace['target_abs'])
        tool_write_file(workspace['project_path'], workspace['target_rel'],
                        'def foo():\n    return 4  # B2\n', conv_id='convB')
        err = check_write_freshness(
            _make_task(), 'write_file',
            {'path': workspace['target_rel'], 'content': 'z\n'},
            workspace['project_path'])
        assert err is None  # detection gone → regression would ship silently
    finally:
        wf._fingerprint = original
