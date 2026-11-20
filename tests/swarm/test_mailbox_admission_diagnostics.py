"""Durable mailbox admission against a disposable Sidecar, with no model transport."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lib.llm.stream_result import ProviderStreamResult
from lib.swarm import persistence, run_messages as mailbox
from lib.tasks_pkg import message_input_receipts as receipts
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar', 'tests.support.semantic_authorities')
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('change,category', [
    ('unchanged', None), ('retired', 'session_absent'),
    ('terminated', 'session_terminal'), ('superseded', 'session_superseded'),
    ('cross_project', 'conversation_scope'), ('wrong_source', 'swarm_mailbox_mismatch'),
])
def test_restored_carrier_admission(chat_sidecar, monkeypatch, caplog, tmp_path, change, category):
    monkeypatch.setattr(receipts.logger, 'propagate', True)
    conv = 'mailbox-' + uuid4().hex
    run = 'run-' + uuid4().hex
    seed_conversation(conv, settings={'projectPath': '/mailbox-project'})
    task = {'id': 'fixture-child', '_userId': 1, 'convId': conv, '_attemptId': 'fixture-attempt',
            'config': {'projectPath': '/mailbox-project'}}
    spec = SimpleNamespace(id='fixture-agent', retained_agent_id='', retained_run_id='', retained_scope='')
    binding = mailbox.binding_for(task, spec, conv, run_id=run)
    task['_swarmMessageBinding'] = binding

    def save(run_id=run, status='running', save_mode=None):
        persistence.save_session(conv, conv_id=conv, task_id=run_id, specs=[],
                                 config={'user_id': 1, 'parent_cfg': {'projectPath': '/mailbox-project'}},
                                 status=status, required=True, save_mode=save_mode)

    save()
    repo = mailbox.RunMessageRepository(binding)
    text = 'fixture-private-mailbox-content'
    msg = repo.submit(content=text, kind='reference', key='original', sender_task_id=run,
                      before_commit=lambda: mailbox.validate_target(binding))
    messages = [{'role': 'user', 'content': 'Fixture request'}]
    mailbox.inject_messages(task, messages, 0)
    assert task['_swarmMessageAttemptIds'] == [msg['id']]
    restored = receipts.historical_swarm_context(messages, binding=binding)
    task.pop(mailbox.ROUND_TARGET_FIELD, None)
    if change == 'retired':
        from lib.swarm.integration import _state
        import lib.swarm.integration as integration
        from threading import Event

        # Drive the real sweep and teardown, at an exact chosen age. The
        # durable-only case has no resident orchestrator to archive from.
        monkeypatch.setattr(integration, '_last_cleanup', 0)
        monkeypatch.setattr(_state, '_active_sessions', {})
        monkeypatch.setattr(_state, '_session_timestamps', {
            conv: _state.time.time() - _state.SESSION_TTL_SECONDS - 1,
        })
        monkeypatch.setattr(_state, '_retiring_sessions', {conv: Event()})
        queued = []
        monkeypatch.setattr(_state, '_start_retirement_worker_locked', queued.extend)
        with _state._sessions_lock:
            _state._cleanup_stale_sessions()
        assert queued == [(conv, None)]
        monkeypatch.setattr(_state.logger, 'propagate', True)
        with caplog.at_level('INFO', logger=_state.logger.name):
            _state._retire_expired_sessions(queued)
        event, = [r for r in caplog.records if r.getMessage().startswith('Swarm mailbox retirement')]
        assert event.getMessage() == 'Swarm mailbox retirement category=ttl_delete_requested outcome=committed scope=exact_run'
        assert event.tofu_event_fields == {'user_id': 1, 'conversation_id': conv, 'swarm_run_id': run}
        from lib.server.server_logging import _app_log_handler, _SinkFormatter, _LOG_FMT, _LOG_DATEFMT
        from lib.logging_pkg.log_aggregates import fingerprint_text, replay_log_lines
        sink_dir = tmp_path / 'logs'
        sink_dir.mkdir()
        handler = _app_log_handler(sink_dir, _SinkFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
        try:
            handler.handle(event)
        finally:
            handler.close()
        line = (sink_dir / 'app.log').read_text().strip()
        prefix = f'[mailbox-correlation owner=1 conversation={conv} run={run} attempt=- pid={event.process}] '
        assert prefix in line and text not in line
        assert event.getMessage() == 'Swarm mailbox retirement category=ttl_delete_requested outcome=committed scope=exact_run'
        (level, name, replayed, _), = replay_log_lines([line])
        assert fingerprint_text(level, name, replayed) == fingerprint_text(
            'INFO', event.name, replayed.removeprefix(prefix))
        assert persistence.load_session(conv, required=True) is None
    elif change == 'terminated':
        save(status='terminated')
    elif change == 'superseded':
        header = persistence.load_session(conv, required=True)
        assert persistence.dispose_run(persistence.run_identity(conv, header), 'replacement')
        save(run_id=run + '-successor', save_mode='launch')
    elif change == 'cross_project':
        task['_swarmMessageBinding'] = {**binding, 'projectPath': '/other-project'}
    elif change == 'wrong_source':
        restored[-1]['_swarmMessageMailbox'] = mailbox.RunMessageRepository(
            {**binding, 'swarmRunId': run + '-other'}).key
    else:
        # Normal mailbox CAS revision advances without invalidating this carrier.
        repo.submit(content='Another original', kind='reference', key='second', sender_task_id=run,
                    before_commit=lambda: mailbox.validate_target(binding))

    task['_activeRoundNum'] = 1
    receipts.remember(task, restored)
    body = {'messages': deepcopy(restored)}
    entered = []

    def provider(*args, **kwargs):
        lease = receipts.receipt_admission(task, None)(max_output_tokens=32)
        lease.prepare_input(body)
        lease.mark_dispatched()
        entered.append(True)
        return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': 'OK'}, 'stop', {})

    if category:
        with pytest.raises(receipts.MessageInputScopeError):
            receipts.stream_with_receipts(provider, task, body)
        assert not entered
        assert f'category={category}' in caplog.text
        event, = [r for r in caplog.records if r.getMessage().startswith('Mailbox input rejected')]
        from lib.server.server_logging import _SinkFormatter, _LOG_FMT, _LOG_DATEFMT
        event.tofu_coalesce_note = ''
        line = _SinkFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT).format(event)
        assert f'owner=1 conversation={conv} run={run} attempt=fixture-attempt' in line
        assert text not in line
        from lib.logging_pkg.log_aggregates import fingerprint_text, replay_log_lines
        (level, name, replayed, _), = replay_log_lines([line])
        assert fingerprint_text(level, name, replayed) == fingerprint_text(level, name, event.getMessage())
        assert conv not in event.getMessage() and run not in event.getMessage()
        assert text not in caplog.text
    else:
        receipts.stream_with_receipts(provider, task, body)
        assert entered == [True]
    mailbox.confirm_input(task, round_num=1)
    assert repo.load()[0]['messages'][0]['state'] == ('unconfirmed' if change in {'retired', 'superseded'} else 'submitted' if category else 'input_confirmed')


def test_browser_ids_share_one_storage_error_fingerprint(monkeypatch, caplog):
    from lib.logging_pkg.log_aggregates import fingerprint_text
    from lib.agent_core.storage_errors import StorageError

    monkeypatch.setattr(receipts.logger, 'propagate', True)
    fingerprints = set()
    for conv in ('mt18xr3wfs0rbq', 'mta7z4kg9d6p1'):
        receipts._log_scope_rejection({'_userId': 1, 'convId': conv},
                                      StorageError('database_integrity', 'private error details'))
        event = caplog.records[-1]
        assert event.tofu_event_fields['conversation_id'] == conv
        assert event.getMessage() == 'Mailbox input rejected category=storage_error'
        fingerprints.add(fingerprint_text(event.levelname, event.name, event.getMessage())[0])
    assert len(fingerprints) == 1


@pytest.mark.parametrize('mask_ack', [False, True])
def test_retirement_outcomes(semantic_authority, monkeypatch, caplog, mask_ack):
    from lib.swarm.integration import _state

    factory, owner, engine = semantic_authority
    monkeypatch.setattr(persistence, '_storage', factory)
    monkeypatch.setattr(_state.logger, 'propagate', True)
    key = 'retirement-' + uuid4().hex
    persistence.save_session(key, conv_id=key, task_id=key, specs=[],
                             config={'user_id': owner}, required=True)
    header = persistence.load_session(key, required=True)
    session = SimpleNamespace(user_id=owner, conv_id=key, task_id=key,
                              _sql_run_identity=None if engine else persistence.run_identity(key, header),
                              record_run_results=lambda: True, abort=lambda: None)
    command = persistence._command
    if mask_ack:
        def missing_ack(operation, *args, **kwargs):
            result = command(operation, *args, **kwargs)
            return None if operation.startswith('swarm.session.delete') else result
        monkeypatch.setattr(persistence, '_command', missing_ack)
    with caplog.at_level('INFO', logger=_state.logger.name):
        _state._retire_expired_session(key, session)
        _state._retire_expired_session(key, session)
    events = [r.getMessage() for r in caplog.records
              if r.getMessage().startswith('Swarm mailbox retirement')]
    outcomes = ['unknown', 'unknown'] if mask_ack else ['committed', 'noop']
    scope = 'key_only' if engine else 'exact_run'
    assert events == [f'Swarm mailbox retirement category=ttl_delete_requested outcome={outcome} scope={scope}'
                      for outcome in outcomes]
    assert persistence.load_session(key, required=True) is None


def test_retirement_refused_by_sql(chat_sidecar, monkeypatch, caplog):
    from lib.swarm.integration import _state

    key = 'retirement-' + uuid4().hex
    persistence.save_session(key, conv_id=key, task_id=key, specs=[],
                             config={'user_id': 1}, required=True)
    command = persistence._command

    def checkpoint_after_archive(operation, *args, **kwargs):
        if operation == 'swarm.session.delete_exact':
            # Arrives after the archive snapshot, before the actual delete transaction.
            command('swarm.agent.save', dict(swarm_key=key, run_id=key, agent_id='late',
                    now_ms=1, role='coder', objective='fixture', messages=[],
                    result={'final_answer': 'fixture'}, status='completed'))
        return command(operation, *args, **kwargs)

    monkeypatch.setattr(persistence, '_command', checkpoint_after_archive)
    monkeypatch.setattr(_state.logger, 'propagate', True)
    with caplog.at_level('INFO', logger=_state.logger.name):
        _state._retire_expired_session(key, None)
    assert 'category=ttl_delete_requested outcome=refused scope=exact_run' in caplog.text
    assert persistence.load_session(key, required=True) is not None


def test_retirement_receipt_read_failure_does_not_claim_rows_retained(chat_sidecar, monkeypatch, caplog):
    from lib.agent_core.storage_errors import StorageError
    from lib.server.server_logging import _SinkFormatter, _LOG_FMT, _LOG_DATEFMT
    from lib.swarm.integration import _state

    key = 'retirement-' + uuid4().hex
    persistence.save_session(key, conv_id=key, task_id=key, specs=[],
                             config={'user_id': 1}, required=True)
    read = persistence.read_lifecycle

    def lose_receipt(identity):
        assert read(identity)['disposition'] == 'ordinary_retirement'
        raise StorageError('database_unavailable', 'private receipt error')

    monkeypatch.setattr(persistence, 'read_lifecycle', lose_receipt)
    monkeypatch.setattr(_state.logger, 'propagate', True)
    with caplog.at_level('INFO', logger=_state.logger.name):
        _state._retire_expired_session(key, None)
    formatter = _SinkFormatter(_LOG_FMT, datefmt=_LOG_DATEFMT)
    lines = []
    for record in caplog.records:
        record.tofu_coalesce_note = ''
        lines.append(formatter.format(record))
    sink = '\n'.join(lines)
    assert f'owner=1 conversation={key} run={key}' in sink
    assert 'category=ttl_delete_requested outcome=unknown scope=exact_run' in sink
    assert 'rows retained' not in sink
    assert 'private receipt error' not in sink
    assert persistence.load_session(key, required=True) is None


@pytest.mark.parametrize('change,category', [
    ('unchanged', None), ('terminated', 'session_terminal'),
    ('cross_project', 'conversation_scope'),
])
def test_later_scope_denial_keeps_completed_local_command_checkpoint(
        chat_sidecar, monkeypatch, caplog, tmp_path, change, category):
    import subprocess
    import sys
    from lib.llm.llm_error_format import format_llm_error_for_user
    from lib.tasks_pkg.turn_retry import should_auto_retry_turn

    monkeypatch.setattr(receipts.logger, 'propagate', True)
    conv, run = 'mailbox-' + uuid4().hex, 'run-' + uuid4().hex
    seed_conversation(conv, settings={'projectPath': '/mailbox-project'})
    task = {'id': 'fixture-child', '_userId': 1, 'convId': conv,
            '_attemptId': 'fixture-attempt', 'config': {'projectPath': '/mailbox-project'}}
    spec = SimpleNamespace(id='fixture-agent', retained_agent_id='', retained_run_id='', retained_scope='')
    binding = mailbox.binding_for(task, spec, conv, run_id=run)
    task['_swarmMessageBinding'] = binding
    config = {'user_id': 1, 'parent_cfg': {'projectPath': '/mailbox-project'}}
    persistence.save_session(conv, conv_id=conv, task_id=run, specs=[], config=config, required=True)
    repo = mailbox.RunMessageRepository(binding)
    repo.submit(content='synthetic scoped context', kind='reference', key='original',
                sender_task_id=run, before_commit=lambda: mailbox.validate_target(binding))
    messages = [{'role': 'user', 'content': 'Run the synthetic local command once.'}]
    mailbox.inject_messages(task, messages, 0)
    entered = []

    def provider(task, body):
        lease = receipts.receipt_admission(task, None)(max_output_tokens=32)
        lease.prepare_input(body)
        lease.mark_dispatched()
        entered.append(task['_activeRoundNum'])
        return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': 'OK'}, 'stop', {})

    task['_activeRoundNum'] = 0
    receipts.remember(task, messages)
    receipts.stream_with_receipts(provider, task, {'messages': messages})
    mailbox.confirm_input(task, round_num=0)
    effect = tmp_path / 'effect.txt'
    command = [sys.executable, '-c',
               'import pathlib,sys; p=pathlib.Path(sys.argv[1]); '
               'p.open("a").write("effect\\n"); print("synthetic command completed")', str(effect)]
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    receipt = {'roundNum': 1, 'llmRound': 1, 'toolCallId': 'local-once',
               'toolName': 'run_command', 'toolArgs': '{}',
               'toolContent': completed.stdout, 'status': 'done'}
    messages.extend([
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'local-once', 'type': 'function',
             'function': {'name': 'run_command', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'local-once', 'content': completed.stdout},
    ])
    persistence.save_agent(conv, spec.id, role='general', objective='Synthetic command',
        status='running', messages=messages, result={'tool_rounds': [receipt]},
        rounds_used=1, required=True, run_id=run)
    checkpoint = deepcopy(persistence.load_session(conv, required=True)['agents'])
    assert checkpoint[0]['messages'] == messages
    assert checkpoint[0]['result']['tool_rounds'] == [receipt]

    if change == 'terminated':
        persistence.save_session(conv, conv_id=conv, task_id=run, specs=[], config=config,
                                 status='terminated', required=True)
    elif change == 'cross_project':
        task['_swarmMessageBinding'] = {**binding, 'projectPath': '/other-project'}
    task.pop(mailbox.ROUND_TARGET_FIELD, None)
    task['_activeRoundNum'] = 1
    receipts.remember(task, messages)
    if category:
        with pytest.raises(receipts.MessageInputScopeError) as failure:
            receipts.stream_with_receipts(provider, task, {'messages': messages})
        diagnostic = format_llm_error_for_user(failure.value, model='fixture-model', source='llm-stream')
        assert diagnostic['source'] == 'context-admission'
        assert diagnostic['failureStage'] == 'before_dispatch'
        assert diagnostic['providerReached'] is False and diagnostic['retryable'] is False
        assert should_auto_retry_turn(diagnostic, 0) == (False, 0.0)
        assert entered == [0] and f'category={category}' in caplog.text
        assert '_verifiedMessageInputIds' not in task
    else:
        receipts.stream_with_receipts(provider, task, {'messages': messages})
        assert entered == [0, 1]
    assert persistence.load_session(conv, required=True)['agents'] == checkpoint
    assert messages[-1]['content'] == 'synthetic command completed\n'
    assert effect.read_text() == 'effect\n'
    assert 'synthetic scoped context' not in caplog.text
    assert 'synthetic command completed' not in caplog.text
    assert messages == checkpoint[0]['messages']
    assert completed.returncode == 0
