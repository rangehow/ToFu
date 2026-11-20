"""Synthetic producer facts through the existing Coordinator tool-collection seam.

The scripts choose calls and board updates; they do not measure model judgment.
The independent observation compares the delivered objective with result facts,
not the producer's identical success claim or the offline QA rubric's answers.
"""
from copy import deepcopy
from importlib import import_module
import json

import pytest

from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from tests.project_mod.test_coordinator_progress import board as board
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.tasks_pkg.test_coordinator_tool_surface import assemble
from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc

__all__ = ('base_runtime', 'context')
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('samples,measurements,board_status,missing,wrong_units', [
    (['alpha', 'beta'], [{'sample': 'alpha', 'value': 12, 'unit': 'ms'},
                        {'sample': 'beta', 'value': 8, 'unit': 'ms'}], 'completed', [], []),
    (['alpha', 'beta'], [{'sample': 'alpha', 'value': 12, 'unit': 'ms'}], 'in_progress', ['beta'], []),
    (['alpha'], [{'sample': 'alpha', 'value': 12, 'unit': 'ms'}], 'completed', [], []),
    (['alpha', 'beta'], [{'sample': 'alpha', 'value': 12, 'unit': 'ms'},
                        {'sample': 'beta', 'value': 8, 'unit': 's'}], 'in_progress', [], ['beta']),
    (['alpha', 'beta'], [{'sample': 'alpha', 'value': 12, 'unit': 'ms'},
                        {'sample': 'alpha', 'value': 12, 'unit': 'ms'},
                        {'sample': 'beta', 'value': 8, 'unit': 'ms'}], 'in_progress', ['alpha'], []),
], ids=['complete-facts', 'missing-sample', 'narrower-objective', 'wrong-unit', 'duplicate-sample'])
def test_producer_facts_are_compared_to_the_objective_shipped_through_dispatch(
        runtime, board, monkeypatch, samples, measurements, board_status, missing, wrong_units):
    from lib.conversations import coordinator_progress
    from lib.llm import astream
    import lib.llm as llm

    r = runtime
    r.c.repo.client = board.store
    monkeypatch.setattr(coordinator_progress, 'CoordinatorTaskProgress',
                        lambda owner, path, **kwargs: board.factory(owner, path))
    monkeypatch.setattr(r.c.repo, 'workspace_get', lambda *a, **kw: None)
    events = []
    def record_event(task, event):
        assert task is r.task
        events.append(deepcopy(event))
        return True

    for target in ('lib.tasks_pkg.manager.append_event',
                   'lib.tasks_pkg.executor._finalize.append_event',
                   'lib.tasks_pkg.tool_dispatch._pipeline.append_event',
                   'lib.tasks_pkg.tool_dispatch._heartbeat.append_event'):
        monkeypatch.setattr(target, record_event)
    monkeypatch.setattr('lib.channels.tools.visible_tools', lambda *a, **kw: [])

    def no_model(*args, **kwargs):
        pytest.fail('this deterministic collection trace must never call a model')

    monkeypatch.setattr(llm, 'stream_chat', no_model)
    monkeypatch.setattr(astream, 'async_stream_chat', no_model)
    monkeypatch.setattr(import_module('lib.llm.chat'), 'chat', no_model)
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    catalog = assemble(r.task, tools={'resultEnvelope': 'legacy'})
    messages, rounds, inputs = [], [], []
    create_turn = r.service.commands.create_turn

    def capture_input(conversation, owner, request, **kwargs):
        inputs.append(deepcopy(request['inputTurn']))
        return create_turn(conversation, owner, request, **kwargs)

    monkeypatch.setattr(r.service.commands, 'create_turn', capture_input)

    def collect(name, arguments):
        call_id = f'synthetic-{len(rounds)}'
        parsed = _mk_tc(call_id, name, len(rounds) + 1, args=arguments)
        parsed[0]['function']['arguments'] = json.dumps(arguments)
        rounds.append(parsed[5])
        r.task['toolRounds'] = rounds
        messages.append({'role': 'assistant', 'content': None, 'tool_calls': [parsed[0]]})
        before = len(messages)
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              catalog, messages, [], len(rounds) - 1, 'model-a')
        assert parsed[5]['status'] == 'done', messages[before:]
        reply, = messages[before:]
        assert reply['tool_call_id'] == call_id
        return json.loads(reply['content'])

    title = 'Measure alpha and beta latency'
    objective = {'samples': samples, 'unit': 'ms', 'records_per_sample': 1}
    brief = json.dumps(objective, sort_keys=True)
    collect('update_task_progress', {'summary': 'Synthetic measurement request', 'tasks': [
        {'title': title, 'status': 'in_progress'}]})
    board_before = collect('update_task_progress', {'read': {}})
    delegated = collect('delegate_project_work', {
        'title': title, 'brief': brief, 'idempotency_key': 'synthetic-measurements',
        'board_task_keys': board_before['taskKeys']})
    assert delegated['dispatch'] == 'accepted', delegated
    row, = r.c.repo.list()
    assert row['id'] == delegated['assignment_id']
    assert row['request']['brief'] == brief
    assert row['request']['boardTaskKeys'] == board_before['taskKeys']
    assert len(inputs) == len(r.created) == len(r.calls) == 1
    shipped, _ = json.JSONDecoder().raw_decode(inputs[0]['content'])
    assert shipped == objective

    # Only the disposable producer registers are populated; no executor/model runs.
    producer = {'claim': 'All requirements met.', 'measurements': measurements}
    association = row['execution']
    r.turns[association['turnId']].update(
        projectionRevision=2, projection={'content': json.dumps(producer, sort_keys=True)})
    r.attempts[association['attemptId']]['status'] = 'completed'
    polled = collect('poll', {'ids': [row['id']], 'wait_s': 0})
    reported, = polled['workers']
    assert reported['assignment_id'] == row['id']
    view = collect('get_project_assignment', {'assignment_id': row['id']})
    assert view['execution']['status'] == 'completed'
    assert view['execution']['resultRevision'] == 2
    observed = json.loads(view['execution']['result']['content'])
    assert observed == producer == json.loads(reported['result'])
    assert view['request']['brief'] == brief
    assert view['review'] is None and view['canonicalDelivery'] is False

    collect('update_task_progress', {'changes': [{
        'title': title, 'status': board_status,
        'progress': 'Synthetic result checked against the shipped samples and units.'}]})
    current = collect('update_task_progress', {'read': {}})
    task, = current['tasks']
    assert current['taskKeys'] == board_before['taskKeys']
    assert task['status'] == board_status
    assert board.factory().read()['board']['tasks'] == current['tasks']

    # This oracle is not used to choose any scripted call, result or board update.
    facts = observed['measurements']
    absent = [sample for sample in shipped['samples']
              if sum(fact['sample'] == sample for fact in facts) != shipped['records_per_sample']]
    unit_mismatches = [fact['sample'] for fact in facts if fact['unit'] != shipped['unit']]
    assert absent == missing and unit_mismatches == wrong_units
    assert {fact['sample'] for fact in facts} <= set(shipped['samples'])
    supported = not absent and not unit_mismatches
    assert (task['status'] == 'completed') is supported
    assert observed['claim'] == 'All requirements met.'
    if not supported:
        assert len(facts) != len(shipped['samples']) or unit_mismatches
    assert [item['toolName'] for item in rounds] == [
        'update_task_progress', 'update_task_progress', 'delegate_project_work',
        'poll', 'get_project_assignment', 'update_task_progress', 'update_task_progress']
    assert all(item['executionEffect']['state'] == 'returned' for item in rounds)
    assert not any(event['type'] == 'human_guidance_request' for event in events)
    assert '_ownerGuidanceAnswers' not in r.task
    assert len(r.created) == len(r.calls) == 1
    assert not r.c.repo._load()[0].get('coordinatorSettlements')
