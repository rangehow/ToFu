"""The owner's delegation to every lead, on a real two-process instance (docs/ROUTER.md R9).

Separate OS processes: storage host, resident execution host and API host
(tests/chaos/instance.py), driven only through the public v4 API and the
scripted provider. The owner grants "Act for me" from the browser (same-origin
Fetch Metadata), binds a Project Router and tells it, in an ordinary owner Turn,
to come back at a due time; that Turn delegates nothing, so no request of the
owner's continues it. When the promise is
due the platform wakes the idle lead with a nonhuman commitment notice, a Turn
with no owner message of its own. In it the lead:

1. dispatches a Worker under the owner's full grant (one ``lead_authority``
   record, trigger ``owner_delegation``), and that Worker, in its isolated task
   workspace, calls ``integration_submit`` with its own changes;
2. after the owner revokes the grant mid-Turn, is refused the next dispatch and
   ``restart_server`` (nothing starts, the server keeps serving).

Before 2026-11-18 such a wake was refused 'Synthetic authorization origin
denied' and Workers had no ``integration_submit``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
import threading
import time
import uuid

import pytest

from tests.chaos import fake_provider as fp
from tests.chaos.test_chaos_faults import SETTLE_DEADLINE_S, _bind_coordinator, _running_chaos

pytestmark = [pytest.mark.api, pytest.mark.timeout(900)]

# The commitment wake fires at the minute its due time falls in
# (docs/PROJECT_COORDINATION.md, Commitments on the task board); a promise due
# at the next minute boundary plus that minute bounds the wait.
WAKE_DEADLINE_S = 180.0 + SETTLE_DEADLINE_S


@pytest.fixture
def chaos(tmp_path_factory):
    # A commitment wake starts with no config of its own: the deployment's default model, and
    # the scheduler's thinking-on default (lib/scheduler/_shared.py), so the model declares
    # reasoning that can be turned off.
    with _running_chaos(tmp_path_factory.mktemp('chaos-leads'), settings={'LLM_MODEL': fp.MODEL_ID},
                        reasoning={'support': 'supported', 'wire_format': 'thinking_type',
                                   'can_disable': True, 'supported_efforts': []}) as harness:
        yield harness


def _key() -> dict:
    return {'Idempotency-Key': f'chaos-{uuid.uuid4().hex}'}


def _data(response: dict):
    return response.get('data', response)


def _grant(chaos) -> dict:
    chaos.client.request('POST', '/api/v4/router', {}, headers=_key())
    state = _data(chaos.client.request('POST', '/api/v4/router/owner-delegation', {'granted': True},
                                       headers={**_key(), 'Sec-Fetch-Site': 'same-origin'}))
    assert state['grant']['state'] == 'granted' and state['grant']['coverage'] == 'full', state
    return state


def _revoke(chaos) -> None:
    state = _data(chaos.client.request('POST', '/api/v4/router/owner-delegation', {'granted': False},
                                       headers=_key()))
    assert state['grant']['state'] == 'revoked', state


def _wait(predicate, what: str, deadline_s: float = SETTLE_DEADLINE_S):
    deadline = time.monotonic() + deadline_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, what
        time.sleep(0.2)


def _results(chaos, nonce: str) -> list[str]:
    return [text for request in chaos.provider.requests(nonce)
            for text in fp.result_texts(request['tool_results'])]


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {'raw': text}
    return value if isinstance(value, dict) else {'raw': text}


def _rows(texts: list[str]) -> list[dict]:
    """Every object carrying an assignment_id in the tool results, oldest first."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get('assignment_id'):
                found.append(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for text in texts:
        try:
            walk(json.loads(text))
        except ValueError:
            continue
    return found


def _latest(texts: list[str], *keys: str) -> dict:
    return [row for row in _rows(texts) if all(row.get(key) for key in keys)][-1]


def _follow_up(instruction: str):
    def arguments(texts):
        row = _latest(texts, 'turn_id', 'attempt_id', 'result_revision')
        turn = [item for item in _status_turns(texts) if item.get('turnId') == row['turn_id']][-1]
        return {'conversation_id': row['conversation_id'], 'turn_id': row['turn_id'],
                'attempt_id': row['attempt_id'], 'expected_revision': turn['projectionRevision'],
                'action': 'follow_up', 'instruction': instruction, 'idempotency_key': 'steer-1'}
    return arguments


def _status_turns(texts: list[str]) -> list[dict]:
    turns = []
    for text in texts:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('turns'), list):
            turns.extend(value['turns'])
    return turns


def _accept(texts):
    row = _latest(texts, 'result_revision', 'turn_id')
    delivery = row.get('delivery') or {}
    sha = delivery.get('checkpointSha') or delivery.get('checkpoint_sha') or delivery.get('sha')
    return {'assignment_id': row['assignment_id'], 'result_revision': row['result_revision'],
            'decision': 'accept', 'note': 'Verified delegated.txt in the submitted checkpoint.',
            **({'checkpoint_sha': sha} if sha else {})}


def test_a_lead_woken_by_a_notice_starts_steers_and_accepts_a_worker_and_revocation_refuses_next_acts(chaos):
    project = str(chaos.workspace)
    lead = _bind_coordinator(chaos)
    _grant(chaos)
    release = threading.Event()
    worker = chaos.provider.script([
        fp.tool('run_command', {'command': 'pwd && echo worker-change > delegated.txt && cat delegated.txt'}),
        fp.tool('integration_submit', {'summary': 'Added delegated.txt; verified with cat.'}),
        fp.text('Submitted. [END_TURN: done]')])
    steered = chaos.provider.script([
        fp.tool('run_command', {'command': 'echo steered >> delegated.txt && cat delegated.txt'}),
        fp.tool('integration_submit', {'summary': 'Appended the steered line; verified with cat.'}),
        fp.text('Steered change submitted. [END_TURN: done]')])
    wake = chaos.provider.script([
        fp.tool('delegate_project_work', {
            'title': 'Add the delegated file', 'brief': f'Add delegated.txt. [chaos turn={worker}]',
            'idempotency_key': 'wake-worker-1'}),
        fp.tool('poll', lambda texts: {'ids': [_latest(texts, 'assignment_id')['assignment_id']],
                                       'wait_s': 600}),
        fp.tool('get_conversation_status', lambda texts: {
            'conversation_id': _latest(texts, 'turn_id')['conversation_id'],
            'turn_id': _latest(texts, 'turn_id')['turn_id']}),
        fp.tool('control_conversation', _follow_up(f'Also append a steered line. [chaos turn={steered}]')),
        fp.tool('poll', lambda texts: {'ids': [_latest(texts, 'assignment_id')['assignment_id']],
                                       'wait_s': 600}),
        fp.tool('review_project_work', _accept),
        # Human-only acts stay out of every lead's reach, granted or not.
        fp.tool('owner_settings', {'reason': 'Store a provider key', 'under_delegation': True,
                                   'idempotency_key': 'credential'}),
        fp.tool('channel_outbox_approve', {'idempotency_key': 'publish'}),
        fp.hold('Checking. ', release, fp.tool('delegate_project_work', {
            'title': 'Second dispatch after revocation', 'brief': 'Must not start.',
            'idempotency_key': 'wake-worker-2'})),
        fp.tool('restart_server', {'reason': 'Load the delegated change', 'idempotency_key': 'wake-restart',
                                   'under_delegation': True}),
        fp.text('Done for now. [END_TURN: done]')])
    # The first minute boundary at least 30 s ahead: the board refuses a due time that passes
    # before the owner's Turn writes it.
    due = (datetime.now(timezone.utc) + timedelta(seconds=90)).replace(second=0, microsecond=0).isoformat()
    title = f'Report on the delegated file [chaos turn={wake}]'
    owner_turn = chaos.provider.script([
        fp.tool('update_task_progress', {'summary': 'I will report at the promised time.',
                                         'tasks': [{'title': title, 'status': 'pending', 'due': due}]}),
        fp.text('Recorded. [END_TURN: done]')])
    owner_text = f'Come back to me at the due time. [chaos turn={owner_turn}]'
    chaos.client.create_turn(lead, owner_text, workspace=project, first=False)
    chaos.assert_contract(lead, owner_text, outcomes={'completed'})
    owner_results = _results(chaos, owner_turn)
    assert len(owner_results) == 1 and '"error"' not in owner_results[0], owner_results

    # The commitment wake, a nonhuman Turn, runs every step up to the hold.
    _wait(lambda: chaos.provider.held.is_set(), 'the lead was never woken or stalled before the hold: '
          + '\n'.join(_results(chaos, wake)), WAKE_DEADLINE_S)
    results = _results(chaos, wake)
    dispatched, _poll, _status, follow_up, _poll2, review, settings, publish = (
        _json(text) for text in results[:8])
    assert 'error' not in dispatched and dispatched['assignment_id'], dispatched
    assert 'error' not in follow_up, follow_up
    assert 'error' not in review, review
    # Credentials/settings and external publication are not in a lead's reach: never executed.
    for refused, name in ((settings, 'owner_settings'), (publish, 'channel_outbox_approve')):
        assert refused.get('status') == 'error' and 'NOT executed' in refused['summary'], refused
        assert all(name not in request['tools'] for request in chaos.provider.requests(wake)), name
    history = _data(chaos.client.request('GET', f'/api/v4/router/owner-delegation/history?requester={lead}'))
    [record] = history['actions']
    assert (record['scope'], record['requester']['kind'], record['trigger']['kind']) == (
        'lead_authority', 'project_lead', 'owner_delegation'), record
    audit = (chaos.instance.root / 'logs' / 'audit.log').read_text(errors='replace')
    assert record['id'] in audit, 'the delegated action is in audit.log'

    # The Worker submitted its own checkpoint from its isolated workspace, twice.
    listing, submitted = _results(chaos, worker)[:2]
    assert 'worker-change' in listing and listing.split('\n')[0].strip().strip('"') != project, listing
    submit = _json(submitted)
    assert 'error' not in submit and submit.get('status') != 'error' and 'NOT executed' not in submitted, submitted
    assert 'steered' in _results(chaos, steered)[0], _results(chaos, steered)
    resubmit = _json(_results(chaos, steered)[1])
    assert 'error' not in resubmit and resubmit.get('status') != 'error', _results(chaos, steered)

    # Revocation refuses the very next act of the same Turn: dispatch, then restart.
    _revoke(chaos)
    release.set()
    chaos.settle(lead)
    second, restart = [_json(text) for text in _results(chaos, wake)[8:10]]
    assert second.get('status') == 'error' and 'nonhuman_request' in json.dumps(second), second
    assert 'owner_delegation_not_granted' in json.dumps(restart), restart
    assignments = json.dumps(_data(chaos.client.request(
        'GET', f'/api/v4/project/coordination/assignments?path={project}')))
    assert 'Second dispatch after revocation' not in assignments
    assert chaos.instance.api.poll() is None, 'the API host restarted'
    every = _data(chaos.client.request('GET', '/api/v4/router/owner-delegation/history'))['actions']
    assert not [item for item in every if item['scope'] == 'restart' and item['status'] != 'refused'], every
