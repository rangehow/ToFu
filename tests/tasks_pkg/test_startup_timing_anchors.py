"""Startup evidence is content-free and does not alter task authority."""
import json
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import observability
from lib.tasks_pkg import spawn

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def test_startup_anchors_pair_identity_and_use_monotonic_duration(monkeypatch):
    records = []
    monkeypatch.setattr(observability.logger, 'info', lambda fmt, value: records.append(json.loads(value)))
    clock = iter([10.0, 10.125])
    wall = iter([100.0, 90.0])  # Wall-clock adjustment cannot change duration.
    monkeypatch.setattr(observability.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(observability.time, 'time', lambda: next(wall))
    observe = Mock()
    monkeypatch.setattr(observability._STORE, 'observe', observe)
    with observability.measure_task_startup('workspace_binding', identity={
            'task_id': 'task', 'conversation_id': 'conv', 'attempt_id': 'attempt',
            'turn_id': 'turn', 'user_id': 7, 'config': 'private-body',
            'trace_id': 'not-admitted'}):
        assert len(records) == 1
        assert records[0]['boundary'] == 'start'
    assert records[1]['durationMs'] == 125.0
    assert records[1]['outcome'] == 'success'
    assert records[1]['observedAtMs'] == 90000
    assert records[0]['attempt_id'] == records[1]['attempt_id'] == 'attempt'
    assert 'private-body' not in json.dumps(records)
    assert 'trace_id' not in records[0]
    assert observe.call_args.kwargs == {'stage': 'workspace_binding', 'outcome': 'success'}


def test_timing_sink_failure_preserves_base_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('sink unavailable')
    monkeypatch.setattr(observability.logger, 'info', fail)
    monkeypatch.setattr(observability._STORE, 'observe', fail)
    failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with observability.measure_task_startup('context', identity={'task_id': 'task'}):
            raise failure
    assert caught.value is failure


def test_slow_workspace_admission_has_phase_before_binding_and_still_fails_closed(monkeypatch):
    order = []
    task = {'id': 'startup-task', 'convId': 'startup-conv', 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: order.append(('phase', fields['detailKey'])))
    failure = RuntimeError('workspace admission refused')
    def bind(task):
        assert task['status'] == 'running'
        assert order == [('phase', 'stream.phase.workerStarting')]
        order.append(('binding', 'entered'))
        raise failure
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace', bind)
    started = threading.Event()
    with pytest.raises(RuntimeError) as caught:
        spawn._mark_worker_started(task, started)
    assert caught.value is failure
    assert not started.is_set()  # Never grant the runner access before binding.
    assert order[-1] == ('binding', 'entered')


def test_workspace_phase_retires_only_after_binding_succeeds(monkeypatch):
    phases = []
    task = {'id': 'workspace-ready', 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append(fields['detailKey']))
    started = threading.Event()

    def bind(task):
        assert phases == ['stream.phase.workerStarting']

        from lib.tasks_pkg.workspace_delivery import _emit_workspace_preparing
        _emit_workspace_preparing(task)
        _emit_workspace_preparing(task)  # One notice even for multiple roots.
        assert not started.is_set()

    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace', bind)
    monkeypatch.setattr('lib.conversations.project_brain.note_isolated_workspace_signal',
                        lambda *args: None)
    assert spawn._mark_worker_started(task, started)
    assert started.is_set()
    assert phases == ['stream.phase.workerStarting', 'stream.phase.workspacePreparing',
                      'stream.phase.workerStarting']


def test_worker_entry_attributes_every_step_around_workspace_binding(monkeypatch):
    """Authority, durable start, running transition and the Project work signal."""
    records = _startup_records(monkeypatch)
    task = {'id': 'entry-task', 'convId': 'entry-conv', '_turnId': 'entry-turn',
            '_attemptId': 'entry-attempt', '_userId': 7, 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.turn_lifecycle.mark_task_started',
                        lambda *args, **kwargs: {'status': 'running', 'startedAt': 1000})
    monkeypatch.setattr('lib.agent_core.events.emit_phase', lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace',
                        lambda task: None)
    monkeypatch.setattr('lib.conversations.project_brain.note_isolated_workspace_signal',
                        lambda *args: '')
    assert spawn._mark_worker_started(task)
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('worker_entry', 'start'),
        ('worker_entry_authority', 'start'), ('worker_entry_authority', 'complete'),
        ('worker_entry_start', 'start'), ('worker_entry_start', 'complete'),
        ('worker_entry_running', 'start'), ('worker_entry_running', 'complete'),
        ('workspace_binding', 'start'), ('workspace_binding', 'complete'),
        ('worker_entry_signal', 'start'), ('worker_entry_signal', 'complete'),
        ('worker_entry', 'complete')]
    assert {record['attempt_id'] for record in records} == {'entry-attempt'}
    assert task['_workerStartedAt'] == 1


def test_no_workspace_keeps_ordinary_worker_start_phase(monkeypatch):
    phases = []
    task = {'id': 'no-workspace', 'status': 'pending', 'config': {}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append(fields['detailKey']))
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace',
                        Mock(side_effect=AssertionError('No workspace should be bound')))
    assert spawn._mark_worker_started(task)
    assert phases == ['stream.phase.workerStarting']


@pytest.mark.parametrize('stage', [
    'input_build', 'turn_persist', 'executor_start', 'acknowledgement',
    'input_translation', 'startup_config', 'startup_resume', 'startup_project',
    'startup_tools', 'startup_resume_view', 'startup_memory', 'startup_context',
    'startup_history',
    'memory_list', 'memory_rank', 'memory_hydrate',
    'resume_view_read', 'resume_view_witness', 'resume_view_verify',
    'registration_route', 'registration_mint', 'registration_checkpoint',
    'context_read', 'context_rebuild', 'startup_context_compose', 'startup_context_guidance',
    'preparation_account', 'preparation_abort_fence', 'preparation_native',
    'claim_wake', 'claim_horizon', 'claim_next', 'claim_proof', 'claim_accepted',
    'dispatch', 'dispatch_binding', 'dispatch_entry', 'request_policy',
    'worker_entry_authority', 'worker_entry_start', 'worker_entry_running',
    'worker_entry_signal',
])
def test_command_and_worker_startup_stages_keep_their_names(monkeypatch, stage):
    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda fmt, value: records.append(json.loads(value)))
    with observability.measure_task_startup(stage, identity={'task_id': 'task'}):
        pass
    assert [record['stage'] for record in records] == [stage, stage]


_IDENTITY_KEYS = ('conversation_id', 'user_id', 'turn_id', 'attempt_id', 'task_id')


def _startup_records(monkeypatch):
    records = []

    def capture(fmt, *args):
        if fmt == '[TaskStartupTiming] %s':
            records.append(json.loads(args[0]))
    monkeypatch.setattr(observability.logger, 'info', capture)
    return records


def _identity(record):
    return {key: record[key] for key in _IDENTITY_KEYS if key in record}


def test_resume_view_splits_into_reads_and_witness_digests(monkeypatch):
    """A resumed view's reads and source digests are separately attributable."""
    from lib.tasks_pkg.compaction._resume_projection import restore_compacted_resume

    reads = []

    class Store:
        def list_compaction_archives(self, conversation, *, user_id, limit):
            reads.append('list')
            return [{'id': 'archive-1', 'resultStatus': 'completed'}]

        def get_compaction_archive(self, conversation, archive_id, **kwargs):
            reads.append(('get', kwargs['include_post_messages']))
            return {'archive': {'receipt': {'resumeWitness': {
                'version': 1, 'turnId': 'turn', 'policyDigest': 'other-policy'}}}}

    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', Store)
    records = _startup_records(monkeypatch)
    task = {'id': 'task', 'convId': 'conv', '_userId': 7, '_turnId': 'turn'}
    with observability.measure_task_startup(
            'startup_resume_view', identity={'task_id': 'task'}):
        restored = restore_compacted_resume(
            task, {'_modelReplayHistoryProof': {'turns': 1}}, [],
            current_messages=[])

    assert restored is None
    assert task['_compactionResumeOutcome']['reason'] == 'settings_changed'
    assert reads == ['list', ('get', False)]
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('startup_resume_view', 'start'),
        ('resume_view_witness', 'start'), ('resume_view_witness', 'complete'),
        ('resume_view_read', 'start'), ('resume_view_read', 'complete'),
        ('resume_view_read', 'start'), ('resume_view_read', 'complete'),
        ('resume_view_witness', 'start'), ('resume_view_witness', 'complete'),
        ('startup_resume_view', 'complete')]
    assert {record['task_id'] for record in records} == {'task'}


def test_identity_created_inside_a_stage_joins_its_completion_not_its_start(monkeypatch):
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('turn_persist', identity={
            'conversation_id': 'conv', 'user_id': 7}) as bind_identity:
        bind_identity({'turn_id': 'turn', 'attempt_id': 'attempt', 'config': 'private-body'})
        with observability.measure_task_startup('context'):
            pass
    command = {'conversation_id': 'conv', 'user_id': '7'}
    turn = {**command, 'turn_id': 'turn', 'attempt_id': 'attempt'}
    assert [(r['stage'], r['boundary'], _identity(r)) for r in records] == [
        ('turn_persist', 'start', command),
        ('context', 'start', turn), ('context', 'complete', turn),
        ('turn_persist', 'complete', turn)]
    assert 'private-body' not in json.dumps(records)


def test_blank_or_missing_identity_is_omitted_never_invented(monkeypatch):
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('startup_config', identity={
            'task_id': 'child-task', 'conversation_id': 'conv', 'turn_id': '',
            'attempt_id': '  ', 'user_id': None}) as bind_identity:
        bind_identity({'turn_id': '', 'attempt_id': None})
    assert [_identity(record) for record in records] == [
        {'conversation_id': 'conv', 'task_id': 'child-task'}] * 2
    assert 'unknown' not in json.dumps(records)


def test_a_stage_without_identity_stays_silent_after_late_identity(monkeypatch):
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('context') as bind_identity:
        bind_identity({'turn_id': 'turn'})
    assert records == []  # A completion without its start would break pairing.


def _command_service(monkeypatch, persist):
    from lib.conversation_sync import command_service as commands
    monkeypatch.setattr(commands, '_capture_task_principal_tenant',
                        lambda config, *_args: dict(config))
    monkeypatch.setattr(commands, 'create_turn_pair', persist)
    monkeypatch.setattr(commands, 'get_turn', lambda *_a, **_k: {'turnId': 'turn-output'})
    monkeypatch.setattr(commands, 'get_attempt', lambda *_a, **_k: {'attemptId': 'attempt-1'})
    monkeypatch.setattr(commands, 'get_conversation_revision', lambda *_a, **_k: 3)
    return commands.ConversationTurnCommandService(
        build_user_message=lambda *_args: {'role': 'user', 'content': 'hello'},
        was_aborted_after=lambda *_args: False,
        start_task=lambda *_args: ('', None))


def _create_turn(service):
    return service.create_turn('conv', 7, {
        'commandId': 'command', 'message': {'text': 'hello'}, 'config': {}},
        request_started_at=1)


def test_turn_command_anchors_carry_the_turn_from_its_persist_completion(monkeypatch):
    records = _startup_records(monkeypatch)
    service = _command_service(monkeypatch, lambda *_a, **_k: {
        '_needsStart': False, 'turn': {'turnId': 'turn-output'},
        'attempt': {'attemptId': 'attempt-1'}})
    _create_turn(service)
    # Input building and the persist start precede the Turn: no identity is
    # invented for them; a log join pairs them through the persist completion.
    command = {'conversation_id': 'conv', 'user_id': '7'}
    turn = {**command, 'turn_id': 'turn-output', 'attempt_id': 'attempt-1'}
    assert [(r['stage'], r['boundary'], _identity(r)) for r in records] == [
        ('input_build', 'start', command), ('input_build', 'complete', command),
        ('turn_persist', 'start', command), ('turn_persist', 'complete', turn),
        ('executor_start', 'start', turn), ('executor_start', 'complete', turn),
        ('acknowledgement', 'start', turn), ('acknowledgement', 'complete', turn)]


@pytest.mark.parametrize('failure, outcome', [
    (RuntimeError('storage unavailable'), 'error'),
    (('stale_projection', 'stale', {}), 'error'),
    # A busy lane defers the input to its live Turn (steer/queue) or to the
    # caller; the conflict names that live Turn, not a Turn this command made.
    (('lane_busy', 'busy', {'turnId': 'live-turn',
                            'currentAttemptId': 'live-attempt'}), 'deferred'),
], ids=['storage-error', 'other-conflict', 'lane-busy'])
def test_an_unpersisted_turn_command_names_no_turn(monkeypatch, failure, outcome):
    from lib.conversation_sync.command_service import LifecycleConflict
    if isinstance(failure, tuple):
        failure = LifecycleConflict(*failure)
    records = _startup_records(monkeypatch)

    def persist(*_args, **_kwargs):
        raise failure
    with pytest.raises(type(failure)) as caught:
        _create_turn(_command_service(monkeypatch, persist))
    assert caught.value is failure
    completions = [r for r in records
                   if r['stage'] == 'turn_persist' and r['boundary'] == 'complete']
    assert [(r['outcome'], _identity(r)) for r in completions] == [
        (outcome, {'conversation_id': 'conv', 'user_id': '7'})]


def test_a_deferral_classifier_never_replaces_the_stage_failure(monkeypatch):
    records = _startup_records(monkeypatch)
    warnings = []
    monkeypatch.setattr(observability.logger, 'warning', lambda fmt, *args, **kwargs:
                        warnings.append((fmt % args, kwargs.get('exc_info'))))
    failure = KeyError('persist failed')

    def broken(error):
        raise RuntimeError('classifier bug')
    with pytest.raises(KeyError) as caught:
        with observability.measure_task_startup(
                'turn_persist', identity={'task_id': 'task'}, deferral=broken):
            raise failure
    assert caught.value is failure
    assert records[-1]['outcome'] == 'error'
    # The classifier's own bug stays visible instead of being swallowed.
    assert warnings == [('[TaskPhase] deferral classifier of stage turn_persist failed', True)]


def test_memory_prefetch_splits_startup_memory_into_nested_stages(monkeypatch):
    from lib.memory.prefetch import run_memory_prefetch

    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda fmt, value: records.append(json.loads(value)))
    summary = {
        'id': 'parser-state', 'name': 'parser-state-rollback',
        'description': 'ParserState rollback rule in lib/parser/state.py',
        'tags': ['parser'], 'enabled': True, 'eligible': True,
        'is_package': False, 'scope': 'project',
    }
    def resident(project_path, extra_paths=None, details=None):
        details.update(listing='resident', snapshotAgeMs=42.0)
        return [dict(summary)]

    monkeypatch.setattr('lib.memory.storage.resident_eligible_memories',
                        resident)
    monkeypatch.setattr('lib.memory.storage.load_eligible_memories',
                        lambda *args, **kwargs: [dict(summary, body='rule')])

    with observability.measure_task_startup(
            'startup_memory', identity={'task_id': 'task', 'turn_id': 'turn'}):
        selected = run_memory_prefetch(
            [{'role': 'user', 'content': 'fix ParserState in lib/parser/state.py'}],
            '/project', task={'id': 'task'})

    assert [row['id'] for row in selected] == ['parser-state']
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('startup_memory', 'start'),
        ('memory_list', 'start'), ('memory_list', 'complete'),
        ('memory_rank', 'start'), ('memory_rank', 'complete'),
        ('memory_hydrate', 'start'), ('memory_hydrate', 'complete'),
        ('startup_memory', 'complete'),
    ]
    # Sub-stages inherit the enclosing stage's identity; IDs stay log-only.
    assert {(record['task_id'], record['turn_id']) for record in records} == {
        ('task', 'turn')}
    # The completion says how the resident listing was obtained.
    listed = records[2]
    assert (listed['listing'], listed['snapshotAgeMs']) == ('resident', 42.0)


def test_startup_completion_details_are_closed_and_content_free(monkeypatch):
    records = _startup_records(monkeypatch)
    details = {}
    with observability.measure_task_startup(
            'memory_list', identity={'task_id': 'task'}, details=details):
        details.update(listing='persisted', snapshotAgeMs=12.34567,
                       memoryName='private-memory-name', rows=['body'])
    start, complete = records
    assert 'listing' not in start and 'snapshotAgeMs' not in start
    assert (complete['listing'], complete['snapshotAgeMs']) == (
        'persisted', 12.346)
    assert 'memoryName' not in complete and 'rows' not in complete
    assert 'private-memory-name' not in json.dumps(records)


def test_context_splits_its_storage_read_from_the_model_rebuild(monkeypatch):
    from copy import deepcopy
    from lib import turn_lifecycle
    from lib.conversation_sync.task_start import _context_stage
    from lib.storage.model_continuation import ModelContinuationStore

    rows = [
        {'turnId': 'user-old', 'actor': 'human', 'laneId': 'main', 'ordinal': 1,
         'status': 'completed', 'projection': {'content': 'Original request'}},
        {'turnId': 'assistant-old', 'actor': 'assistant', 'laneId': 'main', 'ordinal': 2,
         'status': 'completed', 'projection': {'content': 'Finished answer'}},
        {'turnId': 'user-new', 'actor': 'human', 'laneId': 'main', 'ordinal': 3,
         'status': 'completed', 'projection': {'content': 'Follow up'}},
        {'turnId': 'assistant-new', 'actor': 'assistant', 'laneId': 'main', 'ordinal': 4,
         'status': 'running', 'projection': {'content': ''}},
    ]

    class Client:
        def query(self, operation, payload, **kwargs):
            assert operation == 'turn.list'
            return deepcopy(rows)

    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda: Client())
    monkeypatch.setattr(ModelContinuationStore, 'read_context',
                        lambda self, **kwargs: {'supported': False})
    monkeypatch.setattr(ModelContinuationStore, 'get', lambda self, **kwargs: None)
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('context', identity={'attempt_id': 'attempt'}):
        messages = turn_lifecycle.build_api_messages(
            'conversation', 'assistant-new', {'model': 'fixture-model'}, user_id=7,
            observe_stage=_context_stage)
    assert [message['content'] for message in messages] == [
        'Original request', 'Finished answer', 'Follow up']
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('context', 'start'),
        ('context_read', 'start'), ('context_read', 'complete'),
        ('context_rebuild', 'start'), ('context_rebuild', 'complete'),
        ('context', 'complete')]
    assert records[2]['turns'] == len(rows)
    # A cold rebuild names itself, so a slow stage separates a missing
    # continuation checkpoint from a slow storage read.
    assert records[4]['replay'] == 'checkpoint_missing'
    assert {record['attempt_id'] for record in records} == {'attempt'}


def test_context_injection_names_its_slowest_provider_and_guidance(monkeypatch):
    from lib.tasks_pkg.orchestrator import _context_inject

    def compose(messages, *, task, **kwargs):
        task['_contextProviderTimings'] = [
            {'provider': 'static', 'status': 'ok', 'durationMs': 3.0},
            {'provider': 'skills', 'status': 'ok', 'durationMs': 812.5},
            {'provider': 'project', 'status': 'timeout', 'durationMs': 20.0},
        ]

    guidance = []
    monkeypatch.setattr(_context_inject, 'compose_task_context', compose)
    monkeypatch.setattr('lib.tasks_pkg.project_guidance.refresh_project_guidance',
                        lambda task, messages, *, model: guidance.append(model))
    records = _startup_records(monkeypatch)
    task = {'id': 'task', '_userId': 7, 'config': {}}
    with observability.measure_task_startup('startup_context', identity={'task_id': 'task'}):
        _context_inject.inject_context_and_emit_chips(
            task=task, messages=[], cfg={}, project_path='/project', project_enabled=True,
            memory_enabled=False, search_enabled=False, has_real_tools=False,
            model='fixture-model', tool_list=[], prefetch_executor=Mock(), tid='task',
            t_run_start=0.0)
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('startup_context', 'start'),
        ('startup_context_compose', 'start'), ('startup_context_compose', 'complete'),
        ('startup_context_guidance', 'start'), ('startup_context_guidance', 'complete'),
        ('startup_context', 'complete')]
    assert (records[2]['slowestProvider'], records[2]['slowestProviderMs']) == ('skills', 812.5)
    assert guidance == ['fixture-model']


@pytest.mark.usefixtures('chat_sidecar')
def test_registration_times_route_mint_and_birth_checkpoint(monkeypatch):
    """``registration`` is split into its route, mint and checkpoint stages."""
    import lib.model_routing as routing
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import create_task, discard_task
    from tests._seed import delete_conversation, seed_conversation

    conv_id = f'registration-stages-{uuid.uuid4().hex[:10]}'
    seed_conversation(conv_id, messages=[
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': 'hello', 'timestamp': 2},
    ], title='registration stages')
    stages = []
    real_observe = observability._STORE.observe

    def observe(name, value, buckets, **labels):
        if name == 'tofu_task_startup_stage_seconds':
            stages.append(labels['stage'])
        return real_observe(name, value, buckets, **labels)

    def mint(*args, observe_stage, **kwargs):
        with observe_stage('route'):
            pass
        with observe_stage('mint'):
            pass
        return SimpleNamespace(pin_id='registration-stage-pin',
                               candidates=[SimpleNamespace(offering={})])

    monkeypatch.setattr(observability._STORE, 'observe', observe)
    monkeypatch.setattr(routing, 'mint_routed_slot_group', mint)
    monkeypatch.setattr(routing, 'dispose_routed_slot_group', lambda group: True)
    ref = {'creator_id': 'creator', 'model_id': 'fixture-model'}
    task = create_task(conv_id, [{'role': 'user', 'content': 'hi'}],
                       {'modelRef': ref, 'model': 'fixture-model'},
                       user_id=1, supersede=False)
    try:
        assert stages == ['registration_route', 'registration_mint',
                          'registration_checkpoint']
    finally:
        discard_task(task['id'], conv_id)
        get_storage_client(write=True).command(
            'record.delete', {'namespace': 'task_results', 'key': task['id']},
            f'test-delete-task-result:{task["id"]}')
        delete_conversation(conv_id)
