"""Ordered concurrent execution for independent filename lookups."""
import threading

import pytest

from lib.project_mod import read_tools

pytestmark = pytest.mark.unit


def test_find_files_batch_overlaps_slow_entries_and_preserves_order(
        tmp_path, monkeypatch):
    allow_first = threading.Event()
    entered = []
    entered_lock = threading.Lock()

    def delayed_find(base, pattern, rel_path=None, max_results=None):
        with entered_lock:
            entered.append(pattern)
            if len(entered) == 3:
                allow_first.set()
        if pattern == 'first.py':
            assert allow_first.wait(2), 'siblings were serialized behind the first entry'
        return f'result:{pattern}'

    monkeypatch.setattr(read_tools, 'tool_find_files', delayed_find)
    result = read_tools.tool_find_files_batch(str(tmp_path), [
        {'pattern': 'first.py'},
        {'pattern': 'second.py'},
        {'pattern': 'third.py'},
        {'invalid': True},
        {'pattern': 'last.py'},
    ])

    assert result.split('\n\n') == [
        'result:first.py', 'result:second.py', 'result:third.py',
        '[4] Error: each entry must have a "pattern" field', 'result:last.py',
    ]


def test_find_files_batch_waits_for_every_entry_before_propagating_an_error(
        tmp_path, monkeypatch):
    allow_first = threading.Event()
    entered = []
    entered_lock = threading.Lock()

    def delayed_find(base, pattern, rel_path=None, max_results=None):
        with entered_lock:
            entered.append(pattern)
            if len(entered) == 3:
                allow_first.set()
        if pattern == 'first.py':
            assert allow_first.wait(2), 'siblings were serialized behind the first entry'
        if pattern == 'invalid':
            raise ValueError('injected invalid lookup')
        return f'result:{pattern}'

    monkeypatch.setattr(read_tools, 'tool_find_files', delayed_find)
    with pytest.raises(ValueError, match='injected invalid lookup'):
        read_tools.tool_find_files_batch(str(tmp_path), [
            {'pattern': 'first.py'},
            {'pattern': 'invalid'},
            {'pattern': 'last.py'},
        ])

    assert sorted(entered) == ['first.py', 'invalid', 'last.py']


def test_find_files_single_entry_keeps_direct_execution(tmp_path, monkeypatch):
    calls = []

    def find(base, pattern, rel_path=None, max_results=None):
        calls.append((pattern, threading.current_thread().name))
        return f'result:{pattern}'

    monkeypatch.setattr(read_tools, 'tool_find_files', find)
    result = read_tools.tool_find_files_batch(str(tmp_path), [
        {'pattern': 'only.py'}, {'invalid': True},
    ])

    assert calls == [('only.py', threading.current_thread().name)]
    assert result == (
        'result:only.py\n\n[2] Error: each entry must have a "pattern" field')
