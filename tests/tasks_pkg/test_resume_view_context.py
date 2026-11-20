"""Continue resumes the Turn's exact working view, then composes context on it.

The interrupted attempt's context carriers, rounds and system policy are already
in its checkpointed view. Composing against that view reuses every unchanged
block; only genuinely changed context is appended, at the resume point, and only
that change is recorded as a context note.
"""
from copy import deepcopy
import threading

import pytest

from tests.compaction.test_dispatch_context_recovery import _round
from tests.compaction.test_suspended_working_view import _Archives

pytestmark = pytest.mark.unit


@pytest.fixture
def archives(monkeypatch):
    import lib.agent_core.store
    store = _Archives()
    monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
    return store


def _task(attempt):
    return {'id': f'task-{attempt}', 'convId': 'conversation', '_userId': 1,
            '_turnId': 'turn', '_attemptId': attempt, 'toolRounds': [],
            'config': {'model': 'gpt-6-astra', '_modelReplayBase': [['human', 'a' * 64]]},
            'content': '', 'thinking': '', 'content_lock': threading.Lock()}


def _compose(monkeypatch, task, messages, blocks):
    from lib.tasks_pkg import context_composer
    from lib.tasks_pkg.context_composer import ComposeRequest, compose_context
    monkeypatch.setattr(context_composer, 'collect_context_blocks',
                        lambda messages, request: list(blocks))
    return compose_context(messages, ComposeRequest(task=task, user_id=1, model='gpt-6-astra'))


def _blocks(date='Current date: 2026-11-07'):
    from lib.tasks_pkg.context_composer import context_block
    return [context_block('relevant_memories', '<relevant_memories>keep</relevant_memories>'),
            context_block('skills_index', '<available_skills>skills</available_skills>'),
            context_block('current_date', date)]


def _interrupt(monkeypatch, archives):
    """Run the first attempt to its interruption and checkpoint its view."""
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first = _task('first')
    messages = [{'role': 'user', 'content': 'work'}]
    _compose(monkeypatch, first, messages, _blocks())
    assert {note['reason'] for note in first[INJECTED_NOTES_FIELD]} == {
        'relevant_memories', 'skills_index', 'current_date'}
    rounds = [{**_round(i), 'attemptId': 'first', 'taskId': 'task-first'} for i in range(2)]
    messages.extend(_reconstruct_tool_call_messages(rounds))
    first.update(toolRounds=rounds, messages=messages, _messages_include_current_run=True,
                 status='done', aborted=True, finishReason='aborted')
    preserve_suspended_working_view(first)
    return first, rounds


def _resume(archives, first, rounds):
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.compaction._resume_projection import (
        resume_background_digest, resume_execution_digest)
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state, restore_resume_view
    from lib.turn_lifecycle import _task_projection
    projection = _task_projection(first, {})
    config = deepcopy(first['config']) | _resume_executor_config('continue', projection)
    config['_resumeBackgroundDigest'] = resume_background_digest([])
    config['_resumeExecutionDigest'] = resume_execution_digest('', '')
    resumed = _task('second')
    resumed['config'] = config
    fresh = [{'role': 'user', 'content': 'work'}]  # cold history: no carriers
    prepared = prepare_resume_state(config)
    assert restore_resume_view(task=resumed, cfg=config, messages=fresh,
                               prepared_state=prepared)
    return resumed, config, fresh, prepared


def test_unchanged_context_is_reused_without_a_new_record(monkeypatch, archives):
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, _, view, _ = _resume(archives, first, rounds)
    assert view == archives.rows['archive-1']['post']
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, _blocks())
    assert view == before
    assert not resumed.get(INJECTED_NOTES_FIELD)


def test_changed_context_is_appended_at_the_resume_point_and_noted_alone(monkeypatch, archives):
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, _, view, _ = _resume(archives, first, rounds)
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, _blocks(date='Current date: 2026-11-08'))
    assert view[:len(before)] == before
    assert len(view) == len(before) + 1 and view[-1].get('_contextComposer')
    assert [row['id'] for row in view[-1]['_contextBlocks']] == ['current_date']
    assert [note['reason'] for note in resumed[INJECTED_NOTES_FIELD]] == ['current_date']


def test_turn_memory_selection_is_retained_not_reselected(monkeypatch, archives):
    from lib.tasks_pkg.orchestrator._memory_prefetch import maybe_run_memory_prefetch
    from lib.tasks_pkg.orchestrator._resume_state import resume_view_holds_turn_block
    from lib.tasks_pkg.context_composer import context_block
    from lib.tasks_pkg.segments._types import INJECTED_NOTES_FIELD
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, config, view, _ = _resume(archives, first, rounds)
    assert resume_view_holds_turn_block(resumed, view, 'relevant_memories')
    assert not resume_view_holds_turn_block(_task('cold'), view, 'relevant_memories')
    import lib.memory.prefetch as prefetch
    monkeypatch.setattr(prefetch, 'run_memory_prefetch',
                        lambda *a, **k: pytest.fail('the Turn selection was re-run'))
    maybe_run_memory_prefetch(task=resumed, cfg=config, messages=view, tool_list=[],
                              project_path=None, project_enabled=False, memory_enabled=True,
                              has_real_tools=True, injected_tool_calls=0,
                              turn_selection_retained=True)
    assert resumed['_memoryPrefetch'] == {'phase': 'skipped', 'reason': 'turn_selection_retained'}
    # The composer's own provider keeps the restored block for this outcome.
    retained = context_block('relevant_memories', '', reason='provider_unavailable')
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, [retained] + _blocks()[1:])
    assert view == before and not resumed.get(INJECTED_NOTES_FIELD)


def test_hydration_keeps_the_restored_view_and_seeds_round_counters(monkeypatch, archives):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, config, view, prepared = _resume(archives, first, rounds)
    before = deepcopy(view)
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    assert view == before  # rounds are already in the view: never replayed twice
    assert state.model_round_start == 2 and state.tool_call_happened
    assert resumed['_resumeContextHydrated'] is True


def test_continue_keeps_the_interrupted_attempts_context_record(monkeypatch, archives):
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.turn_lifecycle import _task_projection
    first, rounds = _interrupt(monkeypatch, archives)
    resumed, config, view, prepared = _resume(archives, first, rounds)
    carried = config['checkpointInjectedNotes']
    assert {note['reason'] for note in carried} == {
        'relevant_memories', 'skills_index', 'current_date'}
    assert all(note['attemptId'] == 'first' for note in carried)
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    segments = _task_projection(resumed, {})['segments']
    notes = [index for index, segment in enumerate(segments)
             if segment['type'] == 'system_note']
    first_round = next(index for index, segment in enumerate(segments)
                       if segment['type'] != 'system_note')
    # The interrupted attempt's record stays at the head of its own rounds.
    assert notes and max(notes) < first_round
    assert {segments[index]['attemptId'] for index in notes} == {'first'}


def test_interruption_during_the_first_model_call_still_resumes_its_view(monkeypatch, archives):
    from lib.conversation_sync.command_service import _resume_executor_config
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, resume_background_digest, resume_execution_digest)
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state, restore_resume_view
    from lib.turn_lifecycle import _task_projection
    first = _task('first')
    messages = [{'role': 'user', 'content': 'work'}]
    _compose(monkeypatch, first, messages, _blocks())
    first.update(messages=messages, _messages_include_current_run=True,
                 status='done', aborted=True, finishReason='aborted')
    preserve_suspended_working_view(first)
    config = deepcopy(first['config']) | _resume_executor_config(
        'continue', _task_projection(first, {}))
    assert 'checkpointToolRounds' not in config  # no round was retained
    config.update(_turnOperation='continue',
                  _resumeBackgroundDigest=resume_background_digest([]),
                  _resumeExecutionDigest=resume_execution_digest('', ''))
    resumed = _task('second')
    fresh = [{'role': 'user', 'content': 'work'}]
    assert restore_resume_view(task=resumed, cfg=config, messages=fresh,
                               prepared_state=prepare_resume_state(config))
    assert fresh == messages


def test_orphan_attempt_notes_are_not_carried():
    from lib.conversation_sync.command_service import _resume_executor_config
    projection = {'toolRounds': [{**_round(0), 'attemptId': 'kept', 'taskId': 't'}],
                  'segments': [
                      {'type': 'system_note', 'text': 'kept record', 'noteKind': 'context-injection',
                       'attemptId': 'kept', 'llmRound': 0, 'blockId': 'n1'},
                      {'type': 'system_note', 'text': 'orphan record', 'noteKind': 'context-injection',
                       'attemptId': 'orphan', 'llmRound': 0, 'blockId': 'n2'}]}
    carried = _resume_executor_config('continue', projection)['checkpointInjectedNotes']
    assert [note['text'] for note in carried] == ['kept record']


def _checklist_dispatch_resume(monkeypatch, archives, lead, coarse):
    import json
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
    from tests.tasks_pkg.test_root_orchestrator_agent_loop_adapter import (
        _install_common, _request)
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, reconstruct_tool_messages_from_segments
    from lib.turn_lifecycle import _task_projection
    from lib.turn_projection_segments import projection_with_stable_segments

    from lib.acceptance import turns
    monkeypatch.setattr(turns, 'read_execution_requirements', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.round_reads._send', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.working_facts.refresh_working_facts',
                        lambda *a, **k: None)
    first = _task('first')
    if lead:
        first['_coordinatorDispatchScope'] = {
            'project_path': '/synthetic/project', 'binding_revision': 1,
            'conversation_id': first['convId'], 'user_id': 1}
    messages = [{'role': 'user', 'content': 'retain checklist progress'}]
    states = [
        {'version': 3, 'update_count': 1, 'explanation': 'Earlier accepted progress',
         'todos': [{'content': 'Inspect retained source', 'status': 'in_progress'},
                   {'content': 'Verify ordering', 'status': 'pending'}]},
        {'version': 3, 'update_count': 2, 'explanation': 'Latest accepted progress',
         'todos': [{'content': 'Inspect retained source', 'status': 'completed'},
                   {'content': 'Verify replay ordering', 'status': 'in_progress'}]},
        {'version': 3, 'update_count': 3, 'explanation': 'Resumed accepted progress',
         'todos': [{'content': 'Verify replay ordering', 'status': 'completed'},
                   {'content': 'Inspect resumed DOM', 'status': 'blocked'}]},
    ]

    heading = (
        '## Active Task Checklist\n'
        'Accepted task progress preserved by the runtime. Restoration is not '
        'an update. Status reports progress, not proof of success. '
        'Use only currently available tools for any actual update.\n')
    expected_payloads = [
        heading + '- [ ] Inspect retained source ⏳\n- [ ] Verify ordering\n'
        'Progress explanation from the accepted todo update: Earlier accepted progress',
        heading + '- [x] Inspect retained source\n- [ ] Verify replay ordering ⏳\n'
        'Progress explanation from the accepted todo update: Latest accepted progress',
        heading + '- [x] Verify replay ordering\n- [ ] Inspect resumed DOM [blocked]\n'
        'Progress explanation from the accepted todo update: Resumed accepted progress',
    ]
    consuming_requests = []
    lead_scope = deepcopy(first.get('_coordinatorDispatchScope'))
    tool_name = 'get_conversation_status' if lead else 'read_files'

    def invoke(task, view, state, batches):
        from lib.tasks_pkg.coordinator_policy import coordinator_scope, coordinator_call_rejection
        assert bool(coordinator_scope(task)) is lead
        assert coordinator_call_rejection(task, tool_name, {}) is None
        from types import SimpleNamespace
        from lib.llm.stream_result import ProviderStreamResult
        from lib.tasks_pkg.llm_fallback import _transport
        events = []
        _install_common(monkeypatch, events, real_body=True)
        monkeypatch.setattr(root_loop, 'build_stream_accumulator', lambda *a, **k:
                            SimpleNamespace(announced_tc_map={}, on_tool_call_ready=None,
                                            close=lambda **kwargs: None))
        monkeypatch.setattr(root_loop, 'apply_stream_decision',
                            lambda *a, **k: ('proceed', k['premature_retry_count']))
        script = []
        for number, count in batches:
            script.append(({
                'role': 'assistant', 'content': f'Narration {number}',
                'reasoning_content': f'Thinking {number}',
                'tool_calls': [{'id': f'call-{number}-{index}', 'type': 'function',
                                'function': {'name': tool_name, 'arguments': '{}'}}
                               for index in range(count)]}, 'tool_calls', {}))
        replies = iter(zip(batches, script))

        def provider(_task, body, **kwargs):
            (number, _), (message, finish, usage) = next(replies)
            request_messages = deepcopy(body['messages'])
            consuming_requests.append((number, request_messages))
            events.append(('llm', number))
            return ProviderStreamResult.from_legacy(message, finish, usage)

        monkeypatch.setattr(_transport, 'stream_attempt', provider)

        def execute(_task, current, wire, *args, round_num, **kwargs):
            batch = []
            for index, call in enumerate(current.assistant_msg['tool_calls']):
                ordinal = len(task['toolRounds']) + len(batch)
                batch.append({**_round(ordinal), 'toolName': tool_name, 'toolCallId': call['id'],
                              'llmRound': round_num, 'roundNum': ordinal + 1,
                              'attemptId': task['_attemptId'], 'taskId': task['id'],
                              'assistantContent': f'Narration {round_num}' if not index else '',
                              'thinking': f'Thinking {round_num}' if not index else ''})
            task['toolRounds'].extend(batch)
            wire.extend(_reconstruct_tool_call_messages(batch))
            if round_num < 2:
                task['_todoState'] = deepcopy(states[round_num])
            if round_num == 0 and coarse:
                task['toolRounds'].append({
                    'roundNum': 9000001, '_inboxInject': True,
                    'attemptId': 'first', 'taskId': 'task-first',
                    'toolName': 'agent_inbox', 'status': 'done',
                    'toolContent': 'Historical inbox without model-round evidence'})
            if round_num == batches[-1][0]:
                task['aborted'] = True
            return False

        monkeypatch.setattr(root_loop, 'run_tool_dispatch', execute)
        request = _request(task, state)
        request.messages = view
        request.cfg = task['config']
        request.tool_list = [{'type': 'function', 'function': {'name': tool_name}}]
        result = root_loop.run_root_agent_loop(request)
        assert result.outcome.aborted
        assert [event[1] for event in events if event[0] == 'llm'] == [n for n, _ in batches]
        for number, request_messages in consuming_requests:
            text = json.dumps(request_messages, ensure_ascii=False).replace('\\n', '\n')
            for expected in expected_payloads[:number]:
                assert expected in text

    initial_state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    invoke(first, messages, initial_state, [(0, 3), (1, 2), (2, 1)])

    notes_before = deepcopy(first[INJECTED_NOTES_FIELD])
    rounds_before = deepcopy(first['toolRounds'])
    messages_before = deepcopy(messages)
    assert [note['llmRound'] for note in notes_before] == [1, 2], notes_before
    assert all(note['reason'] == 'active_checklist' for note in notes_before)
    first.update(messages=messages, _messages_include_current_run=True,
                 status='done', aborted=True, finishReason='aborted')
    preserve_suspended_working_view(first)
    resumed, config, view, prepared = _resume(archives, first, rounds_before)
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    assert state.model_round_start == 3
    assert view == messages_before
    assert resumed['_todoState'] == first['_todoState']
    assert resumed['_checkpointToolRounds'] == rounds_before
    assert resumed['toolRounds'] == []
    assert resumed[INJECTED_NOTES_FIELD] == notes_before

    if lead_scope is not None:
        resumed['_coordinatorDispatchScope'] = deepcopy(lead_scope)
    resumed['_todoState'] = deepcopy(states[2])
    invoke(resumed, view, state, [(3, 2)])
    assert view[:len(messages_before)] == messages_before
    assert 'Resumed accepted progress' in json.dumps(view[len(messages_before)]['content'])
    projection = _task_projection(resumed, {})
    replayed = projection_with_stable_segments(
        json.loads(json.dumps(projection)), status='running')
    assert projection_with_stable_segments(
        json.loads(json.dumps(replayed)), status='running') == replayed
    segments = replayed['segments']
    notes = [segment for segment in segments if segment['type'] == 'system_note']
    assert [note['llmRound'] for note in notes] == [1, 2, 3]
    assert ['Earlier accepted progress' in notes[0]['text'],
            'Latest accepted progress' in notes[1]['text'],
            'Resumed accepted progress' in notes[2]['text']] == [True, True, True]
    for note in notes:
        consuming_tool = next(segment for segment in segments
                              if segment['type'] == 'tool_use'
                              and segment['llmRound'] == note['llmRound'])
        assert segments.index(note) < segments.index(consuming_tool)
    assert resumed[INJECTED_NOTES_FIELD][:2] == notes_before
    assert first['toolRounds'] == rounds_before
    assert first[INJECTED_NOTES_FIELD] == notes_before
    assert first['messages'] == messages_before
    wire = reconstruct_tool_messages_from_segments(segments)
    assert 'Active Task Checklist' not in json.dumps(wire)
    assert [number for number, _ in consuming_requests] == [0, 1, 2, 3]
    assert [note['text'] for note in notes] == expected_payloads
    for payload in expected_payloads:
        assert sum(payload in json.dumps(message, ensure_ascii=False)
                   .replace('\\n', '\n') for message in view) == 1
    expected_sequence = []
    for number, count in ((0, 3), (1, 2), (2, 1), (3, 2)):
        if number:
            expected_sequence.append(('system_note', expected_payloads[number - 1]))
        expected_sequence.extend([('thinking', f'Thinking {number}'),
                                  ('text', f'Narration {number}')])
        expected_sequence.extend(('tool_use', f'call-{number}-{index}') for index in range(count))
    assert [(segment['type'], segment.get('id') if segment['type'] == 'tool_use'
             else segment.get('text')) for segment in segments
            if not segment.get('terminal')] == expected_sequence
    assert [segment.get('text') for segment in segments if segment.get('terminal')] == ['']
    return replayed, expected_sequence, expected_payloads


@pytest.mark.parametrize('lead', [False, True], ids=['owner', 'project-lead'])
@pytest.mark.parametrize('coarse', [False, True], ids=['exact', 'coarse-history'])
def test_checklist_notes_follow_model_rounds_through_replay_and_resume(
        monkeypatch, archives, lead, coarse):
    _checklist_dispatch_resume(monkeypatch, archives, lead, coarse)


@pytest.mark.parametrize('stale_round', [None, 19], ids=['fresh', 'stale-stamp'])
def test_startup_changed_context_uses_validated_first_consuming_round(
        monkeypatch, archives, stale_round):
    from types import SimpleNamespace
    from lib.acceptance import turns
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.llm_fallback import _transport
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    from tests.tasks_pkg.test_root_orchestrator_agent_loop_adapter import _install_common, _request

    first, rounds = _interrupt(monkeypatch, archives)
    historical_notes = deepcopy(first[INJECTED_NOTES_FIELD])
    # The resumed executor may retain unrelated failure-attribution state.
    from lib.tasks_pkg.orchestrator._resume_state import restore_resume_view
    original_restore = restore_resume_view

    def restore(**kwargs):
        if stale_round is not None:
            kwargs['task']['_activeRoundNum'] = stale_round
        return original_restore(**kwargs)

    monkeypatch.setattr('lib.tasks_pkg.orchestrator._resume_state.restore_resume_view', restore)
    resumed, config, view, prepared = _resume(archives, first, rounds)
    before = deepcopy(view)
    _compose(monkeypatch, resumed, view, _blocks(date='Current date: 2026-11-08'))
    note, = resumed[INJECTED_NOTES_FIELD]
    assert note['reason'] == 'current_date'
    assert note['llmRound'] == prepared.next_model_round == 2
    assert view[:len(before)] == before
    startup_note = deepcopy(note)
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    _install_common(monkeypatch, [], real_body=True)
    monkeypatch.setattr(turns, 'read_execution_requirements', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.round_reads._send', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.working_facts.refresh_working_facts',
                        lambda *a, **k: None)
    monkeypatch.setattr(root_loop, 'build_stream_accumulator', lambda *a, **k:
                        SimpleNamespace(announced_tc_map={}, on_tool_call_ready=None,
                                        close=lambda **kwargs: None))
    requests = []

    def provider(task, body, **kwargs):
        requests.append(deepcopy(body['messages']))
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Synthetic response'}, 'stop', {})

    monkeypatch.setattr(_transport, 'stream_attempt', provider)
    request = _request(resumed, state)
    request.messages = view
    root_loop._RootLoopHooks(request).dispatch(state.model_round_start, request.tool_list)
    import json
    assert len(requests) == 1
    assert 'Current date: 2026-11-08' in json.dumps(requests[0])
    assert resumed[INJECTED_NOTES_FIELD][0] == historical_notes[0]
    assert resumed[INJECTED_NOTES_FIELD][-1] == startup_note
    assert note['llmRound'] == state.model_round_start
    assert first[INJECTED_NOTES_FIELD] == historical_notes
    assert view[:len(before)] == before


@pytest.mark.parametrize('issued_round', [0, 7], ids=['no-native-checkpoint', 'native-restored'])
def test_native_startup_notes_bind_issued_round_before_segment_publication(
        monkeypatch, archives, issued_round):
    import asyncio
    import json
    from concurrent.futures import ThreadPoolExecutor
    from lib.agent_core.native_process import RoundRequest
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_context import NativeContextPolicy
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, assemble_segments, segments_to_json
    from lib.turn_projection_segments import projection_with_stable_segments
    from lib.acceptance import turns

    first, rounds = _interrupt(monkeypatch, archives)
    historical = deepcopy(first[INJECTED_NOTES_FIELD])
    resumed, config, view, prepared = _resume(archives, first, rounds)
    # Storage/route resources are not used: only this port's startup-note owner
    # participates, followed by real NativeContextPolicy preparation.
    invocation = object.__new__(NativePreparedInvocation)
    invocation._startup_context_notes = []
    resumed['_nativePreparedInvocation'] = invocation
    before = deepcopy(view)
    blocks = _blocks(date='Current date: 2026-11-08')
    from lib.tasks_pkg.context_composer import context_block
    blocks.append(context_block('platform_static', 'Synthetic native session policy'))
    _compose(monkeypatch, resumed, view, blocks)
    assert not resumed.get(INJECTED_NOTES_FIELD)
    assert '_sessionContextNoteSig' not in resumed
    state = RoundState(model='gpt-6-astra', preset='default', thinking_enabled=False)
    apply_resume_state(task=resumed, cfg=config, messages=view, tid='second',
                       prepared_state=prepared, round_state=state)
    assert state.model_round_start == 2
    assert resumed[INJECTED_NOTES_FIELD] == historical
    resumed.update(status='running', abort_event=threading.Event())
    monkeypatch.setattr(turns, 'read_execution_requirements', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.round_reads._send', lambda task: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.working_facts.refresh_working_facts',
                        lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._round_message_hygiene.run_compaction_pipeline',
                        lambda *a, **k: None)
    principal = PrincipalContext.user(subject_id='fixture', owner_user_id=1)

    async def prepare(executor):
        policy = NativeContextPolicy(principal=principal, task=resumed, executor=executor)
        policy.bind_route({'owner_user_id': 1, 'logical_model': 'gpt-6-astra',
                           'wire_model': 'fixture-wire', 'context_window': 131072,
                           'provider_id': 'fixture'}, {'max_tokens': 1024}, {})
        try:
            request = RoundRequest(principal, resumed['id'], 'native-request', issued_round,
                                   len(view), view)
            result = await policy.prepare(request)
            assert 'Current date: 2026-11-08' in json.dumps(result)
            notes = deepcopy(resumed[INJECTED_NOTES_FIELD])
            await policy.prepare(request)
            assert resumed[INJECTED_NOTES_FIELD] == notes
        finally:
            policy.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(prepare(executor))
    assert resumed[INJECTED_NOTES_FIELD][:len(historical)] == historical
    added = resumed[INJECTED_NOTES_FIELD][len(historical):]
    assert {note['reason'] for note in added} == {'current_date', 'session_context_committed'}
    assert all(note['llmRound'] == issued_round for note in added)
    assert resumed['_sessionContextNoteSig']
    resumed['toolRounds'].append({**_round(99), 'toolCallId': 'native-consuming',
                                  'llmRound': issued_round, 'roundNum': 100,
                                  'attemptId': 'second', 'taskId': 'task-second',
                                  'thinking': 'Native consuming thought',
                                  'assistantContent': 'Native consuming narration'})
    segments = segments_to_json(assemble_segments(resumed))
    consuming = next(index for index, segment in enumerate(segments)
                     if segment.get('id') == 'native-consuming')
    for note in added:
        index = next(i for i, segment in enumerate(segments)
                     if segment.get('blockId') == note['blockId'])
        thought = next(i for i, segment in enumerate(segments)
                       if segment.get('text') == 'Native consuming thought')
        narration = next(i for i, segment in enumerate(segments)
                         if segment.get('text') == 'Native consuming narration')
        assert index < thought < narration < consuming
        assert all(segment.get('attemptId') == 'first' for segment in segments[:index]
                   if segment['type'] == 'tool_use')
    projection = {'segments': segments, 'toolRounds': rounds + resumed['toolRounds']}
    replay = projection_with_stable_segments(json.loads(json.dumps(projection)))
    assert projection_with_stable_segments(json.loads(json.dumps(replay))) == replay
    assert first[INJECTED_NOTES_FIELD] == historical
    # System insertion may shift the list but cannot change the retained rows.
    assert all(message in view for message in before)


def test_native_startup_note_failure_remains_display_only(monkeypatch):
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    import lib.tasks_pkg.segments as segments
    invocation = object.__new__(NativePreparedInvocation)
    invocation._startup_context_notes = []
    task = {'id': 'synthetic'}
    fields = dict(kind='session-context', text='Synthetic session policy',
                  action='prompt', reason='session_context_committed')
    assert invocation.defer_startup_context_note(task, fields, session_signature='signature')
    assert invocation.defer_startup_context_note(task, fields, session_signature='signature')
    assert len(invocation._startup_context_notes) == 1
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic display failure')
    monkeypatch.setattr(segments, 'record_injected_note', fail)
    invocation.record_startup_context_notes(task, 7)
    assert not task.get(INJECTED_NOTES_FIELD)
    assert '_sessionContextNoteSig' not in task
    assert not invocation.defer_startup_context_note(task, fields)
