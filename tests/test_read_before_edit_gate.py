"""Tests for the apply_diff / insert_content read-before-edit gate.

The gate refuses an edit when the target file has not been read (or
written) earlier in the conversation. See
``lib/tasks_pkg/handlers/_read_gate.py`` for the policy.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Create a temp project with one file and route the gate to it."""
    proj = tmp_path / 'proj'
    proj.mkdir()
    target = proj / 'a.py'
    target.write_text('def foo():\n    return 1\n')
    # Disable env override so the gate is on regardless of the runner's env.
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE', raising=False)
    return {'project_path': str(proj), 'target_rel': 'a.py',
            'target_abs': str(target)}


def _make_task(messages=None, tool_rounds=None, conv_id='c1', task_id='t1'):
    return {
        'id': task_id,
        'convId': conv_id,
        'messages': list(messages or []),
        'toolRounds': list(tool_rounds or []),
    }


def test_gate_blocks_unread_file(workspace):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    task = _make_task()
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is not None
    assert 'must read each target file first' in err
    assert workspace['target_rel'] in err


def test_gate_allows_after_prior_done_read_in_rounds(workspace):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    rounds = [{
        'roundNum': 1,
        'toolName': 'read_files',
        'toolArgs': json.dumps({'reads': [{'path': workspace['target_rel']}]}),
        'status': 'done',
    }]
    task = _make_task(tool_rounds=rounds)
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is None


def test_gate_blocks_when_sibling_read_still_searching(workspace):
    """The exact failure pattern: sibling read_files in same parallel turn
    has not completed yet (status='searching')."""
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    rounds = [{
        'roundNum': 1,
        'toolName': 'read_files',
        'toolArgs': json.dumps({'reads': [{'path': workspace['target_rel']}]}),
        'status': 'searching',  # not done — siblings can't satisfy each other
    }]
    task = _make_task(tool_rounds=rounds)
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is not None


def test_gate_allows_after_prior_turn_read_in_messages(workspace):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    messages = [
        {'role': 'user', 'content': 'fix it'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'tc_read_1',
                'function': {
                    'name': 'read_files',
                    'arguments': json.dumps({'reads': [{'path': workspace['target_rel']}]}),
                },
            }],
        },
        {
            'role': 'tool',
            'tool_call_id': 'tc_read_1',
            'content': '=== a.py ===\n  1: def foo():\n  2:     return 1\n',
        },
    ]
    task = _make_task(messages=messages)
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is None


def test_gate_does_not_count_failed_read_in_messages(workspace):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    messages = [
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'tc_read_fail',
                'function': {
                    'name': 'read_files',
                    'arguments': json.dumps({'reads': [{'path': workspace['target_rel']}]}),
                },
            }],
        },
        {
            'role': 'tool',
            'tool_call_id': 'tc_read_fail',
            'content': 'Error: File not found: a.py',
        },
    ]
    task = _make_task(messages=messages)
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is not None


def test_gate_skips_nonexistent_file(workspace):
    """When the file doesn't exist, let downstream surface the cleaner
    "File not found" error — gating it would be confusing."""
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    task = _make_task()
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': 'does_not_exist.py', 'search': 'x', 'replace': 'y'},
        workspace['project_path'],
    )
    assert err is None


def test_gate_blocks_batch_when_any_path_unread(workspace):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    # Read only one of the two files
    other = os.path.join(workspace['project_path'], 'b.py')
    with open(other, 'w') as f:
        f.write('def bar():\n    return 2\n')
    rounds = [{
        'roundNum': 1,
        'toolName': 'read_files',
        'toolArgs': json.dumps({'reads': [{'path': workspace['target_rel']}]}),
        'status': 'done',
    }]
    task = _make_task(tool_rounds=rounds)
    err = check_read_before_edit(
        task, 'apply_diff',
        {'edits': [
            {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
            {'path': 'b.py', 'search': 'return 2', 'replace': 'return 3'},
        ]},
        workspace['project_path'],
    )
    assert err is not None
    assert 'b.py' in err
    # Read file should NOT appear in the unread list
    # (it might still appear in some other context, but the canonical line is "Unread file(s):")
    unread_line = [ln for ln in err.split('\n') if 'Unread file(s):' in ln][0]
    assert 'b.py' in unread_line
    assert workspace['target_rel'] not in unread_line


def test_gate_allows_after_write_file(workspace):
    """write_file gives the model authoritative content of the file."""
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    rounds = [{
        'roundNum': 1,
        'toolName': 'write_file',
        'toolArgs': json.dumps({'path': workspace['target_rel'], 'content': 'x'}),
        'status': 'done',
    }]
    task = _make_task(tool_rounds=rounds)
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is None


def test_gate_disabled_via_env(workspace, monkeypatch):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    monkeypatch.setenv('TOFU_APPLY_DIFF_READ_GATE', '0')
    task = _make_task()
    err = check_read_before_edit(
        task, 'apply_diff',
        {'path': workspace['target_rel'], 'search': 'return 1', 'replace': 'return 2'},
        workspace['project_path'],
    )
    assert err is None


def test_gate_passthrough_for_nongated_tools(workspace):
    """write_file / read_files / list_dir / etc. are NOT gated."""
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    task = _make_task()
    for name in ('write_file', 'read_files', 'list_dir', 'grep_search', 'find_files'):
        err = check_read_before_edit(
            task, name,
            {'path': workspace['target_rel']},
            workspace['project_path'],
        )
        assert err is None, f'{name} should not be gated'


def test_gate_insert_content_also_gated(workspace):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit
    task = _make_task()
    err = check_read_before_edit(
        task, 'insert_content',
        {'path': workspace['target_rel'], 'anchor': 'def foo', 'content': 'pass\n'},
        workspace['project_path'],
    )
    assert err is not None
    assert 'insert_content refused' in err
