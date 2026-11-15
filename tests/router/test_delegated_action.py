"""The shared R9 gate for delegated scopes without a bespoke tool (ROUTER.md R9 "Extending the grant").

Owner decisions: 2026-11-06 (one revocable, audited grant: the Global Router acts, never speaks,
for the owner) and 2026-11-09 (「…就反正人能干的事情，总负责人都要能干」: the one switch covers every
delegated scope). ``owner_delegation.delegated_action`` checks the grant, resolves the owner trigger
and writes one audit record BEFORE the effect runs, then settles it. ``owner_settings`` and
``project_lifecycle`` both go through it, so the authority/audit mechanism exists once.
"""
from __future__ import annotations

import json

import pytest

from lib import lifecycle_approval as gate
from lib.identity import PrincipalContext
from lib.lifecycle_approval import DelegationRefused
from lib.router import owner_delegation
from lib.router.contract import validate
from tests.router.test_standing_instructions import OWNER, r as standing

__all__ = ('ROUTER', 'delegation', 'human', 'standing')

pytestmark = pytest.mark.unit

ROUTER = {'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None}
TARGET = {'kind': 'project', 'path': '/srv/projects/demo', 'action': 'create'}


def human(owner=OWNER):
    return {'kind': 'human-ui', 'ownerUserId': owner, 'subject': 'session-key', 'credential': 'session',
            'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    return tmp_path


@pytest.fixture
def delegation(standing, approvals, monkeypatch):
    """The owner's Global Router inside a genuine owner request, with grant/revoke/audit helpers."""
    s = standing
    s.turns['owner-turn']['projection'] = {'content': '把默认模型换成 claude-opus-5.5',
                                          'originalContent': '把默认模型换成 claude-opus-5.5'}
    owner = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)
    s.task.update(_turnId='router-turn', _attemptId='router-attempt', _attended=True,
                  _principalContext=owner.to_payload())
    monkeypatch.setattr('lib.router.owner_delegation._live_router_attempt', lambda task, scope: None)
    s.grant = lambda: gate.set_delegation(OWNER, True, grantee=dict(ROUTER), provenance=human())
    s.revoke = lambda: gate.set_delegation(OWNER, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': OWNER, 'credential': 'header'})
    s.audit = lambda: gate.delegated_actions(OWNER)
    return s


def _act(s, effect, *, target=TARGET, **arguments):
    return owner_delegation.delegated_action(
        s.task, s.scope, {'reason': 'owner asked', 'source_turn_id': 'owner-turn', **arguments},
        scope_name='project_lifecycle', target=target, action=effect)


def _never():
    raise AssertionError('the effect must not run')


# ── the grant covers every delegated scope, and its views stay valid ─────────

def test_the_grant_views_validate_with_every_delegated_scope(delegation):
    assert {'owner_settings', 'project_lifecycle'} <= set(gate.DELEGATION_SCOPES)
    validate(gate._revoked(OWNER), 'ownerDelegationGrant')
    validate(owner_delegation.owner_delegation_state(OWNER), 'ownerDelegationState')
    delegation.grant()
    validate(gate.delegation(OWNER), 'ownerDelegationGrant')
    validate(owner_delegation.owner_delegation_state(OWNER), 'ownerDelegationState')


def test_a_grant_from_before_a_scope_existed_covers_every_scope(delegation):
    """One switch, widened by definition (owner decision 2026-11-09), not per-scope consent."""
    delegation.grant()
    with open(gate._APPROVALS_FILE) as handle:
        document = json.load(handle)
    document['delegations'][str(OWNER)]['scopes'] = ['restart', 'conversation_control']
    with open(gate._APPROVALS_FILE, 'w') as handle:
        json.dump(document, handle)
    assert gate.delegation(OWNER)['scopes'] == list(gate.DELEGATION_SCOPES)
    result, _ = _act(delegation, lambda: {'created': True})
    assert result == {'created': True}


# ── refusals happen before any effect ───────────────────────────────────────

def test_without_the_grant_nothing_runs_and_nothing_is_recorded(delegation):
    with pytest.raises(DelegationRefused) as refused:
        _act(delegation, _never)
    assert refused.value.code == 'owner_delegation_not_granted'
    assert refused.value.execution_started is False
    assert delegation.audit() == []


def test_without_an_owner_trigger_the_refusal_is_recorded_and_nothing_runs(delegation):
    delegation.grant()
    with pytest.raises(DelegationRefused) as refused:
        owner_delegation.delegated_action(delegation.task, delegation.scope, {'reason': 'owner asked'},
                                          scope_name='project_lifecycle', target=TARGET, action=_never)
    assert refused.value.code == 'owner_delegation_trigger_missing'
    [record] = delegation.audit()
    assert (record['status'], record['code'], record['scope']) == (
        'refused', 'owner_delegation_trigger_missing', 'project_lifecycle')
    assert record['target'] == TARGET


def test_a_reason_is_required(delegation):
    delegation.grant()
    with pytest.raises(ValueError):
        owner_delegation.delegated_action(delegation.task, delegation.scope, {'source_turn_id': 'owner-turn'},
                                          scope_name='project_lifecycle', target=TARGET, action=_never)


def test_revoking_the_grant_stops_the_next_action(delegation):
    delegation.grant()
    _act(delegation, lambda: {})
    delegation.revoke()
    with pytest.raises(DelegationRefused) as refused:
        _act(delegation, _never)
    assert refused.value.code == 'owner_delegation_not_granted'


def test_only_the_global_router_can_act(delegation):
    delegation.grant()
    project_scope = {**delegation.scope, 'project_path': '/srv/projects/not-the-router-home'}
    with pytest.raises(PermissionError):
        owner_delegation.delegated_action(
            delegation.task, project_scope, {'reason': 'owner asked', 'source_turn_id': 'owner-turn'},
            scope_name='project_lifecycle', target=TARGET, action=_never)


# ── the audit record precedes the effect and settles with its outcome ───────

def test_an_effect_runs_once_after_its_audit_record_and_settles_completed(delegation):
    delegation.grant()
    seen = []

    def effect():
        seen.append([record['status'] for record in delegation.audit()])
        return {'created': True}

    result, action_id = _act(delegation, effect)
    assert result == {'created': True}
    assert seen == [['accepted']]
    [record] = delegation.audit()
    assert record['id'] == action_id and record['status'] == 'completed'
    assert record['requester'] == {**ROUTER, 'turnId': 'router-turn', 'attemptId': 'router-attempt'}
    assert record['trigger'] == {'kind': 'owner_message', 'turnId': 'owner-turn',
                                 'text': '把默认模型换成 claude-opus-5.5'}
    assert record['target'] == TARGET and record['reason'] == 'owner asked'


def test_a_refused_effect_settles_failed_with_its_code(delegation):
    delegation.grant()

    def effect():
        raise DelegationRefused('project_path_taken', 'That folder already belongs to a project')

    with pytest.raises(DelegationRefused):
        _act(delegation, effect)
    [record] = delegation.audit()
    assert (record['status'], record['code']) == ('failed', 'project_path_taken')


def test_an_unexpected_effect_error_settles_failed_with_its_type(delegation):
    delegation.grant()

    def effect():
        raise OSError('disk full')

    with pytest.raises(OSError):
        _act(delegation, effect)
    [record] = delegation.audit()
    assert (record['status'], record['code'], record['detail']) == ('failed', 'OSError', 'disk full')


def test_the_same_idempotency_key_never_applies_twice(delegation):
    delegation.grant()
    runs = []
    first, first_id = _act(delegation, lambda: runs.append(1) or {'created': True}, idempotency_key='k1')
    again, again_id = _act(delegation, lambda: runs.append(2) or {'created': True}, idempotency_key='k1')
    assert runs == [1] and first_id == again_id
    assert first == {'created': True}
    assert again['replayed'] is True and again['status'] == 'completed'
    assert again['interrupted'] is False and again['execution_started'] is False


@pytest.mark.parametrize('target', [
    {'kind': 'owner_settings', 'path': 'search.skip_domains'},
    {'kind': 'owner_settings', 'channel': 'dx', 'action': 'disable'},
    {'kind': 'project', 'path': '/srv/projects/demo', 'folderId': 'folder-1', 'action': 'configure'},
])
def test_every_new_target_shape_is_a_valid_audit_record(delegation, target):
    delegation.grant()
    _act(delegation, lambda: {}, target=target)
    [record] = delegation.audit()
    validate(record, 'ownerDelegatedAction')
    validate(owner_delegation.owner_delegation_state(OWNER), 'ownerDelegationState')
