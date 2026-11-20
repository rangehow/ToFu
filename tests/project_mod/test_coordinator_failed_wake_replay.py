"""Coordinator wake diagnostics stay durable without repeating their model context."""
from copy import deepcopy

import pytest

from lib import turn_lifecycle
from lib.conversations.coordinator_continuation import HEADER
from lib.project_dispatch.continuation import CONTINUATION_BRIEF_MARK
from lib.project_dispatch.delegation import WORKER_INPUT_SOURCE_HEAD, WORKER_INPUT_SOURCE_TAIL
from lib.storage.model_continuation import ModelContinuationStore

pytestmark = pytest.mark.unit

BOARD = 'Open tasks on your board:\n- [in_progress] ' + 'DolphinsFS baseline ' * 270
WORKERS = 'Worker states that need you (settled, cancelled and delivered ones are omitted):\n'
RUNNING = WORKERS + '- worker-a: running — compare baselines'
REVIEW = WORKERS + '- worker-a: result to review (review_project_work) — compare baselines'
PROMISE = 'These promises you made to the owner on your task board are due:\n- "Ship" — due 12:30. '


def wake(rows, *, source='owner-a', subject='', workers=RUNNING, board=BOARD,
         failure='summary_dispatch_failed'):
    """The durable input and exact parent/reply evidence selected for model replay."""
    n = len(rows)
    attempt = f'attempt-{n}'
    text = ('Continue the request. ' + subject + CONTINUATION_BRIEF_MARK + '. '
            + WORKER_INPUT_SOURCE_HEAD + 'lead; Turn ' + source + WORKER_INPUT_SOURCE_TAIL
            + HEADER + board + '\n' + workers)
    rows += [
        {'turnId': f'input-{n}', 'actor': 'virtual_user', 'kind': 'input', 'laneId': 'main',
         'ordinal': n, 'status': 'completed', 'settlement': {'cause': 'orchestration_generated'},
         'projection': {'origin': {'initiator': 'brain'}, 'content': text}},
        {'turnId': f'reply-{n}', 'actor': 'assistant', 'kind': 'reply', 'laneId': 'main',
         'ordinal': n + 1, 'parentTurnId': f'input-{n}', 'currentAttemptId': attempt,
         'status': 'running' if failure is None else 'failed', 'projection': {'content': ''}},
    ]
    if failure is not None:
        rows[-1]['settlement'] = {'cause': 'generation_error', 'error': {
            'kind': 'internal', 'source': 'orchestrator', 'failureAttribution': {'roundNum': 1},
            'raw': ('Automatic context compaction failed locally before provider dispatch '
                    f'({failure}); the prompt remains 1,488,242 tokens. No main-provider request was sent.'),
        }}
        rows[-1]['projection']['activityTimeline'] = {'entries': [
            {'attemptId': attempt, 'reasonCode': 'context_compaction', 'trigger': 'dispatch_guard',
             'status': 'failed', 'roundNum': 0, 'detailArgs': {'code': failure}},
        ]}
    return text


@pytest.fixture
def replay(monkeypatch):
    def build(rows, *, prefix=None):
        from lib.tasks_pkg.model_replay import policy_witness
        before = deepcopy(rows)
        config = {'excludeLast': True}
        context = {'supported': True, 'turns': rows, 'historyProof': {}, 'continuation': None}
        if prefix:
            context.update(covered=2, continuation={
                'historyProof': {'version': 2}, 'turns': [['cached-anchor', 'revision']],
                'policy': policy_witness(config), 'messages': prefix, 'lastModelActivityAtMs': 1,
            })
        monkeypatch.setattr(ModelContinuationStore, 'read_context', lambda *_args, **_kwargs: context)
        monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda: object())
        result = turn_lifecycle.build_api_messages('lead', rows[-1]['turnId'],
            config, user_id=1, allow_model_replay=bool(prefix))
        assert rows == before, 'model replay rewrote canonical Turn data'
        if prefix:
            assert config['_modelReplayStatus'] == 'restored'
        return result
    return build


@pytest.mark.parametrize('failure', ['summary_dispatch_failed', 'summary_route_unavailable', 'typed'])
def test_repeated_undispatched_wakes_do_not_grow_the_model_request(replay, failure):
    rows = []
    for _ in range(6):
        if failure == 'typed':
            wake(rows, failure='summary_route_unavailable')
            rows[-1]['settlement']['error'] = {
                'origin': 'local', 'providerReached': False, 'failureStage': 'prompt_admission',
                'reason': 'summary_route_unavailable', 'failureAttribution': {'roundNum': 1},
            }
            rows[-1]['projection'].pop('activityTimeline')
        else:
            wake(rows, failure=failure)
    current = wake(rows, failure=None)
    assert [m['content'] for m in replay(rows)] == [current]


def test_changed_worker_evidence_and_a_due_promise_survive_cross_request_replay(replay):
    rows = []
    wake(rows, source='owner-a', subject=PROMISE, workers=REVIEW)
    wake(rows, source='owner-b')
    wake(rows, source='owner-a', workers=REVIEW)
    rows.append({'turnId': 'report', 'ordinal': len(rows), 'status': 'completed',
                 'actor': 'assistant', 'kind': 'coordinator_report.progress', 'laneId': 'main',
                 'projection': {'content': 'human-facing report'}})
    current = wake(rows, source='owner-b', failure=None)
    messages = replay(rows)
    assert messages[-1]['content'] == current
    text = '\n'.join(m['content'] for m in messages)
    assert text.count(BOARD) == 1
    assert PROMISE in text and 'Turn owner-a' in text
    assert 'result to review (review_project_work) — compare baselines' in text
    assert 'human-facing report' not in text


def test_warm_checkpoint_is_untouched_while_the_wake_suffix_is_folded(replay):
    prefix = [{'role': 'user', 'content': 'original owner request'},
              {'role': 'assistant', 'content': 'successful reply'}]
    rows = []
    wake(rows)
    current = wake(rows, failure=None)
    messages = replay(rows, prefix=deepcopy(prefix))
    assert messages[:-1] == prefix
    assert messages[-1]['content'] == current


def test_regenerating_a_failed_wake_keeps_its_current_full_input(replay):
    rows = []
    wake(rows)
    current = wake(rows)
    assert [m['content'] for m in replay(rows)] == [current]


def test_an_intervening_owner_input_starts_a_new_sequence(replay):
    rows = []
    previous = wake(rows)
    rows.append({'turnId': 'new-request', 'ordinal': len(rows), 'status': 'completed',
                 'actor': 'human', 'kind': 'input', 'laneId': 'main',
                 'projection': {'content': 'new owner instructions'}})
    current = wake(rows, failure=None)
    assert [m['content'] for m in replay(rows)] == [previous, 'new owner instructions', current]


@pytest.mark.parametrize('unproven', ['human', 'provider', 'partial', 'tool', 'prior_attempt',
                                     'later_round', 'parent', 'user_steer', 'quote'])
def test_unproven_pairs_keep_their_original_input(replay, unproven):
    rows = []
    previous = wake(rows)
    row, reply = rows
    if unproven == 'human':
        row['actor'] = 'human'
    elif unproven == 'provider':
        reply['settlement']['error']['raw'] = 'Provider refused request'
    elif unproven == 'partial':
        reply['projection']['content'] = 'Actual assistant output'
    elif unproven == 'tool':
        reply['projection']['toolRounds'] = [{'toolName': 'get_project_workers', 'toolContent': 'result'}]
    elif unproven == 'prior_attempt':
        reply['projection']['activityTimeline']['entries'][0]['attemptId'] = 'not-current'
    elif unproven == 'later_round':
        reply['projection']['activityTimeline']['entries'][0]['roundNum'] = 1
    elif unproven == 'parent':
        reply['parentTurnId'] = 'wrong-parent'
    elif unproven == 'user_steer':
        reply['projection']['_userSteerInjects'] = [{'text': 'New instructions from owner'}]
    else:
        row['projection']['replyQuote'] = 'Referenced evidence'
    current = wake(rows, failure=None)
    messages = replay(rows)
    assert messages[0]['content'].endswith(previous)
    if unproven == 'quote':
        assert 'Referenced evidence' in messages[0]['content']
    assert messages[-1]['content'] == current
