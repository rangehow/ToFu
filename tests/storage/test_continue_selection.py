"""Read-only normal replay selection over one candidate-fenced scratch snapshot."""
import json
from pathlib import Path

import jsonschema
import pytest

from lib.agent_core.model_context_limits import MAX_ENTRY_BYTES
from lib.conversation_sync.resume_request_plan import read_cold_resume_candidate
from lib.storage.model_continuation import settled_witness
from lib.tasks_pkg.compaction._resume_projection import compaction_resume_witness
from lib.tasks_pkg.model_replay import policy_witness

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit


def test_continue_selection_uses_normal_proof_and_content_identity(semantic_authority, monkeypatch):
    factory, owner, _ = semantic_authority
    writer, reader = factory(write=True), factory(write=False)
    scope = dict(conversation_id='selection', user_id=owner)

    def command(operation, key, **payload):
        return writer.command(operation, scope | payload, key)

    def read(operation, **payload):
        return reader.query(operation, scope | payload)

    command('conversation.create', 'create', conv_id='selection', settings={})
    command('turn.append_settled', 'input', turn_id='input', command_id='input',
            actor='human', projection={'content': 'question'})
    command('turn.append_settled', 'publisher', turn_id='publisher', command_id='publisher',
            actor='assistant', projection={'content': 'earlier answer'})
    publisher = read('turn.context.read', turn_id='publisher', replay=False)
    source = command('turn.append_settled', 'source', turn_id='source', command_id='source',
                     actor='assistant', status='failed', projection={'content': 'partial'},
                     settlement={'outcome': 'failed', 'resumeOptions': ['continue']})['turn']
    context_query = dict(turn_id='source', replay=False)
    cold = read('turn.context.read', **context_query)
    pin = dict(attempt_id=source['currentAttemptId'], projection_revision=source['projectionRevision'],
               history_digest=cold['historyProof']['digest'])
    config = {'model': 'fixture'}

    mutate_inputs = None

    class ReadOnly:
        def query(self, operation, payload, **kwargs):
            assert operation in ('turn.context.read', 'turn.resume.plan.read')
            result = reader.query(operation, payload, **kwargs)
            if mutate_inputs and operation == 'turn.context.read':
                mutate_inputs(result)
            return result

    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: ReadOnly())

    def no_effect(*args, **kwargs):
        pytest.fail('detached selection entered a serving effect')

    monkeypatch.setattr('lib.tasks_pkg.model_replay._remember_context_activity', no_effect)
    monkeypatch.setattr('lib.observability.measure_task_startup', no_effect)
    monkeypatch.setattr('lib.tasks_pkg.model_images.retained_images_reproducible', no_effect)

    def plan(**opts):
        return read_cold_resume_candidate(**scope, turn_id='source', **pin, **opts)

    assert plan().archive_witness is None
    before = read('turn.sync.snapshot')
    absent = plan(selection_config=config)
    assert absent.archive_witness.status == absent.continuation_witness.status == 'verified_absent'
    assert absent.archive_witness.content_digest is None
    assert absent.readiness == 'unknown'
    def mutate_selection(mutate):
        return lambda result: mutate(result['continueSelectionInputs'])

    for mutation in (
        lambda result: result.pop('continueSelectionInputs'),
        mutate_selection(lambda inputs: inputs.pop('continuation')),
        mutate_selection(lambda inputs: inputs.pop('archives')),
        mutate_selection(lambda inputs: inputs.update(covered=False)),
        mutate_selection(lambda inputs: inputs.update(version=1.0)),
        mutate_selection(lambda inputs: inputs.update(extra=None)),
        mutate_selection(lambda inputs: inputs.update(continuation={})),
    ):
        mutate_inputs = mutation
        missing = plan(selection_config=config)
        assert missing.archive_witness.status == missing.continuation_witness.status == 'indeterminate'
        assert missing.messages_json == absent.messages_json
    mutate_inputs = mutate_selection(lambda inputs: inputs.update(archives=None))
    null_archive = plan(selection_config=config)
    assert null_archive.archive_witness.status == 'indeterminate'
    assert null_archive.continuation_witness.status == 'verified_absent'
    mutate_inputs = None

    checkpoint = {'turns': [settled_witness(publisher['turns'][-1])],
                  'historyProof': publisher['historyProof'], 'policy': policy_witness(config),
                  'messages': [{'role': 'assistant', 'content': 'retained prefix'}]}
    assert command('model_continuation.put', 'checkpoint', lane_id='main', projection=checkpoint)
    archive_config = config | {'_modelReplayHistoryProof': cold['historyProof']}
    receipt = {'status': 'completed', 'implementation': 'suspended_working_context',
               'resumeWitness': compaction_resume_witness({'_turnId': 'source', 'config': archive_config}, rounds=[])}
    command('compaction_archive.create', 'archive', archive_id='archive', messages=[], created_at_ms=10)
    command('compaction_archive.create', 'tied', archive_id='z', messages=[], created_at_ms=10)
    command('compaction_archive.update_summary', 'tied-post', archive_id='z', summary='',
            tokens_after=0, msgs_after=1, receipt=receipt,
            post_messages=[{'role': 'assistant', 'content': 'shorter id loses the tie'}])

    def update(key, content, **fields):
        command('compaction_archive.update_summary', key, archive_id='archive', summary='',
                tokens_after=0, msgs_after=1,
                post_messages=[{'role': 'assistant', 'content': content}], **fields)

    update('initial-post', 'old archive post', receipt=receipt)
    with_selection = read('turn.context.read', **context_query, continue_plan=pin | {'selection': True})
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/model_context_v1.schema.json').read_text())
    jsonschema.validate(with_selection, {'$ref': '#/$defs/result', '$defs': schema['$defs']})
    legacy = read('turn.context.read', **context_query, continue_plan=pin)
    assert {k: v for k, v in with_selection.items() if k != 'continueSelectionInputs'} == legacy
    assert with_selection['continueSelectionInputs']['continuation'] == checkpoint
    assert [row['id'] for row in read('compaction_archive.list')['archives']] == ['z', 'archive']
    assert [item['archive']['id'] for item in with_selection['continueSelectionInputs']['archives']] == [
        'archive', 'z',
    ]
    first = plan(selection_config=config)
    assert first.archive_witness.status == first.continuation_witness.status == 'selected'
    assert first.archive_witness.candidate_digest == first.continuation_witness.candidate_digest
    assert first.messages_json == absent.messages_json  # Evidence never replaces cold bytes.
    assert first.readiness == 'unknown'
    assert first.missing_witnesses == ('policy', 'tools', 'route_subscription', 'window')
    mutate_inputs = mutate_selection(lambda inputs: inputs['archives'][0]['archive'].update(payloadSize=42))
    assert plan(selection_config=config).archive_witness == first.archive_witness
    for mutation in (
        lambda inputs: inputs['archives'].reverse(),
        lambda inputs: inputs['archives'][0].pop('postMessages'),
        lambda inputs: inputs['archives'][0].update(postMessages=None),
        lambda inputs: inputs['archives'][0].update(postMessages=[None]),
        lambda inputs: inputs['archives'][0]['archive'].update(receipt=None),
    ):
        mutate_inputs = mutate_selection(mutation)
        malformed = plan(selection_config=config)
        assert malformed.archive_witness.status == 'indeterminate'
        assert malformed.continuation_witness == first.continuation_witness
    mutate_inputs = mutate_selection(lambda inputs: inputs['continuation'].update(messages=[None]))
    assert plan(selection_config=config).continuation_witness.status == 'indeterminate'
    mutate_inputs = mutate_selection(lambda inputs: inputs['continuation'].update(
        messages=[{'role': 'assistant', 'content': 'q' * MAX_ENTRY_BYTES}]))
    assert plan(selection_config=config).continuation_witness.status == 'indeterminate'
    mutate_inputs = None
    assert config == {'model': 'fixture'}
    assert not {'attempt', '_resumeSource', 'dispatchGrant'} & with_selection.keys()
    assert plan(selection_config={'model': 'different'}).archive_witness.status == 'indeterminate'
    update('replace-post-only', 'new archive post')  # no receipt change
    second = plan(selection_config=config)
    assert first.archive_witness.candidate_digest == second.archive_witness.candidate_digest
    assert first.archive_witness.content_digest != second.archive_witness.content_digest
    assert second.archive_witness.status == 'selected'
    assert first.continuation_witness == second.continuation_witness
    persisted = read('compaction_archive.get', archive_id='archive', include_post_messages=True)
    assert persisted['archive']['receipt'] == receipt
    assert persisted['postMessages'][0]['content'] == 'new archive post'
    assert read('turn.sync.snapshot') == before

    # Admissible post messages can exceed the hydrated archive response
    # budget once its receipt and metadata are included.
    update('oversized-hydration', 'x' * (MAX_ENTRY_BYTES - 100))
    budgeted = read('turn.context.read', **context_query, continue_plan=pin | {'selection': True})
    assert {k: v for k, v in budgeted.items() if k != 'continueSelectionInputs'} == legacy
    assert budgeted['continueSelectionInputs']['archives'] is None
    assert plan(selection_config=config).archive_witness.status == 'indeterminate'
    update('restore-small-post', 'new archive post')

    for i in range(7):
        command('compaction_archive.create', f'cap-{i}', archive_id=f'cap-{i}', messages=[])
    assert plan(selection_config=config).archive_witness.status == 'indeterminate'
