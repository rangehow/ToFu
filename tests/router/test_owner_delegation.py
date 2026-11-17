"""The owner's delegation lets the Global Router act, never speak, as the owner (ROUTER.md R9).

Owner decision 2026-11-06. The grant lives in the lifecycle approval gate,
the single decision point for live-server restarts: only the owner acting in
the Tofu UI grants it, any owner request revokes it at once, and every action
it covers needs an owner trigger (the owner's message of the current request,
an owner message steered into the reply, or an active standing instruction),
a reason and one audit record. The server restart is only ever mocked here.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib import lifecycle_approval as gate
from lib.identity import PrincipalContext
from lib.lifecycle_approval import DelegationRefused
from lib.router import forwarding
from lib.router.instructions import RouterInstructions
from lib.server.server_reexec import ServerReexecOutcome
from tests.router.test_standing_instructions import INSTRUCTION, OWNER, r as standing

__all__ = ('standing',)

pytestmark = pytest.mark.unit

OWNER_WORDS = '把 Tofu 服务器重启一下，前端要先构建成功。'
ROUTER = {'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None}


def human(owner=OWNER, credential='session', browser='fetch-metadata'):
    """The provenance only routes/api_v1/auth.human_ui_provenance mints."""
    return {'kind': 'human-ui', 'ownerUserId': owner, 'subject': 'session-key', 'credential': credential,
            'browser': browser, 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    # The server manager's restart outcome lives in this project's manager state.
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    return tmp_path


def _manager_outcome(approvals, **outcome):
    """What server_manager.LifecycleManager._settle_restart persisted for the restart."""
    (approvals / 'data').mkdir(exist_ok=True)
    (approvals / 'data' / 'server-manager-state.json').write_text(json.dumps({'lastRestart': outcome}))


@pytest.fixture
def d(standing, approvals, monkeypatch):
    """The standing-instruction fixture plus a live router attempt and mocked restart owners."""
    s = standing
    s.turns['owner-turn']['projection'] = {'content': 'restart the server', 'originalContent': OWNER_WORDS}
    owner = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)
    s.task.update(_turnId='router-turn', _attemptId='router-attempt', _attended=True,
                  _principalContext=owner.to_payload())
    s.events, s.running, s.prepare_error = [], [], ''
    monkeypatch.setattr('lib.router.owner_delegation._live_router_attempt',
                        lambda task, scope: s.events.append(('live-router-attempt',)))

    def running(exclude_conv_id=None, *, user_id=None):
        s.events.append(('protect', exclude_conv_id, user_id))
        if isinstance(s.running, Exception):
            raise s.running
        return list(s.running.pop(0) if s.running and isinstance(s.running[0], list) else s.running)
    monkeypatch.setattr('lib.tasks_pkg.manager.list_running_tasks', running)

    def prepare():
        s.events.append(('prepare',))
        return s.prepare_error
    monkeypatch.setattr('lib.server.server_reexec.prepare_server_reexec_frontend', prepare)

    def perform(reason, **kwargs):
        s.events.append(('perform', reason, kwargs))
        return ServerReexecOutcome(True)
    monkeypatch.setattr('lib.server.server_reexec.perform_server_reexec', perform)

    def human_gate(*args, **kwargs):  # the owner's own approval prompts are never touched
        raise AssertionError('owner delegation must not create, decide or consume lifecycle approvals')
    for name in ('create_request', 'decide', 'consume', 'consume_any', 'consume_restart'):
        monkeypatch.setattr(gate, name, human_gate)
    s.detached = []
    monkeypatch.setattr('lib.router.owner_delegation._start_detached', s.detached.append)

    def run_detached():
        while s.detached:
            s.detached.pop(0)()
    s.run_detached = run_detached
    s.grant = lambda **grantee: gate.set_delegation(OWNER, True, grantee={**ROUTER, **grantee},
                                                    provenance=human())
    s.revoke = lambda: gate.set_delegation(OWNER, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': OWNER, 'credential': 'header'})
    s.restart = lambda **args: s.call('restart_server', **{key: value for key, value in {
        'reason': 'The owner asked for a restart', 'source_turn_id': 'owner-turn',
        'idempotency_key': 'restart-1', **args}.items() if value is not None})
    s.audit = lambda: gate.delegated_actions(OWNER)
    s.names = lambda: [event[0] for event in s.events]
    return s


def _approval_document(approvals):
    return json.loads((approvals / 'lifecycle_approvals.json').read_text())


def test_without_the_grant_a_restart_is_a_typed_refusal_and_nothing_is_pending(d, approvals):
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_not_granted'
    assert refused.value.execution_started is False
    assert 'Act for me' in str(refused.value) and 'No approval request was created' in str(refused.value)
    assert d.events == [] and d.detached == []
    # No pending approval for the owner to click (create_request would have failed
    # the fixture), and without a grant there is no delegated action to show.
    assert not (approvals / 'lifecycle_approvals.json').exists()
    assert d.audit() == []
    # A refusal never consumes the idempotency key: the same call works once granted.
    d.grant()
    assert d.restart()['status'] == 'accepted'


def test_a_granted_restart_passes_the_gate_runs_the_sanctioned_path_detached_and_is_audited(d, approvals):
    grant = d.grant()
    assert grant['state'] == 'granted' and grant['revision'] == 1 and grant['assurance'] == 'authenticated'
    result = d.restart()
    assert result['status'] == 'accepted' and result['execution_started'] is True
    # Accepted before anything runs: the restart outlives the calling turn.
    assert d.events == [('protect', 'router', None)] and len(d.detached) == 1
    [pending] = d.audit()
    assert pending['id'] == result['action_id'] and pending['status'] == 'accepted'
    d.run_detached()
    # Frontend preparation first; the old server stops only after it succeeded,
    # through the shared restart owner of Settings \u2192 restart.
    assert d.names() == ['protect', 'prepare', 'protect', 'perform']
    _, reason, kwargs = d.events[-1]
    assert reason == 'restart' and kwargs['frontend_prevalidated'] is True
    assert isinstance(kwargs['requested_at'], float)
    [action] = d.audit()
    assert action['status'] == 'restarting' and action['acceptedAt'] >= action['requestedAt']
    assert action['requester'] == {**ROUTER, 'turnId': 'router-turn', 'attemptId': 'router-attempt'}
    assert action['reason'] == 'The owner asked for a restart'
    assert action['trigger'] == {'kind': 'owner_message', 'turnId': 'owner-turn', 'text': OWNER_WORDS}
    assert action['grantRevision'] == grant['revision'] and action['grantedBy'] == human()
    assert action['target'] == {'kind': 'server'} and action['interrupted'] == []
    # The gate stamped the restart in the same transaction (cooldown authority).
    assert isinstance(_approval_document(approvals)['last_restart_at'], float)


def test_running_conversations_refuse_unless_the_owner_asked_to_interrupt_them(d):
    d.grant()
    d.running = [{'taskId': 'worker-task', 'convId': 'worker-a', 'elapsed': 9.0}]
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'restart_would_interrupt' and 'worker-a' in str(refused.value)
    [refusal] = d.audit()
    assert refusal['status'] == 'refused' and refusal['code'] == 'restart_would_interrupt'
    assert d.detached == []
    accepted = d.restart(interrupt_running_conversations=True, idempotency_key='restart-anyway')
    assert accepted['status'] == 'accepted'
    d.run_detached()
    assert d.names()[-1] == 'perform'
    restarted = next(item for item in d.audit() if item['id'] == accepted['action_id'])
    assert restarted['status'] == 'restarting' and restarted['interrupted'] == ['worker-a']


def test_a_running_landing_is_waited_for_instead_of_refusing_the_restart(d):
    """Continuous landings must not starve restarts: a restart takes precedence and waits
    for the running landing (lib/runtime/protected_steps.py) instead of refusing."""
    d.grant()
    d.running = [{'taskId': 'protected-step:git_land-host-42-ab', 'convId': '', 'elapsed': 3.0,
                  'protectedStep': 'git_land', 'marker': '/data/protected-steps/git_land.json'}]
    accepted = d.restart(interrupt_running_conversations=False)
    assert accepted['status'] == 'accepted' and d.detached


def test_unreadable_activity_refuses_even_when_interrupting(d):
    d.grant()
    d.running = RuntimeError('worker_job.active.list unavailable')
    with pytest.raises(DelegationRefused) as refused:
        d.restart(interrupt_running_conversations=True)
    expected = 'restart_protection_unavailable'
    assert refused.value.code == expected
    # The caller's own conversation never counts against itself, exactly as in Settings.
    assert d.events == [('protect', 'router', None)] and d.detached == []
    [action] = d.audit()
    assert action['status'] == 'refused' and action['code'] == expected


def test_a_protected_step_that_starts_during_preparation_stops_the_restart_before_shutdown(d):
    d.grant()
    d.running = [[], [{'taskId': 'protected-step:export-host-7-cd', 'convId': '', 'elapsed': 0.1,
                       'protectedStep': 'export', 'marker': '/data/protected-steps/export.json'}]]
    d.restart(interrupt_running_conversations=True)
    d.run_detached()
    assert d.names() == ['protect', 'prepare', 'protect']
    [action] = d.audit()
    assert action['status'] == 'failed' and action['code'] == 'restart_protected_step'


def test_failed_preparation_keeps_the_old_server_and_reports_why(d):
    d.grant()
    d.prepare_error = 'frontend build failed: vite exited 1'
    d.restart()
    d.run_detached()
    assert d.names() == ['protect', 'prepare']
    [action] = d.audit()
    assert action['status'] == 'failed' and action['code'] == 'restart_preparation_failed'
    assert 'vite exited 1' in action['detail']


def test_a_replayed_restart_never_restarts_twice(d):
    d.grant()
    first = d.restart()
    d.run_detached()
    replay = d.restart()
    assert replay['action_id'] == first['action_id'] and replay['replayed'] is True
    assert replay['status'] == 'restarting'
    assert d.detached == [] and d.names().count('perform') == 1 and len(d.audit()) == 1


def test_a_handed_over_restart_reads_completed_once_a_newer_server_answers(d, monkeypatch):
    """Every restart replaces the serving worker, so a server generation that
    booted after the handover proves it happened."""
    from lib import boot_identity
    d.grant()
    first = d.restart()
    d.run_detached()
    [handed_over] = d.audit()
    assert handed_over['status'] == 'restarting'
    monkeypatch.setattr(boot_identity, 'BOOT_ID', 'next-generation')
    monkeypatch.setattr(boot_identity, 'BOOT_TS', handed_over['acceptedAt'] + 1)
    [completed] = d.audit()
    assert completed['status'] == 'completed' and 'next-generation' in completed['detail']
    replay = d.restart()
    assert (replay['action_id'], replay['status'], replay['replayed']) == (first['action_id'], 'completed', True)
    assert replay['execution_started'] is True and d.names().count('perform') == 1


def test_a_manager_refusal_after_the_handover_reads_failed_while_the_old_server_serves(
        d, approvals, monkeypatch):
    """The manager decides every refusal before it stops the old server, which then
    keeps serving and reads the outcome the manager recorded."""
    from lib import boot_identity
    d.grant()
    d.restart()
    d.run_detached()
    [handed_over] = d.audit()
    monkeypatch.setattr(boot_identity, 'BOOT_TS', handed_over['acceptedAt'] - 600)
    _manager_outcome(approvals, outcome='refused', stage='admission',
                     message='native storage service belongs to another host',
                     settledAt=handed_over['acceptedAt'] - 60)
    assert d.audit()[0]['status'] == 'restarting'  # an earlier restart's refusal
    _manager_outcome(approvals, outcome='refused', stage='admission',
                     message='native storage service belongs to another host',
                     settledAt=handed_over['acceptedAt'] + 1)
    [refused] = d.audit()
    assert refused['status'] == 'failed' and refused['code'] == 'restart_refused_by_manager'
    assert 'keeps serving' in refused['detail'] and 'another host' in refused['detail']


def test_a_restart_that_replaced_a_host_after_an_unclean_close_says_so(d, approvals, monkeypatch):
    from lib import boot_identity
    d.grant()
    d.restart()
    d.run_detached()
    [handed_over] = d.audit()
    _manager_outcome(approvals, outcome='spawned', settledAt=handed_over['acceptedAt'] + 1,
                     uncleanCloses=[{'service': 'native storage',
                                     'detail': 'clean close was not confirmed'}])
    monkeypatch.setattr(boot_identity, 'BOOT_ID', 'next-generation')
    monkeypatch.setattr(boot_identity, 'BOOT_TS', handed_over['acceptedAt'] + 2)
    [completed] = d.audit()
    assert completed['status'] == 'completed' and 'next-generation' in completed['detail']
    assert ('replaced the native storage host after an unclean close '
            '(clean close was not confirmed)') in completed['detail']


def test_delegated_session_control_is_recorded_and_acts_through_the_shared_command(d):
    """Continue and Stop under the grant: a refused trigger is recorded and runs nothing;
    an accepted one runs the shared session command as the owner's delegation, without
    the owner_delegation argument, and settles its record."""
    from lib.router.owner_delegation import delegated_conversation_control
    calls = []

    def control(task, scope, arguments, *, acting_principal):
        calls.append((dict(arguments), acting_principal))
        return {'turn': None, 'attempt': None}
    args = {'conversation_id': 'elsewhere', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1',
            'action': 'continue', 'expected_revision': 3, 'idempotency_key': 'after-restart',
            'owner_delegation': {'reason': 'The restart interrupted it', 'source_turn_id': 'event-turn'}}
    with pytest.raises(DelegationRefused) as refused:
        delegated_conversation_control(d.task, d.scope, args, control=control)
    assert refused.value.code == 'owner_delegation_not_granted' and d.audit() == []
    d.grant()
    with pytest.raises(ValueError):
        delegated_conversation_control(d.task, d.scope, {**args, 'action': 'regenerate'}, control=control)
    with pytest.raises(DelegationRefused) as refused:
        delegated_conversation_control(d.task, d.scope, args, control=control)
    assert refused.value.code == 'owner_delegation_trigger_invalid' and calls == []
    target = {'kind': 'conversation', 'conversationId': 'elsewhere', 'turnId': 'turn-1',
              'attemptId': 'attempt-1', 'action': 'continue'}
    [refusal] = d.audit()
    assert (refusal['status'], refusal['scope'], refusal['target']) == ('refused', 'conversation_control', target)
    accepted = {**args, 'owner_delegation': {'reason': 'The owner asked', 'source_turn_id': 'owner-turn'}}
    result = delegated_conversation_control(d.task, d.scope, accepted, control=control)
    [(arguments, principal)] = calls
    assert arguments == {key: value for key, value in args.items() if key != 'owner_delegation'}
    assert (principal.kind, principal.owner_user_id) == ('user', OWNER)
    assert principal.subject_id == 'owner-delegation:' + result['owner_delegation_action_id']
    action = d.audit()[0]
    assert (action['id'], action['status'], action['target']) == (
        result['owner_delegation_action_id'], 'completed', target)


@pytest.mark.parametrize('model', ['model-b', None])
def test_a_delegated_session_model_change_takes_the_same_r9_path_and_stays_auditable(d, model):
    """set_model under the grant (lib/conversations/session_model.py) is a delegated session
    control: grant, owner trigger and one audit record precede the shared command, which runs
    as the owner's delegation without the owner_delegation argument. Every record it writes,
    refused or completed, names the chosen model (None restores the original) and stays
    readable through the owner's audit view (contract ownerDelegatedAction)."""
    from lib.router.contract import validate
    from lib.router.owner_delegation import delegated_conversation_control
    calls = []

    def control(task, scope, arguments, *, acting_principal):
        calls.append((dict(arguments), acting_principal))
        return {'model': arguments['model'] or '', 'execution_started': False}
    args = {'conversation_id': 'worker-session', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1',
            'action': 'set_model', 'model': model, 'reason': 'Provider content-filter refusal',
            'idempotency_key': 'route-1',
            'owner_delegation': {'reason': 'Move it off the refusing provider', 'source_turn_id': 'event-turn'}}
    d.grant()
    with pytest.raises(DelegationRefused) as refused:
        delegated_conversation_control(d.task, d.scope, args, control=control)
    assert refused.value.code == 'owner_delegation_trigger_invalid' and calls == []
    accepted = {**args, 'owner_delegation': {**args['owner_delegation'], 'source_turn_id': 'owner-turn'}}
    result = delegated_conversation_control(d.task, d.scope, accepted, control=control)
    [(arguments, principal)] = calls
    assert arguments == {key: value for key, value in args.items() if key != 'owner_delegation'}
    assert (principal.kind, principal.owner_user_id) == ('user', OWNER)
    assert principal.subject_id == 'owner-delegation:' + result['owner_delegation_action_id']
    target = {'kind': 'conversation', 'conversationId': 'worker-session', 'turnId': 'turn-1',
              'attemptId': 'attempt-1', 'action': 'set_model', 'model': model}
    records = d.audit()
    assert [(record['status'], record['scope'], record['target']) for record in records] == [
        ('completed', 'conversation_control', target), ('refused', 'conversation_control', target)]
    assert records[0]['id'] == result['owner_delegation_action_id']
    for record in records:
        validate(record, 'ownerDelegatedAction')


def test_revocation_takes_effect_immediately_even_during_preparation(d):
    d.grant()
    d.restart()
    revoked = d.revoke()
    assert revoked['state'] == 'revoked' and revoked['revision'] == 2
    assert revoked['revokedBy']['kind'] == 'authenticated-request'
    d.run_detached()
    assert 'perform' not in d.names()
    [action] = d.audit()
    assert action['status'] == 'refused' and action['code'] == 'owner_delegation_not_granted'
    with pytest.raises(DelegationRefused) as refused:
        d.restart(idempotency_key='restart-2')
    assert refused.value.code == 'owner_delegation_not_granted'


@pytest.mark.parametrize('grantee', [{'conversationId': 'router-old'}, {'bindingRevision': 2}])
def test_only_a_grant_to_the_current_router_binding_authorizes(d, grantee):
    d.grant(**grantee)
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_router_changed'
    assert d.events == []


@pytest.mark.parametrize('provenance', [
    {'kind': 'authenticated-request', 'ownerUserId': OWNER, 'credential': 'header'},
    {'kind': 'explicit-user-instruction-via-codex-chat'},
    {**human(), 'browser': ''},
    {**human(), 'credential': 'header'},
    human(owner=OWNER + 1),
    None,
])
def test_a_grant_needs_the_owners_own_ui_provenance(approvals, provenance):
    with pytest.raises(PermissionError):
        gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=provenance)
    assert gate.delegation(OWNER)['state'] == 'revoked'


def test_open_mode_grants_are_recorded_as_advisory_and_owner_scoped(approvals):
    granted = gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=human(credential='open-mode',
                                                                                 browser='origin'))
    assert granted['assurance'] == 'advisory' and granted['grantedBy']['credential'] == 'open-mode'
    # Granting again is idempotent: the record and its provenance stay as first granted.
    assert gate.set_delegation(OWNER, True, grantee=ROUTER, provenance=human()) == granted
    assert gate.delegation(OWNER + 1)['state'] == 'revoked'
    with pytest.raises(DelegationRefused):
        gate.check_delegation(OWNER + 1, {**ROUTER, 'turnId': None, 'attemptId': None}, 'restart')


REQUESTER = {**ROUTER, 'turnId': None, 'attemptId': None}


def _store_grant(approvals, **fields):
    """Rewrite the stored grant as an earlier writer or a narrower grant left it (None drops a field)."""
    document = _approval_document(approvals)
    record = document['delegations'][str(OWNER)]
    for name, value in fields.items():
        if value is None:
            record.pop(name, None)
        else:
            record[name] = value
    (approvals / 'lifecycle_approvals.json').write_text(json.dumps(document))


def test_the_switch_grants_the_full_delegation_and_it_covers_every_scope(d):
    granted = d.grant()
    assert granted['coverage'] == 'full' and granted['scopes'] == list(gate.DELEGATION_SCOPES)
    assert 'scheduler' in gate.DELEGATION_SCOPES
    for scope in gate.DELEGATION_SCOPES:
        assert gate.check_delegation(OWNER, REQUESTER, scope)['coverage'] == 'full'


def test_a_full_grant_made_before_a_scope_existed_covers_it_without_the_owner_acting(d, approvals):
    """The owner's production grant of 2026-11-06: the switch's whole list of its day, no coverage field."""
    d.grant()
    _store_grant(approvals, coverage=None, scopes=['restart', 'conversation_control'])

    assert gate.check_delegation(OWNER, REQUESTER, 'scheduler')['state'] == 'granted'
    grant = gate.delegation(OWNER)
    assert grant['coverage'] == 'full' and grant['scopes'] == list(gate.DELEGATION_SCOPES)
    from lib.router.contract import validate
    validate(grant, 'ownerDelegationGrant')
    # Nothing was rewritten: the projection derives coverage, storage keeps what the owner granted.
    assert 'coverage' not in _approval_document(approvals)['delegations'][str(OWNER)]


@pytest.mark.parametrize('stored', [{'coverage': 'enumerated', 'scopes': ['restart']},
                                    {'coverage': None, 'scopes': ['restart']},
                                    {'coverage': 'unknown-future-value', 'scopes': ['restart']}])
def test_an_enumerated_grant_stays_as_narrow_as_it_was_granted(d, approvals, stored):
    d.grant()
    _store_grant(approvals, **stored)

    assert gate.check_delegation(OWNER, REQUESTER, 'restart')['scopes'] == ['restart']
    for scope in ('conversation_control', 'scheduler'):
        with pytest.raises(DelegationRefused) as refused:
            gate.check_delegation(OWNER, REQUESTER, scope)
        assert refused.value.code == 'owner_delegation_scope'
        # The refusal names what the grant covers and the owner's remedy.
        assert 'covers only restart' in str(refused.value) and 'Act for me' in str(refused.value)
    grant = gate.delegation(OWNER)
    assert (grant['coverage'], grant['scopes']) == ('enumerated', ['restart'])
    # The owner's switch grants the full delegation over it; nothing else widens it.
    widened = d.grant()
    assert widened['coverage'] == 'full' and widened['revision'] == grant['revision'] + 1
    assert gate.check_delegation(OWNER, REQUESTER, 'scheduler')['state'] == 'granted'


def test_the_state_the_owner_reads_names_the_coverage_and_every_covered_scope(d, monkeypatch):
    from lib.router.owner_delegation import owner_delegation_state
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create=False: {
        'conversationId': 'router', 'revision': 1, 'availability': 'available'})
    assert owner_delegation_state(OWNER)['grant']['coverage'] == 'full'  # what the switch would grant
    d.grant()
    state = owner_delegation_state(OWNER)
    assert state['active'] and state['grant']['coverage'] == 'full'
    assert state['grant']['scopes'] == list(gate.DELEGATION_SCOPES)
    assert {'restart', 'conversation_control', 'scheduler'} <= set(state['grant']['scopes'])


def test_a_tampered_grant_record_authorizes_nothing(d, approvals):
    d.grant()
    document = _approval_document(approvals)
    document['delegations'][str(OWNER)]['grantedBy'] = {'kind': 'explicit-user-instruction-via-codex-chat'}
    (approvals / 'lifecycle_approvals.json').write_text(json.dumps(document))
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_not_granted'


def test_an_owner_message_trigger_must_be_the_current_request(d):
    d.grant()
    with pytest.raises(DelegationRefused) as refused:
        d.restart(source_turn_id='event-turn')
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    d.source = PermissionError('Synthetic input cannot authorize new workers')
    with pytest.raises(DelegationRefused) as refused:
        d.restart()
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    with pytest.raises(DelegationRefused) as refused:
        d.restart(source_turn_id=None)
    assert refused.value.code == 'owner_delegation_trigger_missing'
    with pytest.raises(ValueError):
        d.restart(reason='')
    assert d.events == []
    # Under the grant every refused claim of owner authority is a recorded use, a missing
    # trigger included; a call whose own arguments are malformed (an empty reason) is not.
    assert [(item['status'], item['code'], item['trigger']) for item in d.audit()] == [
        ('refused', 'owner_delegation_trigger_missing', None)] + [
        ('refused', 'owner_delegation_trigger_invalid', None)] * 2


def test_a_standing_instruction_authorizes_a_nonhuman_turn_and_a_paused_one_does_not(d):
    d.turns['owner-turn']['projection'] = {'content': 'translated', 'originalContent': INSTRUCTION}
    instruction = d.create()
    d.grant()
    d.source = PermissionError('the current input is a channel event, not the owner')
    d.task['_principalContext'] = PrincipalContext.system(
        subject_id='router-notice', owner_user_id=OWNER, scopes={'chat'}).to_payload()
    arguments = {'source_turn_id': None, 'instruction_id': instruction['instructionId'],
                 'event_id': 'dx:msg:77'}
    RouterInstructions(OWNER).pause(instruction['instructionId'])
    with pytest.raises(DelegationRefused) as refused:
        d.restart(**arguments)
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    RouterInstructions(OWNER).resume(instruction['instructionId'])
    assert d.restart(**arguments)['status'] == 'accepted'
    assert ('live-router-attempt',) in d.events
    assert d.audit()[0]['trigger'] == {'kind': 'standing_instruction', 'turnId': 'owner-turn',
                                       'instructionId': instruction['instructionId'], 'eventId': 'dx:msg:77',
                                       'text': INSTRUCTION}


def test_an_owner_message_steered_into_the_reply_is_a_trigger(d, monkeypatch):
    d.grant()
    d.source = PermissionError('the reply was started by a report relay')

    def steer(task, scope, steer_id):
        assert steer_id == 'steer-1'
        return {'conversationId': 'router', 'turnId': 'router-turn', 'actor': 'human',
                'projection': {'content': OWNER_WORDS}}
    monkeypatch.setattr(forwarding, '_current_steer_source', steer)
    assert d.restart(source_turn_id=None, source_steer_id='steer-1')['status'] == 'accepted'
    assert d.audit()[0]['trigger'] == {'kind': 'owner_steer', 'turnId': 'router-turn', 'steerId': 'steer-1',
                                       'text': OWNER_WORDS}


@pytest.mark.parametrize('trigger,code', [
    pytest.param({'instruction_id': 'ri-1'}, 'owner_delegation_trigger_malformed', id='instruction-alone'),
    pytest.param({'instruction_id': 'ri-1', 'event_id': 'dx:msg:77', 'source_turn_id': 'event-turn'},
                 'owner_delegation_trigger_malformed', id='instruction-with-both-triggers'),
    pytest.param({'instruction_id': 'ri-1', 'event_id': 'dx:msg:77', 'source_steer_id': 'steer-1'},
                 'owner_delegation_trigger_malformed', id='instruction-with-steer'),
    pytest.param({'event_id': 'dx:msg:77'}, 'owner_delegation_trigger_malformed', id='event-without-instruction'),
    pytest.param({'source_turn_id': 'owner-turn', 'source_steer_id': 'steer-1'},
                 'owner_delegation_trigger_malformed', id='message-and-steer'),
    pytest.param({}, 'owner_delegation_trigger_missing', id='no-trigger'),
])
def test_a_malformed_trigger_is_a_recorded_refusal_before_anything_runs(d, trigger, code):
    d.grant()
    with pytest.raises(DelegationRefused) as refused:
        d.restart(**{'source_turn_id': None, 'interrupt_running_conversations': True, **trigger})
    assert refused.value.code == code and refused.value.execution_started is False
    assert 'exactly one of' in str(refused.value)
    assert d.events == [] and d.detached == []
    [refusal] = d.audit()
    assert (refusal['status'], refusal['code'], refusal['scope'], refusal['trigger'], refusal['target']) == (
        'refused', code, 'restart', None, {'kind': 'server'})
    assert refusal['reason'] == 'The owner asked for a restart' and str(refused.value) in refusal['detail']


def test_an_instruction_without_its_trigger_is_refused_and_a_corrected_call_restarts_once(d):
    """2026-11-10 03:46: restart_server(instruction_id=…) without event_id or source_turn_id
    returned a plain error that nothing recorded, and the call read as a restart."""
    d.turns['owner-turn']['projection'] = {'content': 'translated', 'originalContent': INSTRUCTION}
    instruction = d.create()
    d.grant()
    d.source = PermissionError('the current input is a channel event, not the owner')
    d.task['_principalContext'] = PrincipalContext.system(
        subject_id='router-notice', owner_user_id=OWNER, scopes={'chat'}).to_payload()
    call = {'source_turn_id': None, 'instruction_id': instruction['instructionId'],
            'interrupt_running_conversations': True}
    with pytest.raises(DelegationRefused) as refused:
        d.restart(**call)
    assert refused.value.code == 'owner_delegation_trigger_malformed'
    assert 'With instruction_id, give exactly one of event_id or source_turn_id' in str(refused.value)
    assert d.events == [] and d.detached == []
    [refusal] = d.audit()
    assert (refusal['status'], refusal['code']) == ('refused', 'owner_delegation_trigger_malformed')
    # A refusal never consumes the idempotency key: the corrected call is accepted once.
    accepted = d.restart(**call, event_id='dx:msg:77')
    assert accepted['status'] == 'accepted' and accepted['replayed'] is False
    assert [item['status'] for item in d.audit()] == ['accepted', 'refused']


def test_a_malformed_trigger_refuses_delegated_session_control_and_is_recorded(d):
    from lib.router.owner_delegation import delegated_conversation_control
    calls = []

    def control(task, scope, arguments, *, acting_principal):
        calls.append(arguments)
        return {}
    d.grant()
    for action in ('continue', 'stop'):
        with pytest.raises(DelegationRefused) as refused:
            delegated_conversation_control(d.task, d.scope, {
                'conversation_id': 'elsewhere', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1',
                'action': action, 'expected_revision': 3, 'idempotency_key': action,
                'owner_delegation': {'reason': 'The restart interrupted it', 'instruction_id': 'ri-1'}},
                control=control)
        assert refused.value.code == 'owner_delegation_trigger_malformed'
    assert calls == []
    assert [(item['status'], item['scope'], item['code'], item['target']['action']) for item in d.audit()] == [
        ('refused', 'conversation_control', 'owner_delegation_trigger_malformed', action)
        for action in ('stop', 'continue')]


def test_the_contract_enforces_the_trigger_rule_and_the_model_facing_schemas_state_it():
    from lib.router.contract import validate
    from lib.tools.coordinator import COORDINATOR_TOOLS, ROUTER_TOOL_SCHEMAS
    for trigger in ({'source_turn_id': 'turn'}, {'source_steer_id': 'steer'},
                    {'instruction_id': 'ri-1', 'event_id': 'dx:msg:1'},
                    {'instruction_id': 'ri-1', 'source_turn_id': 'turn'}):
        assert validate({'reason': 'asked', **trigger}, 'ownerDelegationTrigger')
    with pytest.raises(ValueError, match='With instruction_id, give exactly one of event_id or source_turn_id'):
        validate({'reason': 'asked', 'instruction_id': 'ri-1'}, 'ownerDelegationTrigger')
    tools = {tool['function']['name']: tool['function'] for tool in [*ROUTER_TOOL_SCHEMAS, *COORDINATOR_TOOLS]}
    restart = tools['restart_server']
    delegation = tools['control_conversation']['parameters']['properties']['owner_delegation']
    for text in (restart['description'], restart['parameters']['properties']['instruction_id']['description'],
                 delegation['description'], delegation['properties']['instruction_id']['description']):
        assert 'exactly one of event_id or source_turn_id' in text, text
    # Providers reject combinators in tool parameters: the rule stays out of the wire schema.
    for schema in (restart['parameters'], delegation):
        assert not {'allOf', 'anyOf', 'oneOf', 'if', 'not'} & set(schema), schema


def _router_pipeline(d, monkeypatch):
    """Run one Global Router tool call through the real tool pipeline, as the model would."""
    from types import SimpleNamespace as Binding
    from lib.conversations import project_coordination
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS, ROUTER_TOOL_SCHEMAS
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope',
                        lambda task, project_path=None: dict(d.scope))
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: Binding(
        read=lambda path: {'projectPath': path, 'conversationId': 'router', 'revision': 1,
                           'availability': 'available'}))
    # This Turn answers the owner, not a review of other people's channel messages (CHANNELS.md C11).
    monkeypatch.setattr('lib.channels.assistant.review_tool_ceiling', lambda task, scope: None)
    d.task.update({key: value for key, value in _mk_task().items() if key not in d.task})
    d.task['config'].update(autoApply=True)
    # Without a Turn/Attempt binding the effect journal is process-scoped, so no Turn store is needed.
    for key in ('_turnId', '_attemptId'):
        d.task.pop(key)

    def run(name, arguments):
        parsed = _mk_tc('call-' + name, name, 0, args=arguments)
        d.task['toolRounds'] = [parsed[5]]
        messages = []
        execute_tool_pipeline(d.task, [parsed], d.task['config'], d.scope['project_path'], True,
                              [*ROUTER_TOOL_SCHEMAS, *COORDINATOR_TOOLS], messages, [], 0, 'test-model')
        return parsed[5], messages[-1]['content']
    return run


@pytest.mark.parametrize('name,arguments', [
    pytest.param('restart_server', {'reason': 'The owner asked for a restart', 'instruction_id': 'ri-1',
                                    'interrupt_running_conversations': True, 'idempotency_key': 'restart-1'},
                 id='restart'),
    *[pytest.param('control_conversation', {
        'conversation_id': 'elsewhere', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1', 'action': action,
        'expected_revision': 3, 'idempotency_key': action,
        'owner_delegation': {'reason': 'The restart interrupted it', 'instruction_id': 'ri-1'}}, id=action)
      for action in ('continue', 'stop')],
])
def test_a_refused_delegated_call_reads_failed_in_the_round_transcript_and_model_view(
        d, monkeypatch, name, arguments):
    """The refusal came back in 0.1 s as {"error": …, "execution_started": false} and the
    transcript showed the call as [done]; a refusal is a failed call wherever it is read."""
    from lib.conv_ref import _format_tool_rounds
    d.grant()
    round_entry, content = _router_pipeline(d, monkeypatch)(name, arguments)
    assert round_entry['status'] == 'error', round_entry
    assert '[error]' in _format_tool_rounds([round_entry]) and '[done]' not in _format_tool_rounds([round_entry])
    view = json.loads(content)
    assert view['status'] == 'error', view
    [refusal] = view['items']
    assert refusal['code'] == 'owner_delegation_trigger_malformed' and refusal['execution_started'] is False
    assert d.events == [] and d.detached == []
    [record] = d.audit()
    assert (record['status'], record['code']) == ('refused', 'owner_delegation_trigger_malformed')


def test_an_accepted_restart_still_reads_done_through_the_pipeline(d, monkeypatch):
    d.grant()
    round_entry, content = _router_pipeline(d, monkeypatch)('restart_server', {
        'reason': 'The owner asked for a restart', 'source_turn_id': 'owner-turn', 'idempotency_key': 'restart-1'})
    assert round_entry['status'] == 'done', round_entry
    assert json.loads(content)['status'] == 'accepted'
    assert len(d.detached) == 1 and d.audit()[0]['status'] == 'accepted'


SET_MODEL = {'conversation_id': 'worker-session', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1',
             'action': 'set_model', 'model': 'model-b', 'reason': 'Provider content-filter refusal',
             'idempotency_key': 'route-1'}
SET_MODEL_TARGET = {'kind': 'conversation', 'conversationId': 'worker-session', 'turnId': 'turn-1',
                    'attemptId': 'attempt-1', 'action': 'set_model', 'model': 'model-b'}


@pytest.mark.parametrize('trigger,code', [
    pytest.param({'instruction_id': 'ri-1'}, 'owner_delegation_trigger_malformed', id='instruction-alone'),
    pytest.param({}, 'owner_delegation_trigger_missing', id='no-trigger'),
])
def test_a_delegated_set_model_obeys_the_trigger_rule_and_its_refusal_reads_failed(d, monkeypatch, trigger, code):
    """set_model under the grant obeys the owner trigger rule (ownerDelegationTrigger) of every
    delegated action: a missing or malformed trigger is a recorded refusal naming the chosen
    model, the session's model never changes, and the round reads failed wherever it is read."""
    from lib.conv_ref import _format_tool_rounds
    from lib.conversations import session_model
    from lib.router.contract import validate
    changed = []
    monkeypatch.setattr(session_model, 'set_session_model', lambda *args, **_: changed.append(args))
    d.grant()
    round_entry, content = _router_pipeline(d, monkeypatch)('control_conversation', {
        **SET_MODEL, 'owner_delegation': {'reason': 'Move it off the refusing provider', **trigger}})
    assert round_entry['status'] == 'error', round_entry
    assert '[error]' in _format_tool_rounds([round_entry]) and '[done]' not in _format_tool_rounds([round_entry])
    view = json.loads(content)
    assert view['status'] == 'error', view
    [refusal] = view['items']
    assert refusal['code'] == code and refusal['execution_started'] is False
    assert 'exactly one of' in refusal['error'] and changed == []
    [record] = d.audit()
    assert (record['status'], record['code'], record['trigger'], record['target']) == (
        'refused', code, None, SET_MODEL_TARGET)
    validate(record, 'ownerDelegatedAction')


def test_a_delegated_set_model_the_command_refuses_settles_failed_and_reads_failed(d, monkeypatch):
    """With a genuine trigger the gate records the action first. When the shared command then
    refuses (the owner's route resolver does not admit the model, docs/ROUTER.md R5), nothing
    changed: the record settles failed and the round reads failed, never a completed change."""
    from lib.conversations import session_model
    from lib.router.contract import validate

    def refuse(task, scope, principal, arguments, **_):
        raise ValueError(f"model {arguments['model']!r} is not routable for this owner: unknown model")
    monkeypatch.setattr(session_model, 'set_session_model', refuse)
    d.grant()
    round_entry, content = _router_pipeline(d, monkeypatch)('control_conversation', {
        **SET_MODEL, 'owner_delegation': {'reason': 'Move it off the refusing provider',
                                          'source_turn_id': 'owner-turn'}})
    assert round_entry['status'] == 'error', round_entry
    view = json.loads(content)
    assert view['status'] == 'error', view
    [refusal] = view['items']
    assert 'not routable' in refusal['error']
    [record] = d.audit()
    assert (record['status'], record['code'], record['target']) == ('failed', 'ValueError', SET_MODEL_TARGET)
    assert record['trigger']['kind'] == 'owner_message' and 'not routable' in record['detail']
    validate(record, 'ownerDelegatedAction')


def test_a_project_lead_restarts_under_the_full_grant_on_its_own_judgment(d):
    """Owner decision 2026-11-18: the owner's full grant reaches every lead. A Project
    Router restarts with under_delegation (no owner message asks); the record names it
    as project_lead, and revocation refuses the next call at once."""
    from lib.router.owner_delegation import LEAD_DELEGATION_WORDS, restart_server
    lead_scope = {**d.scope, 'project_path': d.project, 'conversation_id': 'coordinator-project'}
    call = {'reason': 'Merged fixes are live only after a restart', 'under_delegation': True,
            'idempotency_key': 'lead-restart-1'}
    with pytest.raises(DelegationRefused) as refused:
        restart_server(d.task, lead_scope, call)
    assert refused.value.code == 'owner_delegation_not_granted'
    d.grant()
    accepted = restart_server(d.task, lead_scope, call)
    assert accepted['status'] == 'accepted' and len(d.detached) == 1
    [record] = d.audit()
    assert record['requester']['kind'] == 'project_lead'
    assert record['requester']['conversationId'] == 'coordinator-project'
    assert record['trigger'] == {'kind': 'owner_delegation', 'turnId': 'router-turn',
                                 'text': LEAD_DELEGATION_WORDS}
    from lib.router.contract import validate
    validate(record, 'ownerDelegatedAction')
    d.revoke()
    with pytest.raises(DelegationRefused) as refused:
        restart_server(d.task, lead_scope, {**call, 'idempotency_key': 'lead-restart-2'})
    assert refused.value.code == 'owner_delegation_not_granted'


def test_under_delegation_is_a_trigger_alone(d):
    d.grant()
    with pytest.raises(DelegationRefused) as refused:
        d.restart(under_delegation=True)  # with source_turn_id
    assert refused.value.code == 'owner_delegation_trigger_malformed'


def test_every_lead_holds_restart_and_workers_never_do():
    from lib.project_dispatch.contract import CONTRACT
    from lib.router.policy import ROUTER_ONLY_TOOLS, ROUTER_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    # Owner decision 2026-11-18 (docs/ROUTER.md R9): restart moved from the Router-only
    # ceiling to every lead's; the gate still refuses without the owner's grant.
    assert 'restart_server' in COORDINATOR_TOOLS and 'restart_server' not in ROUTER_ONLY_TOOLS
    assert 'restart_server' in CONTRACT['workerDeniedTools']
    assert not {name for name in ROUTER_TOOLS if 'grant' in name or 'delegation' in name}


def test_workers_submit_their_own_checkpoint():
    """A Worker's integration_submit only checkpoints its isolated task workspace for the
    automatic delivery; it starts nothing outside its grant (owner decision 2026-11-18)."""
    from lib.project_dispatch.contract import CONTRACT
    assert 'integration_submit' not in CONTRACT['workerDeniedTools']
    assert 'integration_checkpoint' not in CONTRACT['workerDeniedTools']


# -- The boundary: the grant lets the Router act as the owner, never speak as the owner. --

def test_with_the_grant_router_text_still_never_becomes_the_owners_words(d):
    """Answering for the owner means forwarding words the owner did not write; the
    grant lets no nonhuman turn forward, record standing authority or create workers."""
    d.grant()
    d.source = PermissionError('Synthetic input cannot authorize new workers')
    with pytest.raises(PermissionError):
        d.call('route_to_project', project_path=d.project, source_turn_id='owner-turn')
    with pytest.raises(PermissionError):
        d.call('router_instruction_create', source_turn_id='owner-turn', label='Self-made',
               idempotency_key='self')
    assert d.calls == []


def test_with_the_grant_a_synthetic_request_still_cannot_start_workers(d, monkeypatch):
    """Irreversible publications (for example pushing an edition to GitHub) run in
    Workers or behind owner approvals; only a genuine owner request starts one."""
    d.grant()
    from lib.project_dispatch.delegation import source_for_task
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope', lambda task: dict(d.scope))
    monkeypatch.setattr('lib.project_dispatch.continuation.current_continuation',
                        lambda task, **kwargs: None)
    d.task['_principalContext'] = PrincipalContext.system(
        subject_id='router-notice', owner_user_id=OWNER, scopes={'chat'}).to_payload()
    with pytest.raises(PermissionError, match='Synthetic input'):
        source_for_task(SimpleNamespace(owner=OWNER, project=d.scope['project_path']), d.task)


def test_with_the_grant_owner_prompts_still_need_the_owner(d, monkeypatch):
    """Channel drafts and thread policy stay genuine-human-turn decisions, and a
    lifecycle approval request still needs the owner's own UI decision."""
    d.grant()
    from lib.channels import tools as channel_tools
    from lib.channels.model import ChannelError
    monkeypatch.setattr('lib.channels.delivery.is_global_router', lambda owner, conversation: True)
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', lambda conversation, turn, **kwargs: {
        'conversationId': 'router', 'turnId': turn, 'actor': 'assistant', 'parentTurnId': 'event-turn'}
        if turn == 'router-turn' else {'conversationId': 'router', 'turnId': turn, 'actor': 'human',
                                       'projection': {'content': 'bug', 'origin': {'blockId': 'o',
                                                                                   'initiator': 'channel'}}})
    for name, args in (('channel_outbox_approve', {'item_id': 'draft-1', 'decision': 'approve'}),
                       ('channel_configure_thread', {'thread': 'dx:gid:1', 'mode': 'watch'})):
        with pytest.raises(ChannelError) as refused:
            channel_tools.execute(name, args, d.task)
        assert refused.value.code == 'human_turn_required'


def test_an_agent_cannot_approve_a_lifecycle_request(approvals):
    record = gate.create_request('restart', origin={'ua': 'router'})
    for provenance in ({'kind': 'explicit-user-instruction-via-codex-chat'}, None,
                       {**human(), 'browser': ''}):
        with pytest.raises(PermissionError):
            gate.decide(record['id'], True, provenance=provenance)
    assert gate.get(record['id'])['status'] == 'pending'
    decided = gate.decide(record['id'], True, provenance=human())
    assert decided['status'] == 'approved' and decided['decided_via'] == human()
    assert decided['decided_assurance'] == 'authenticated'
