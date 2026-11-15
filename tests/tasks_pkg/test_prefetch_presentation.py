"""Revocable presentation of a completed streaming prefetch."""
import json
import threading

import pytest

from lib.project_mod.config import build_root_policy, clear_conv_state
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator

pytestmark = pytest.mark.unit


@pytest.fixture
def task(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'note.txt').write_text('prefetch evidence')
    owner = {
        'id': f'early-prefetch-{tmp_path.name}',
        'convId': f'early-prefetch-chat-{tmp_path.name}',
        '_userId': 1, '_invocationStorageFree': True, 'aborted': False,
        'config': {'projectPath': str(root)},
        '_workspaceRootPolicy': build_root_policy(str(root)),
        'messages': [], 'toolRounds': [], 'events': [],
        'events_lock': threading.Lock(), '_tool_result_cache': {},
    }
    from lib.tasks_pkg.executor import _finalize as finalize
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    from lib.tasks_pkg.tool_dispatch import api as dispatch_api
    record = lambda _task, event: owner['events'].append(dict(event))
    monkeypatch.setattr(finalize, 'append_event', record)
    monkeypatch.setattr(pipeline, 'append_event', record)
    monkeypatch.setattr(dispatch_api, 'append_event', record, raising=False)
    yield owner
    clear_conv_state(owner['id'])


def _find_call(call_id='find-1'):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': 'find_files', 'arguments': json.dumps({'pattern': '*.txt'})}}


def _dispatch(task, accumulator, assistant):
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    parsed, _ = parse_tool_calls(
        assistant, task, round_num=1,
        tool_round_num=accumulator.tool_round_num, project_enabled=True,
        early_announced=accumulator.announced_tc_map)
    messages = [assistant]
    execute_tool_pipeline(
        task, parsed, task['config'], task['config']['projectPath'], True,
        None, messages, [], 1, 'test-model')
    return messages


def test_completed_prefetch_publishes_preview_before_model_adoption(task):
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    call = _find_call()
    try:
        accumulator.on_tool_call_ready(call)
        future = accumulator._futures['find-1'][0]
        assert future.result(timeout=85) is not None
        _rn, row = accumulator.announced_tc_map['find-1']
        assert row['status'] == 'executing'
        assert row['_speculativePresentation'] is True
        assert row['results'][0]['source'] == 'Prefetch'
        assert row['results'][0]['fetchedChars'] > 0
        assert not task['_tool_result_cache']
        assert not task['messages']
        assert 'toolContent' not in row
        assert not any(event['type'] == 'tool_complete' for event in task['events'])
        preview = [event for event in task['events']
                   if event['type'] == 'tool_result'][-1]
        assert preview['status'] == 'executing'
        assert preview['speculativePreview'] is True
    finally:
        accumulator.close()


def test_discarded_attempt_withdraws_provisional_result(task):
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    call = _find_call()
    try:
        accumulator.on_tool_call_ready(call)
        assert accumulator._futures['find-1'][0].result(timeout=85) is not None
        _rn, row = accumulator.announced_tc_map['find-1']
        assert row['_speculativePresentation'] is True
        assert accumulator.reconcile_announced_rounds({'tool_calls': []}) == 1
        assert row['status'] == 'aborted'
        assert row['_providerAttemptDiscarded'] is True
        assert all(result.get('badge') == 'superseded'
                   for result in row['results'])
    finally:
        accumulator.close()


def test_canonical_adoption_replaces_preview_and_completes_once(task):
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    call = _find_call()
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
    try:
        accumulator.on_tool_call_ready(call)
        assert accumulator._futures['find-1'][0].result(timeout=85) is not None
        assert accumulator.inject_into_cache(task) == 1
        _rn, row = accumulator.announced_tc_map['find-1']
        assert row['_speculativePresentation'] is True
        messages = _dispatch(task, accumulator, assistant)
        assert row['status'] == 'done'
        assert '_speculativePresentation' not in row
        assert row['results'][0]['source'] == 'Prefetch'
        assert 'note.txt' in row['toolContent']
        assert messages[-1]['role'] == 'tool'
        assert 'note.txt' in messages[-1]['content']
        assert row['toolCallId'] == 'find-1'
        completes = [event for event in task['events']
                     if event['type'] == 'tool_complete']
        assert len(completes) == 1
        assert completes[0]['toolCallId'] == 'find-1'
    finally:
        accumulator.close()


def test_late_parser_rejection_replaces_success_preview(task):
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    call = _find_call()
    try:
        accumulator.on_tool_call_ready(call)
        assert accumulator._futures['find-1'][0].result(timeout=85) is not None
        call['caller'] = {'type': 'unknown'}
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
        _dispatch(task, accumulator, assistant)
        _rn, row = accumulator.announced_tc_map['find-1']
        assert row['status'] == 'rejected'
        assert row.get('rejection')
        assert all(result.get('source') != 'Prefetch'
                   for result in row.get('results') or [])
        assert not any(message.get('role') == 'tool'
                       and str(message.get('content', '')).startswith(
                           'Files matching') for message in task['messages'])
    finally:
        accumulator.close()


def test_retirement_wins_against_late_worker_completion(task):
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    call = _find_call()
    release = threading.Event()
    entered = threading.Event()

    def delayed_read(_name, _args):
        entered.set()
        assert release.wait(2)
        return 'discarded evidence'

    accumulator._execute_one = delayed_read
    try:
        accumulator.on_tool_call_ready(call)
        assert entered.wait(10)
        accumulator.on_provider_attempt_restart(reason='test provider retry')
        release.set()
        assert accumulator._discarded_futures[0].result(timeout=85)[0] == 'discarded evidence'
        _rn, row = accumulator._discarded_announced[0][1:3]
        assert not any(event['type'] == 'tool_result'
                       and event.get('speculativePreview') for event in task['events'])
        assert accumulator.reconcile_announced_rounds({'tool_calls': []}) == 1
        assert row['status'] == 'aborted'
    finally:
        release.set()
        accumulator.close()


def test_duplicate_calls_replay_in_provider_order(task):
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    calls = [_find_call('repeat'), _find_call('repeat')]
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': calls}
    try:
        for call in calls:
            accumulator.on_tool_call_ready(call)
        for call in calls:
            assert accumulator._futures[call['id']][0].result(timeout=85) is not None
        accumulator.reconcile_announced_rounds(assistant)
        messages = _dispatch(task, accumulator, assistant)
        tool_messages = [message for message in messages if message.get('role') == 'tool']
        assert [message['tool_call_id'] for message in tool_messages] == [
            calls[0]['id'], calls[1]['id']]
        assert len(tool_messages) == 2
        assert len(task['toolRounds']) == 2
        assert len({row['toolCallId'] for row in task['toolRounds']}) == 2
        assert len([event for event in task['events']
                    if event['type'] == 'tool_complete']) == 2
    finally:
        accumulator.close()


def test_identity_repair_keeps_preview_on_one_reminted_occurrence(task):
    from lib.tasks_pkg.tool_dispatch import api as dispatch_api
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    call = _find_call()
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
    try:
        accumulator.on_tool_call_ready(call)
        assert accumulator._futures['find-1'][0].result(timeout=85) is not None
        accumulator.reconcile_announced_rounds(assistant)
        messages = [assistant]
        parsed, _ = __import__('lib.tasks_pkg.tool_dispatch._parse', fromlist=['parse_tool_calls']).parse_tool_calls(
            assistant, task, round_num=1,
            tool_round_num=accumulator.tool_round_num, project_enabled=True,
            early_announced=accumulator.announced_tc_map)
        task['toolRounds'].append({
            'roundNum': 0, 'toolCallId': 'find-1', 'toolName': 'find_files',
            'status': 'done', 'toolContent': 'historical result'})
        dispatch_api.repair_prepared_call_ids(
            task, parsed, 1, task['config']['projectPath'])
        reminted = parsed[0][2]
        assert reminted != 'find-1'
        row = parsed[0][5]
        assert row['_speculativePresentation'] is True
        assert len(task['toolRounds']) == 2
        dispatch_api.execute_tool_pipeline(
            task, parsed, task['config'], task['config']['projectPath'], True,
            None, messages, [], 1, 'test-model')
        assert row['toolCallId'] == reminted
        assert row['status'] == 'done'
        assert len(task['toolRounds']) == 2
        previews = [event for event in task['events']
                    if event['type'] == 'tool_result']
        assert previews[0]['toolCallId'] == 'find-1'
        assert previews[-1]['toolCallId'] == reminted
    finally:
        accumulator.close()
