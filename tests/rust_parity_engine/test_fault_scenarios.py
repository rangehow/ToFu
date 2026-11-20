"""Fault scenarios must finish their intended path, not exhaust the replay script."""

import base64
import json
from pathlib import Path

import pytest

from tests.rust_parity_engine import reference

pytestmark = pytest.mark.api


@pytest.mark.parametrize(('name', 'effect', 'code'), [
    ('mutating_tool_lost_after_entry', 'unknown', 'tool_effect_unknown'),
    ('mutating_tool_refused_before_entry', 'not_started', 'tool_execution_failed'),
    ('mutating_tool_handler_error', 'unknown', 'tool_effect_unknown'),
    ('read_only_tool_handler_error', 'unknown', 'tool_execution_failed'),
])
def test_tool_failure_reaches_explicit_blocked_answer_without_replaying_the_tool(name, effect, code):
    cases = [json.loads(line) for line in
             Path(__file__).with_name('cases.jsonl').read_text().splitlines()]
    case = next(case for case in cases if case['name'] == name)
    parts = {part['name']: json.loads(base64.b64decode(part['b64']))
             for part in reference.engine_session(case['input'])}
    assert parts['outcome']['status'] == 'done'
    assert parts['outcome']['finishReason'] == 'stop'
    assert parts['outcome']['errorKind'] is None
    assert len(parts['requests']) == 3
    assert len(parts['tools']) == 1
    assert parts['tools'][0]['isError'] is True
    assert parts['tools'][0]['effect'] == effect
    error = json.loads(parts['tools'][0]['result'])
    assert error['code'] == code
    assert error['retryable'] is False
    nudge = parts['requests'][-1]['messages'][-1]
    assert nudge['role'] == 'user'
    assert 'UNRESOLVED TOOL FAILURE' in nudge['content']
    assert '[END_TURN:' not in parts['outcome']['content']
    assert 'script exhausted' not in parts['outcome']['content']
