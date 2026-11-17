"""A cold invocation compares its first round against the persisted predecessor.

The in-memory baseline dies with the process, so before this the first resumed
round could only say "TURN-BOUNDARY RE-BILL ... gap=0.0s" with no culprits.
The process-stable part of the baseline (hoisted-region byte digests, routing,
model, send time, counts) rides the guarded result checkpoint beside
``lastTurnCacheRead``; a cold round 1 names what changed from it, a real gap,
and records ``baseline: 'durable'``. Another digest version is not comparable.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from lib.task_replay.task_result_checkpoint_contract import (
    CONVERSATION_CACHE_BASELINE_SETTING,
    TASK_CACHE_BASELINE_CANDIDATE_FIELD,
)
from lib.tasks_pkg.cache_tracking import _detect as detect
from lib.tasks_pkg.cache_tracking import _persist as persist
from lib.tasks_pkg.cache_tracking import _state as state
from lib.tasks_pkg.manager._persist import _upsert_task_row
from lib.tasks_pkg.orchestrator._cache_round_accounting import stamp_round_cache_accounting
from lib.tasks_pkg.wire_fingerprint import (
    canonical_messages, routing_fingerprint, system_fingerprint, wire_byte_region,
)
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

OWNER = 7
SYSTEM = 'You are Tofu. ' + 'x' * 400
TOOLS_A = [{'name': 'read_files', 'description': 'read',
            'input_schema': {'type': 'object', 'properties': {}}}]
TOOLS_B = [{'name': 'read_files', 'description': 'read files (changed)',
            'input_schema': {'type': 'object', 'properties': {}}}]
CONTROLS = {'thinking': {'type': 'adaptive'}, 'effort': 'high'}
MESSAGES = [{'role': 'user', 'content': 'hello'}]


@pytest.fixture(autouse=True)
def isolated_memory():
    stores = (state._cache_states, state._cache_invocations,
              persist._hwm_read_cache, persist._last_turn_read_cache,
              persist._baseline_read_cache)
    saved = [dict(store) for store in stores]
    for store in stores:
        store.clear()
    yield
    for store, original in zip(stores, saved):
        store.clear()
        store.update(original)


def restart():
    """Everything process-local is gone; only durable settings remain."""
    state._cache_states.clear()
    state._cache_invocations.clear()
    persist._reset_read_cache_for_tests()


def task_for(conv_id):
    return {'id': f'baseline-task-{time.time_ns()}', 'convId': conv_id,
            '_userId': OWNER, 'created_at': time.time()}


def settings(conv_id):
    from lib.storage import get_storage_client
    doc = get_storage_client().query('conversation.get', {
        'conv_id': conv_id, 'user_id': OWNER, 'derive_messages': False})
    return doc['metadata']['settings']


def usage(*, tools, read, write, controls=CONTROLS, key='key-a'):
    return {
        'cache_read_input_tokens': read,
        'cache_creation_input_tokens': write,
        'input_tokens': 10,
        # Production relays the region only beside the message fingerprint.
        '_wire_fp': canonical_messages(MESSAGES),
        '_wire_region': wire_byte_region(SYSTEM, tools, controls),
        '_wire_system': system_fingerprint(SYSTEM, tools),
        '_wire_controls': controls,
        '_wire_routing': routing_fingerprint(
            key_hash=key, anthropic_beta='prompt-caching',
            endpoint='https://gateway.invalid/v1/messages'),
    }


def run_previous_turn(conv_id):
    task = task_for(conv_id)
    with state.cache_tracking_scope(task):
        stamp_round_cache_accounting(
            task, messages=MESSAGES, tools=TOOLS_A, model='claude-test',
            usage=usage(tools=TOOLS_A, read=270_000, write=4_000),
            api_rounds=[{'round': 1}], round_num=0, tid=task['id'][:8],
            assistant_msg={'content': 'answer'})
        staged = task[TASK_CACHE_BASELINE_CANDIDATE_FIELD]
        assert _upsert_task_row(task, conv_id, content='answer', thinking='',
                                status='done', error_json=None, tr_json=None,
                                meta_json=None)
    assert TASK_CACHE_BASELINE_CANDIDATE_FIELD not in task
    return staged


def resumed_round(conv_id, monkeypatch, *, tools, gap_s=125.0, **extra):
    records = []
    monkeypatch.setattr(detect._health.read_share_monitor, 'observe',
                        records.append)
    now = time.time() + gap_s
    monkeypatch.setattr(detect, 'time', SimpleNamespace(time=lambda: now))
    with state.cache_tracking_scope(task_for(conv_id)):
        verdict = detect.detect_cache_break(
            conv_id, MESSAGES, tools, 'claude-test',
            usage=usage(tools=tools, **extra), user_id=OWNER)
    [record] = records
    return verdict, record


def new_conversation():
    conv_id = f'durable-baseline-{time.time_ns()}'
    seed_conversation(conv_id, user_id=OWNER)
    return conv_id


def test_staged_baseline_is_committed_with_the_last_read():
    conv_id = new_conversation()
    staged = run_previous_turn(conv_id)
    stored = settings(conv_id)
    assert stored['lastTurnCacheRead'] == 270_000
    assert stored[CONVERSATION_CACHE_BASELINE_SETTING] == staged
    assert staged['v'] == persist.CACHE_BASELINE_VERSION
    assert staged['read'] == 270_000 and staged['calls'] == 1
    assert set(staged['region']) == {'system', 'tools', 'controls'}
    assert staged['controls'] == CONTROLS
    assert 'messages' in staged and 'fingerprint' not in staged


def test_cold_round_names_changed_tools_and_the_real_gap(monkeypatch, caplog):
    conv_id = new_conversation()
    run_previous_turn(conv_id)
    restart()
    verdict, record = resumed_round(conv_id, monkeypatch, tools=TOOLS_B,
                                    read=0, write=280_000)
    assert record['bucket'] == 'turn_boundary_rebill'
    assert record['baseline'] == 'durable'
    assert '<bytes>tools' in record['culprits']
    assert '<hoisted>.tools' in record['culprits']
    assert '<bytes>system' not in record['culprits']
    assert record['routing_diff'] == []
    assert record['gap_s'] == pytest.approx(125.0, abs=1.0)
    assert verdict['gap_s'] == pytest.approx(125.0, abs=1.0)
    assert 'TURN-BOUNDARY RE-BILL' in caplog.text
    assert 'baseline=durable changed=[<hoisted>.tools, <bytes>tools]' in caplog.text


def test_cold_round_with_identical_bytes_names_nothing(monkeypatch):
    conv_id = new_conversation()
    run_previous_turn(conv_id)
    restart()
    _, record = resumed_round(conv_id, monkeypatch, tools=TOOLS_A,
                              read=0, write=280_000)
    assert record['bucket'] == 'turn_boundary_rebill'
    assert record['baseline'] == 'durable'
    assert 'culprits' not in record
    assert record['routing_diff'] == []


def test_cold_round_names_control_and_routing_changes(monkeypatch, caplog):
    conv_id = new_conversation()
    run_previous_turn(conv_id)
    restart()
    _, record = resumed_round(
        conv_id, monkeypatch, tools=TOOLS_A, read=0, write=280_000,
        controls={**CONTROLS, 'effort': 'low'}, key='key-b')
    assert '<bytes>controls' in record['culprits']
    assert record['routing_diff'] == ['<ns>key']
    assert "effort: 'high' → 'low'" in caplog.text


def test_other_digest_version_is_not_comparable(monkeypatch):
    conv_id = new_conversation()
    run_previous_turn(conv_id)
    from lib.conversations.settings_store import update_conversation_settings

    def older_version(stored):
        stored[CONVERSATION_CACHE_BASELINE_SETTING]['v'] = (
            persist.CACHE_BASELINE_VERSION + 1)

    update_conversation_settings(conv_id, older_version, user_id=OWNER,
                                 notify=False)
    restart()
    _, record = resumed_round(conv_id, monkeypatch, tools=TOOLS_B,
                              read=0, write=280_000)
    assert record['bucket'] == 'turn_boundary_rebill'
    assert record['baseline'] == 'none'
    assert 'culprits' not in record
    assert record['gap_s'] == 0.0


def test_second_round_uses_the_live_baseline(monkeypatch):
    conv_id = new_conversation()
    run_previous_turn(conv_id)
    task = task_for(conv_id)
    records = []
    monkeypatch.setattr(detect._health.read_share_monitor, 'observe',
                        records.append)
    with state.cache_tracking_scope(task):
        detect.detect_cache_break(
            conv_id, MESSAGES, TOOLS_A, 'claude-test',
            usage=usage(tools=TOOLS_A, read=270_000, write=1_000),
            user_id=OWNER)
    assert records[-1]['baseline'] == 'live'


def test_baseline_without_its_last_read_is_refused():
    from lib.storage import StorageError, get_storage_client
    conv_id = new_conversation()
    with pytest.raises(StorageError):
        get_storage_client(write=True).command('task_results.checkpoint', {
            'key': 'lonely-baseline', 'expected_version': 0,
            'guard_contract': 'tofu.task-results.checkpoint.guard/v1',
            'require_parent': True,
            'value': {'task_id': 'lonely-baseline', 'conv_id': conv_id,
                      'user_id': OWNER, 'status': 'done',
                      'cache_baseline': {'v': 1}},
        }, f'lonely-{time.time_ns()}')
