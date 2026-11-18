"""Delegated execution receipts must not become invented provider actions."""

from copy import deepcopy
import json

import pytest

from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix, checkpoint_retention_positions
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

pytestmark = pytest.mark.unit


def _rounds(source='execute_calls'):
    scope = {'attemptId': 'attempt-one', 'taskId': 'task-one', 'llmRound': 1}
    parent = {**scope, 'toolName': 'execute_tools', 'toolCallId': 'gateway-one',
              '_hiddenToolAdapter': True, 'roundNum': 2, 'status': 'done',
              'toolArgs': json.dumps({'calls': [{'name': 'run_command',
                          'call_id': '1', 'arguments': {'command': 'synthetic'}}]}),
              'toolContent': json.dumps({'results': [{'call_id': '1',
                             'resultId': 'result-one', 'output': 'actual result'}]})}
    child = {**scope, 'source': source, 'toolName': 'code_exec',
             'toolCallId': '1', 'roundNum': 8700001, 'status': 'done',
             'toolArgs': '{"command":"synthetic"}', 'toolContent': 'actual result',
             'toolResultEvidence': {'resultId': 'result-one'}}
    return [parent, child]


@pytest.mark.parametrize('source', ['execute_calls', 'execute_program'])
def test_covered_child_receipt_is_not_replayed_as_another_assistant_call(source):
    rounds = _rounds(source)
    original = deepcopy(rounds)
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.blocked_position is None
    assert prefix.positions == (0,)
    assert prefix.ignored_count == 1
    assert prefix.delegated_receipt_count == 1
    assert checkpoint_retention_positions(rounds, prefix)[1] == [0, 1]
    wire = _reconstruct_tool_call_messages(rounds)
    assert [call['function']['name'] for message in wire
            for call in message.get('tool_calls', [])] == ['execute_tools']
    assert len(wire) == 2 and 'actual result' in wire[1]['content']
    assert rounds == original


@pytest.mark.parametrize('change', ['attempt', 'task', 'llm_round', 'result_id',
                                   'native_call', 'missing_scope', 'no_parent',
                                   'parent_not_container', 'compacted_parent',
                                   'malformed_source'])
def test_independent_or_unproven_receipts_are_never_deduplicated(change):
    rounds = _rounds()
    child = rounds[1]
    if change == 'attempt': child['attemptId'] = 'another-attempt'
    if change == 'task': child['taskId'] = 'another-task'
    if change == 'llm_round': child['llmRound'] = 2
    if change == 'result_id': child['toolResultEvidence']['resultId'] = 'another-result'
    if change == 'native_call': child['source'] = 'native_direct'
    if change == 'missing_scope':
        for row in rounds:
            row.pop('taskId'); row.pop('attemptId')
    if change == 'no_parent': rounds = [child]
    if change == 'parent_not_container': rounds[0].pop('_hiddenToolAdapter')
    if change == 'compacted_parent': rounds[0]['toolContent'] = '{"artifactRef":"retained"}'
    if change == 'malformed_source': child['source'] = {'unknown': True}
    assert len(scan_replayable_tool_round_prefix(rounds).rounds) == len(rounds)


def _artifact_envelope(projected, *, truncated):
    from lib.tools.result_projection_text import text_section
    head = {'status': 'partial' if truncated else 'error', 'artifactRef': 'tr:batch:1'}
    if truncated:
        head['truncated'] = True
        projected = projected[:len(projected) // 2]
    return json.dumps(head, separators=(',', ':')) + '\n' + text_section('summary', projected)


def _model_projected_rounds(output='{"action_id": "od-1",\n "status": "accepted"}', *,
                            envelope=None):
    """The container as recorded live: its toolContent is the model-only projection."""
    from lib.tools.gateway_result_projection import project_gateway_result
    scope = {'attemptId': 'attempt-one', 'taskId': 'task-one', 'llmRound': 41}
    canonical = json.dumps({'status': 'ok', 'warnings': [], 'results': [{
        'call_id': 'gw_restart', 'name': 'restart_server', 'status': 'done',
        'approval': {'required': False, 'status': 'not_required'}, 'duration': 1363,
        'source': 'execute_calls', 'output': output, 'resultId': 'result-one',
        'output_truncated': False, 'raw_output_bytes': 64, 'output_bytes': 64}]})
    projected = project_gateway_result('execute_tools', canonical)
    if envelope is not None:
        projected = _artifact_envelope(projected, truncated=envelope == 'truncated')
    parent = {**scope, 'toolName': 'execute_tools', 'toolCallId': 'toolu_container',
              '_hiddenToolAdapter': True, 'roundNum': 87, 'status': 'done',
              'source': 'native_direct', 'toolContent': projected,
              'toolArgs': json.dumps({'calls': [{'name': 'restart_server', 'arguments': {}}]})}
    child = {**scope, 'source': 'execute_calls', 'toolName': 'restart_server',
             'toolCallId': 'gw_restart', 'roundNum': 8700001, 'status': 'done',
             'toolArgs': '{}', 'toolContent': output,
             'toolResultEvidence': {'resultId': 'result-one'}}
    return [parent, child]


@pytest.mark.parametrize('output', ['{"action_id": "od-1",\n "status": "accepted"}',
                                    'plain text', 'body that embeds\n\n## results[1]\n{}'])
def test_model_projected_container_still_covers_its_delegated_receipt(output):
    """execute_tools results reach the model as a framed projection without resultId.

    Coverage must not depend on the container text being canonical JSON:
    otherwise every gateway child is replayed as a call the model never made,
    and a suspended view (which holds only the container) never checkpoints.
    """
    rounds = _model_projected_rounds(output)
    assert 'result-one' not in rounds[0]['toolContent']
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.positions == (0,) and prefix.delegated_receipt_count == 1
    wire = _reconstruct_tool_call_messages(rounds)
    assert [call['id'] for message in wire
            for call in message.get('tool_calls', [])] == ['toolu_container']


def test_untruncated_artifact_summary_is_the_exact_receipt():
    rounds = _model_projected_rounds(envelope='complete')
    assert scan_replayable_tool_round_prefix(rounds).positions == (0,)
    clipped = _model_projected_rounds(envelope='truncated')
    assert scan_replayable_tool_round_prefix(clipped).positions == (0, 1)


@pytest.mark.parametrize('change', ['call_id', 'attempt', 'llm_round', 'native_call',
                                   'truncated_projection'])
def test_model_projected_container_never_covers_an_unlisted_receipt(change):
    rounds = _model_projected_rounds()
    parent, child = rounds
    if change == 'call_id': child['toolCallId'] = 'gw_other'
    if change == 'attempt': child['attemptId'] = 'another-attempt'
    if change == 'llm_round': child['llmRound'] = 42
    if change == 'native_call': child['source'] = 'native_direct'
    if change == 'truncated_projection': parent['toolContent'] = parent['toolContent'][:-3]
    assert len(scan_replayable_tool_round_prefix(rounds).rounds) == 2


def test_parent_receipt_covers_incomplete_child_display_without_a_causal_gap():
    rounds = _rounds()
    rounds[1].pop('toolContent')
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.positions == (0,) and prefix.blocked_position is None
    rounds[0].pop('toolContent')
    assert scan_replayable_tool_round_prefix(rounds).blocked_position == 0


def test_continue_retains_child_audit_but_sends_only_the_provider_container():
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
    config = _resume_executor_config('continue', {
        'content': 'partial response', 'toolRounds': _rounds()})
    assert len(config['checkpointToolRounds']) == 2
    prepared = prepare_resume_state(config)
    assert len(prepared.checkpoint_tool_rounds) == 2
    assert len(prepared.checkpoint_messages) == 2
    calls = prepared.checkpoint_messages[0]['tool_calls']
    assert len(calls) == 1 and calls[0]['function']['name'] == 'execute_tools'
