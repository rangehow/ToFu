"""Request Inspector server fold (P2) — pytest suite.

Design: docs/DEBUG_PANEL_REDESIGN.md §3.3 (frozen row schemas). Verifies
``lib/tasks_pkg/request_inspector.py`` against REAL seeded ``task_events``
rows (unique task ids in the dev DB, cleaned up after):

  1. Request rows are METADATA-ONLY (no ``messages``/``tools`` bulk) and
     come ONLY from request-kind snapshots — state snapshots (post-tool /
     final / fallback) route to ``states``.
  2. Legacy rows (no ``kind``) classify via the roundNum/label shim and are
     flagged ``legacy:true``.
  3. ``round_usage`` events join as ``attempts`` per round — MULTIPLE per
     round (R1 + R1-FALLBACK = 2 real HTTP calls, the fallback case).
  4. ``coverage`` flips to ``'partial'`` when the task drove endpoint mode
     (Planner/Critic calls not captured — the honest chip).
  5. Unknown/expired task → ``eventsAvailable:false``, empty lists.
  6. ``get_request_payload`` serves the full payload per round, last
     re-emitted snapshot wins, 404 (None) for state-only/unknown rounds.
  7. ``list_conv_tasks`` returns task rows with EXACT kind-counted tallies.
  8. Route registration pins the three endpoints on the v1 blueprint.

NEUTER: make ``_snapshot_kind`` classify everything as 'request' → the
state-separation assertions flip red (proving the split is load-bearing).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_TARGET = os.path.join(ROOT, 'lib', 'tasks_pkg', 'request_inspector.py')


def _seed(task_id, events):
    """Persist (type, payload) events with sequential ids; returns task_id."""
    from lib.tasks_pkg.event_log import append_persistent_event
    for eid, (etype, payload) in enumerate(events):
        append_persistent_event(task_id, eid, payload | {'type': etype})
    return task_id


def _cleanup(*task_ids):
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        for tid in task_ids:
            db.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
            db.execute('DELETE FROM task_results WHERE task_id=?', (tid,))
        db.commit()
    except Exception:
        pass


def _snap(kind=None, round_num=1, label='', n_msgs=3, tools=2):
    p = {
        'roundNum': round_num,
        'label': label or f'Round {round_num} 请求前 · {n_msgs}条',
        'messages': [{'role': 'user', 'content': 'x' * 100}] * n_msgs,
        'model': 'm-test',
        'params': {'maxTokens': 1000, 'temperature': 1},
    }
    if tools:
        p['tools'] = [{'function': {'name': 't%d' % i}} for i in range(tools)]
    if kind:
        p['kind'] = kind
    return p


def _usage_event(round_num, tag, model='m-test', trace='trace-abc'):
    return ('round_usage', {
        'roundNum': round_num, 'model': model, 'tag': tag,
        'tokensIn': 500, 'tokensOut': 120,
        'usage': {'trace_id': trace, 'stream_elapsed_ms': 2300,
                  'prompt_tokens': 500, 'completion_tokens': 120},
    })


@pytest.fixture()
def task_a():
    tid = f'ri-a-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('messages_snapshot', _snap('request', 1, n_msgs=3)),
        _usage_event(1, 'R1', trace='trace-r1'),
        _usage_event(1, 'R1-FALLBACK', model='m-fb', trace='trace-r1fb'),
        ('messages_snapshot', _snap('request', 2, n_msgs=5)),
        _usage_event(2, 'R2', trace='trace-r2'),
        ('messages_snapshot', _snap('state', 'final', label='最终回复后 · 6条',
                                    n_msgs=6, tools=0)),
    ])
    yield tid
    _cleanup(tid)


@pytest.fixture()
def task_legacy():
    tid = f'ri-l-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('messages_snapshot', _snap(None, 1, label='Round 1 请求前 · 2条',
                                    n_msgs=2)),
        ('messages_snapshot', _snap(None, 1, label='Round 1 工具结果后 · 4条',
                                    n_msgs=4)),
        ('messages_snapshot', _snap(None, 'final', label='最终回复后 · 5条',
                                    n_msgs=5, tools=0)),
    ])
    yield tid
    _cleanup(tid)


@pytest.fixture()
def task_endpoint():
    tid = f'ri-e-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('endpoint_iteration', {'iteration': 1, 'phase': 'working'}),
        ('messages_snapshot', _snap('request', 1, n_msgs=3)),
    ])
    yield tid
    _cleanup(tid)


def test_request_rows_metadata_only_and_split(task_a):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    assert fold['eventsAvailable'] is True
    assert fold['requestCount'] == 2
    reqs = fold['requests']
    assert [r['roundNum'] for r in reqs] == [1, 2]
    for r in reqs:
        # METADATA-ONLY: the payload must NOT ride the list rows.
        assert 'messages' not in r, f'payload leaked into row: {r.keys()}'
        assert 'tools' not in r
        assert r['model'] == 'm-test'
        assert r['params'].get('maxTokens') == 1000
        assert r['approxTokens'] > 0
        assert r['ts'] > 0
    # state snapshot routed to states, never into requests
    assert len(fold['states']) == 1
    assert fold['states'][0]['roundNum'] == 'final'
    assert fold['coverage'] == 'full'


def test_attempts_join_multi_call_round(task_a):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    r1 = fold['requests'][0]
    assert len(r1['attempts']) == 2, (
        'R1 primary + R1-FALLBACK = two real HTTP calls, both must join')
    tags = [a['tag'] for a in r1['attempts']]
    assert tags == ['R1', 'R1-FALLBACK']
    fb = r1['attempts'][1]
    assert fb['model'] == 'm-fb' and fb['traceId'] == 'trace-r1fb'
    assert fb['tokensIn'] == 500 and fb['streamElapsedMs'] == 2300
    r2 = fold['requests'][1]
    assert [a['tag'] for a in r2['attempts']] == ['R2']


def test_legacy_rows_classified_by_shim(task_legacy):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_legacy)
    assert fold['requestCount'] == 1
    assert fold['requests'][0]['legacy'] is True
    assert fold['requests'][0]['messageCount'] == 2
    state_labels = [s['label'] for s in fold['states']]
    assert any('工具结果后' in lb for lb in state_labels)
    assert any('最终回复后' in lb for lb in state_labels)
    assert all(s['legacy'] for s in fold['states'])


def test_coverage_partial_for_endpoint_task(task_endpoint):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_endpoint)
    assert fold['coverage'] == 'partial'
    assert fold['requestCount'] == 1


def test_unknown_task_honest_empty():
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(f'ri-none-{uuid.uuid4().hex[:8]}')
    assert fold['eventsAvailable'] is False
    assert fold['requests'] == [] and fold['states'] == []
    assert fold['requestCount'] == 0


def test_payload_on_demand_last_wins(task_a):
    from lib.tasks_pkg.request_inspector import get_request_payload
    p2 = get_request_payload(task_a, 2)
    assert p2 is not None
    assert len(p2['messages']) == 5
    assert len(p2['tools']) == 2
    assert p2['params'].get('maxTokens') == 1000
    # string roundNum also resolves (frontend passes strings)
    p2s = get_request_payload(task_a, '2')
    assert p2s is not None and len(p2s['messages']) == 5
    # 'final' is a STATE round → no request payload
    assert get_request_payload(task_a, 'final') is None
    # unknown round → None
    assert get_request_payload(task_a, 99) is None


def test_list_conv_tasks_exact_tallies(task_a, task_legacy):
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import TASK_RESULTS, upsert
    conv = f'ri-conv-{uuid.uuid4().hex[:8]}'
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    for tid, ts in ((task_a, now), (task_legacy, now - 1000)):
        upsert(db, TASK_RESULTS,
               {'task_id': tid, 'conv_id': conv, 'content': '',
                'status': 'done', 'created_at': ts, 'completed_at': ts},
               conflict_cols=['task_id'],
               insert_cols=['task_id', 'conv_id', 'content', 'status',
                            'created_at', 'completed_at'],
               update_cols=[], commit=True, retry=False)
    try:
        from lib.tasks_pkg.request_inspector import list_conv_tasks
        out = list_conv_tasks(conv)
        rows = {t['taskId']: t for t in out['tasks']}
        assert task_a in rows and task_legacy in rows
        ra = rows[task_a]
        assert ra['requestCount'] == 2 and ra['stateCount'] == 1
        assert ra['legacyCount'] == 0 and ra['hasEvents'] is True
        assert ra['status'] == 'done' and ra['live'] is False
        rl = rows[task_legacy]
        assert rl['legacyCount'] == 3 and rl['requestCount'] == 0
        # newest first
        assert out['tasks'][0]['taskId'] == task_a
        # unknown conv → empty
        assert list_conv_tasks(f'ri-noconv-{uuid.uuid4().hex[:6]}')['tasks'] == []
    finally:
        _cleanup(task_a, task_legacy)


def test_routes_registered_on_v1_blueprint():
    from quart import Quart

    from routes.api_v1.tasks import api_v1_tasks_bp
    app = Quart(__name__)
    app.register_blueprint(api_v1_tasks_bp)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert '/api/v1/tasks/by-conv/<conv_id>' in rules
    assert '/api/v1/tasks/<task_id>/requests' in rules
    assert '/api/v1/tasks/<task_id>/requests/<round_num>' in rules


def test_neuter_state_split_is_load_bearing(task_a):
    """NC: classify EVERYTHING as 'request' → the states bucket empties and
    the request list gets polluted — proving the split is load-bearing."""
    from tests._nc_harness import neutered_source
    fixed = "    kind = payload.get('kind')\n    if kind in ('request', 'state'):"
    broken = ("    kind = payload.get('kind')\n    if True:  # NC-RI-SPLIT\n"
              "        return 'request'\n    if kind in ('request', 'state'):")
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert fixed in src, 'NC anchor drifted — update the neuter'
    with neutered_source(_TARGET, fixed, broken) as mod:
        # Drive the NEUTERED module object directly (importlib.reload would
        # re-read the un-neutered file and defeat the neuter — see
        # tests/_nc_harness.py docstring).
        fold = mod.fold_request_log(task_a)
        assert fold['states'] == [] and any(
            r['roundNum'] == 'final' for r in fold['requests']), (
            f'expected state rows to pollute requests under NC: {fold}')
    # Post-restore: the canonical module (never mutated) splits again.
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    assert len(fold['states']) == 1 and fold['requestCount'] == 2
    with open(_TARGET, encoding='utf-8') as f:
        assert 'NC-RI-SPLIT' not in f.read(), (
            'shipped request_inspector.py must be byte-identical')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
