"""Large head-truncation projections preserve ordering and complete tool units."""

import json

import pytest

from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate


def _round(call_id):
    return [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': call_id, 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': call_id, 'content': 'result'},
    ]


@pytest.mark.parametrize('budget', [{'token_target': 1}, {'byte_target': 1}])
def test_large_projection_preserves_required_rows_and_pairing(budget, monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._reactive._headtrunc.audit_log',
                        lambda *args, **kwargs: None)
    system = {'role': 'system', 'content': 'required system'}
    objective = {'role': 'user', 'content': 'opening request'}
    developer = {'role': 'developer', 'content': 'required developer'}
    current = {'role': 'user', 'content': 'current request'}
    newest = _round('newest')
    cold = [{'role': 'assistant', 'content': f'old observation {i}'} for i in range(2_000)]
    source = [system, objective, *cold[:1000], developer, *_round('old'),
              *cold[1000:], current, *newest]
    before = json.dumps(source)
    projection = list(source)

    assert _head_truncate(projection, **budget) == len(cold) + 2
    assert projection == [system, objective, developer, current, *newest]
    assert json.dumps(source) == before
