"""Dispatch-level spec: one local-file fetch_url settles as read_files.

The ingest seam owns the rewrite; this pins that the chat dispatcher's tool
round, persisted tool-call name and repair badge all describe the call that
actually executes, not the model's original (wrong-surface) emission.
"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@pytest.mark.unit
def test_file_uri_fetch_url_parses_as_read_files():
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tools.project import READ_FILES_TOOL
    from lib.tools.search import build_fetch_url_tool

    catalog = [build_fetch_url_tool(), READ_FILES_TOOL]
    task = {
        'id': 'local-file-fetch', 'convId': 'local-file-fetch',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False, 'config': {},
        '_executable_tool_catalog': catalog,
        '_toolContractDocumentsByName': compile_execution_contract_documents(catalog),
    }
    message = {'content': '', 'tool_calls': [{
        'id': 'call-local', 'type': 'function', 'function': {
            'name': 'fetch_url',
            'arguments': json.dumps({'url': 'file:///mnt/project/docs/a.md'})}}]}

    parsed, _ = parse_tool_calls(
        message, task, round_num=0, tool_round_num=0, project_enabled=False)
    tc, fn_name, _tc_id, fn_args, _rn, round_entry, parse_error = parsed[0]

    assert parse_error is None
    assert fn_name == 'read_files'
    assert fn_args == {'path': '/mnt/project/docs/a.md'}
    assert tc['function']['name'] == 'read_files'
    assert round_entry['status'] != 'rejected'
    assert 'local_file_tool_redirect' in round_entry['_repaired']['patterns']
    assert any('dispatched as read_files' in note
               for note in round_entry['_repaired']['warnings'])

@pytest.mark.unit
def test_early_announced_row_is_settled_under_the_executed_tool():
    """Streaming already showed fetch_url for this call id; the repair must
    relabel the same row to read_files rather than leaving a stale tool name."""
    from lib.tasks_pkg.tool_dispatch._repair import _apply_repair_to_round

    round_entry = {
        'toolName': 'fetch_url', 'toolCallId': 'call-local', 'roundNum': 1,
        'query': 'file:///mnt/project/docs/a.md',
    }
    _apply_repair_to_round(
        round_entry, 'read_files', {'path': '/mnt/project/docs/a.md'},
        {'label': 'auto-fixed', 'detail': 'redirected',
         'patterns': ['local_file_tool_redirect']},
        False, 'local-file-fetch')

    assert round_entry['toolName'] == 'read_files'
    assert round_entry['toolArgs'] == '{"path": "/mnt/project/docs/a.md"}'

@pytest.mark.unit
def test_all_local_batch_parses_as_one_read_files_batch():
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tools.project import READ_FILES_TOOL
    from lib.tools.search import build_fetch_url_tool

    catalog = [build_fetch_url_tool(), READ_FILES_TOOL]
    task = {
        'id': 'local-file-batch', 'convId': 'local-file-batch',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False, 'config': {},
        '_executable_tool_catalog': catalog,
        '_toolContractDocumentsByName': compile_execution_contract_documents(catalog),
    }
    message = {'content': '', 'tool_calls': [{
        'id': 'call-batch', 'type': 'function', 'function': {
            'name': 'fetch_url', 'arguments': json.dumps({'urls': [
                {'url': 'file:///tmp/a.md'}, {'url': 'file:///tmp/b.md'}]})}}]}

    parsed, _ = parse_tool_calls(
        message, task, round_num=0, tool_round_num=0, project_enabled=False)
    tc, fn_name, _tc_id, fn_args, _rn, round_entry, parse_error = parsed[0]

    assert parse_error is None
    assert fn_name == 'read_files'
    assert fn_args == {'reads': [{'path': '/tmp/a.md'}, {'path': '/tmp/b.md'}]}
    assert tc['function']['name'] == 'read_files'
    assert round_entry['status'] != 'rejected'
    assert 'local_file_tool_redirect' in round_entry['_repaired']['patterns']
