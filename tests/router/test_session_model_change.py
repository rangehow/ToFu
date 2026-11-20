"""The owner or delegated Global Router changes a session's model, not its authority.

A provider refusal ends in a model change on the same conversation, grant,
checkout and account, never in a clean replacement session. The choice is an
owner's (docs/ROUTER.md R5, R9): route-admitted, audited, applied from the next
accepted attempt — never to an in-flight attempt's config.
"""
from types import SimpleNamespace

import pytest

from lib.conversations import session_model
from lib.identity import PrincipalContext
from tests.project_mod.test_coordinator_workers import runtime as worker_runtime
from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_project_dispatch_repository import context

__all__ = ('base_runtime', 'context', 'worker_runtime')

pytestmark = pytest.mark.unit


def owner_principal():
    return PrincipalContext.user(subject_id='user:7', owner_user_id=7, tenant_id='tenant-a')


def test_a_session_model_is_admitted_by_the_owners_real_route_resolver(monkeypatch):
    """The choice passes the same admission as workerModel (admit_worker_model, R5), whose
    refusal names the field the owner set; a refused choice writes nothing."""
    import lib.model_routing as routing
    from lib.conversations import settings_store
    settings = {}
    monkeypatch.setattr(session_model, '_conversation_settings', lambda conversation_id, owner: dict(settings))

    def update(conversation_id, mutate, *, user_id):
        assert (conversation_id, user_id) == ('worker-session', 7)
        mutate(settings)
        return settings
    monkeypatch.setattr(settings_store, 'update_conversation_settings', update)
    resolved = []

    class Repository:
        def get(self, boundary):
            resolved.append((boundary.owner_user_id, boundary.tenant_id))
            return SimpleNamespace(document={})

    def resolve(document, model):
        if model != 'routable-model':
            raise routing.ModelRoutingError('unknown model', field='model')
        return 'selection'
    monkeypatch.setattr(routing, 'ModelRoutingRepository', Repository)
    monkeypatch.setattr(routing, 'resolve_compatible_model', resolve)
    with pytest.raises(ValueError, match=r"^model 'made-up-model' is not routable"):
        session_model.store_session_model('worker-session', 'made-up-model', owner=7, tenant_id='tenant-a')
    assert settings == {}
    stored = session_model.store_session_model('worker-session', 'routable-model', owner=7, tenant_id='tenant-a')
    assert stored == {'conversationId': 'worker-session', 'modelOverride': 'routable-model', 'previousModel': ''}
    assert settings == {'modelOverride': 'routable-model'} and resolved == [(7, 'tenant-a')] * 2


@pytest.fixture
def routed(worker_runtime, monkeypatch):
    """One delegated Worker plus the recorded owner model-route admissions and audit."""
    from lib.project_dispatch import delegation
    from lib.conversations import settings_store
    admitted = []
    monkeypatch.setattr(delegation, 'admit_worker_model',
                        lambda owner, model, **kwargs: admitted.append((owner, model)) or model)
    audited = []
    monkeypatch.setattr(session_model, 'audit_log', lambda event, **fields: audited.append((event, fields)))
    r = worker_runtime
    from lib import turn_lifecycle
    monkeypatch.setattr(turn_lifecycle, 'get_attempt', r.c.repo.attempt_get)
    # The base dispatch fixture stubs the settings seam to 'no override'; this
    # suite exercises the real settings the Worker conversation carries.
    monkeypatch.setattr(session_model, '_conversation_settings',
                        lambda conversation_id, owner: dict(
                            (r.c.conversations.rows.get(conversation_id) or {}).get('settings') or {}))

    def update(conversation_id, mutate, *, user_id):
        row = r.c.conversations.rows.get(conversation_id)
        if row is None:
            return None
        mutate(row['settings'])
        return row['settings']
    monkeypatch.setattr(settings_store, 'update_conversation_settings', update)
    # Fresh grants now read the Coordinator's stored selection at admission.
    r.c.conversations.rows['coordinator']['settings']['workerModel'] = 'model-a'
    result = r.call('delegate_project_work', **r.args)
    assert 'error' not in result, result
    row = r.c.repo.read(result['assignment_id'])
    assert row['request']['model'] == 'model-a'
    # The Worker's accepted attempt carries its execution identity, as storage
    # records it; the tenant fence below reads exactly this config.
    from lib.project_dispatch.admission import TrustedDispatch
    trusted = TrustedDispatch(r.c.repo.admission(row['id']))
    config = trusted.capture({}, 7, trusted.principal, result['conversation_id'])
    config['_turnOwnerUserId'] = 7
    r.attempts[row['execution']['attemptId']]['config'] = config
    r.attempts[row['execution']['attemptId']]['status'] = 'interrupted'
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator',
             'binding_revision': 1}
    return SimpleNamespace(r=r, row=row, admitted=admitted, audited=audited,
                           conversation_id=result['conversation_id'], scope=scope)


def _set(m, **kwargs):
    arguments = {'conversation_id': m.conversation_id, 'turn_id': m.row['execution']['turnId'],
                 'attempt_id': m.row['execution']['attemptId'], 'model': 'model-b',
                 'reason': 'Anthropic content-filter refusal', 'idempotency_key': 'route-1', **kwargs}
    return session_model.set_session_model(m.r.task, m.scope, owner_principal(), arguments)


def _trusted(m):
    from lib.project_dispatch.admission import TrustedDispatch
    return TrustedDispatch(m.r.c.repo.admission(m.row['id']))


def test_model_change_preserves_grant_checkout_and_account(routed):
    from lib.project_dispatch import admission
    m = routed
    trusted = _trusted(m)
    # Captured before the change: the in-flight attempt's accepted config.
    in_flight = trusted.capture({}, 7, trusted.principal, m.conversation_id)
    in_flight['_turnOwnerUserId'] = 7
    answer = _set(m)
    assert answer['model'] == 'model-b' and answer['previous_model'] == ''  # no override was stored
    assert answer['execution_started'] is False  # a setting, not a resume
    assert m.admitted == [(7, 'model-b')]
    assert m.r.c.repo.read(m.row['id'])['grant'] == m.row['grant']
    # The next accepted attempt runs the new model on the same grant and checkout.
    resumed = trusted.capture({}, 7, trusted.principal, m.conversation_id)
    assert resumed['model'] == 'model-b'
    assert in_flight['model'] == 'model-a'  # no rewrite mid-call
    assert resumed[admission.KEY] == in_flight[admission.KEY]
    resumed['_turnOwnerUserId'] = 7
    task = {'id': 'worker-task', 'convId': m.conversation_id, 'config': resumed,
            '_userId': 7, '_principalContext': trusted.principal.to_payload(), '_attended': False,
            '_turnId': m.row['execution']['turnId'], '_attemptId': m.row['execution']['attemptId'],
            'status': 'running', 'usage': {}}
    admission.bind_account(task)  # the change keeps the grant, account and work id
    assert not task.get('aborted')
    # The bound account and work id survive; no new account is created.
    assert task['_executionBudget'] is not None
    assert task['_projectDispatchBinding'] == resumed[admission.KEY]


def _running(m):
    """Accept the session's next attempt as storage records it, bind it, return its live task."""
    from copy import deepcopy
    from lib.project_dispatch import admission
    trusted = _trusted(m)
    config = trusted.capture({}, 7, trusted.principal, m.conversation_id)
    config['_turnOwnerUserId'] = 7
    m.r.attempts[m.row['execution']['attemptId']].update(
        taskId='worker-task', status='running', config=deepcopy(config))
    task = {'id': 'worker-task', 'convId': m.conversation_id, 'config': config,
            '_userId': 7, '_principalContext': trusted.principal.to_payload(), '_attended': False,
            '_turnId': m.row['execution']['turnId'], '_attemptId': m.row['execution']['attemptId'],
            'status': 'running', 'usage': {}}
    admission.bind_account(task)
    assert not task.get('aborted')
    return task


@pytest.mark.parametrize('accepted_under, later', [
    (None, 'model-c'), ('model-b', 'model-c'), ('model-b', None)])
def test_an_in_flight_attempt_never_loses_authority_when_the_model_changes(routed, accepted_under,
                                                                          later):
    """The owner changes the model again, or restores the original, while an attempt
    accepted under an earlier choice runs: it keeps that model and its live authority,
    so a refusal-driven change never cancels or invalidates work already under way."""
    from lib.project_dispatch import admission
    m = routed
    if accepted_under:
        _set(m, model=accepted_under)
    task = _running(m)
    accepted = task['config']['model']
    assert accepted == (accepted_under or 'model-a')
    _set(m, model=later, idempotency_key='route-again')
    admission.validate_task(task)  # must not abort: the accepted model is unchanged
    assert not task.get('aborted')
    assert task['config']['model'] == accepted


@pytest.mark.parametrize('accepted_under', [None, 'model-b'])
def test_a_carrier_rewritten_to_an_unchosen_model_loses_its_authority(routed, accepted_under):
    """Only the grant's model, the owner's current choice for the session, or the model
    its bound attempt was durably accepted with keeps dispatch authority. A live carrier
    rewritten to any other model is refused, even when it forges a private acceptance
    stamp beside it, so a session never spends outside the owner's choice."""
    from lib.llm.llm_errors import AbortedError
    from lib.project_dispatch import admission
    m = routed
    if accepted_under:
        _set(m, model=accepted_under)
    task = _running(m)
    task['config']['model'] = 'model-x'
    task['config']['_projectDispatchAcceptedModel'] = 'model-x'
    with pytest.raises(AbortedError):
        admission.validate_task(task)


def test_a_follow_up_of_the_same_assignment_inherits_the_change(routed):
    m = routed
    trusted = _trusted(m)
    _set(m)
    old = m.row['execution']
    follows = type(trusted)(m.r.c.repo.admission(m.row['id']), follows={
        'execution': dict(old), 'source': {'attemptId': old['attemptId'], 'turnId': old['turnId'],
                                           'taskId': 'worker-task', 'status': 'completed'}})
    from lib.project_dispatch.admission import FOLLOW_UP_KEY
    config = follows.capture({}, 7, trusted.principal, m.conversation_id)
    assert config['model'] == 'model-b'
    assert config[FOLLOW_UP_KEY]['attemptId'] == old['attemptId']


def test_new_workers_inherit_the_routers_current_worker_model(routed):
    """New Workers follow the Router's current Worker model, not a request's frozen pin.

    The session override above is deliberately per-session: it re-routes that one
    session only and never changes unrelated work. Workers this request delegates
    next read the owner setting live.
    """
    m = routed
    from lib.project_dispatch.delegation import capture_worker_policy
    # This request already delegated, so its issued policy is replayed from the
    # first grant. The new delegation must still resolve the live owner setting.
    m.r.c.conversations.rows['coordinator']['settings']['workerModel'] = 'model-b'
    # A fresh tool assembly of the Coordinator's next round issues the policy;
    # even the replay of this request's first grant must not re-pin the old model.
    capture_worker_policy(m.r.task, m.r.task['config'], [
        {'function': {'name': name}} for name in ('run_command', 'read_files')], 'model-a')
    result = m.r.call('delegate_project_work', title='Another part', brief='Another part.',
                      idempotency_key='part-two')
    assert m.r.c.repo.read(result['assignment_id'])['request']['model'] == 'model-b'


def test_a_coordinator_continuation_resolves_the_live_choice_not_the_frozen_one(routed):
    from lib.project_dispatch import continuation
    from lib.project_dispatch.delegation import capture_worker_policy, issued_worker_policy
    m = routed
    _set(m)
    # The Coordinator's Worker-model setting now reads model-b (the owner's new
    # default for this Router); the continuation must not replay its frozen pin.
    m.r.c.conversations.rows['coordinator']['settings']['workerModel'] = 'model-b'
    m.r.c.repo._load = m.r.c.repo._load  # unchanged storage
    task = {'id': 'coordinator-task', 'config': {}, '_userId': 7,
            '_coordinatorDispatchScope': {'project_path': '/project', 'binding_revision': 1,
                                          'conversation_id': 'coordinator', 'user_id': 7}}
    controls = {'version': 1, 'model': 'model-a', 'workerModel': 'model-a',
                'workerTools': ['run_command'], 'tools': ['report_to_user']}
    record = {'request': {'delegation': {'policy': {'config': {}, 'continuation': controls}}}}
    import lib.project_dispatch.continuation as cont
    original = cont.current_continuation
    cont.current_continuation = lambda task, **kwargs: record
    try:
        capture_worker_policy(task, {}, [], 'model-a')
        assert issued_worker_policy(m.r.c.repo, task).model == 'model-b'
    finally:
        cont.current_continuation = original


def test_only_an_owner_principal_changes_a_session_model(routed):
    m = routed
    synthetic = PrincipalContext.system(subject_id='project-dispatch:x', owner_user_id=7,
                                        tenant_id='tenant-a', scopes=[])
    foreign_tenant = PrincipalContext.user(subject_id='user:7', owner_user_id=7,
                                           tenant_id='other-tenant')
    for principal in (synthetic, foreign_tenant):
        if principal.kind == 'system':
            # A synthetic continuation cannot route any session, even before a
            # target is read: models never choose models.
            with pytest.raises(PermissionError):
                session_model.set_session_model(m.r.task, m.scope, principal, {
                    'conversation_id': m.conversation_id,
                    'attempt_id': m.row['execution']['attemptId'],
                    'model': 'model-b', 'reason': 'provider refusal', 'idempotency_key': 'denied'})
            continue
        # Another tenant's principal is refused by the target session's accepted
        # execution identity; without that evidence nothing is written either.
        with pytest.raises((PermissionError, ValueError)):
            session_model.set_session_model(m.r.task, m.scope, principal, {
                'conversation_id': m.conversation_id,
                'attempt_id': m.row['execution']['attemptId'],
                'model': 'model-b', 'reason': 'provider refusal', 'idempotency_key': 'denied'})
    with pytest.raises(PermissionError):
        _set(m, conversation_id='someone-elses-session')
    assert not m.admitted and not m.audited


def test_an_unroutable_choice_changes_nothing(routed, monkeypatch):
    from lib.project_dispatch import delegation
    m = routed

    def refuse(owner, model, **kwargs):
        raise ValueError(f'model {model!r} is not routable for this owner')
    monkeypatch.setattr(delegation, 'admit_worker_model', refuse)
    with pytest.raises(ValueError, match='not routable'):
        _set(m, model='unknown-model')
    assert m.r.c.repo.read(m.row['id'])['request']['model'] == 'model-a'
    assert not m.audited


def test_the_change_is_audited_and_replay_is_stable(routed):
    m = routed
    first = _set(m)
    second = _set(m)
    assert second['model'] == first['model'] == 'model-b'
    assert second['previous_model'] == 'model-b'  # a set-to-value replay writes nothing again
    assert second['previousModel'] == 'model-b'
    assert m.admitted == [(7, 'model-b')]  # the replay writes nothing again
    assert m.r.c.repo.read(m.row['id'])['grant'] == m.row['grant']
    events = [event for event, _ in m.audited]
    assert events == ['session_model_changed']
    fields = m.audited[0][1]
    assert fields['conversation_id'] == m.conversation_id
    assert fields['model'] == 'model-b' and fields['previous_model'] == ''
    assert fields['reason'] == 'Anthropic content-filter refusal'
