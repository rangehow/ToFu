"""The Global Router changes reversible owner settings through the Settings page's own Save (R11).

Owner decisions 2026-11-09: 「…就反正人能干的事情，总负责人都要能干」
(coordinator-1a390889aa954da2b18551d1b4a32625, Turn 3315ee59-e227-4025-8246-2aee9a9a543e) and
"…if you need to adjust something in the settings, you can also have that permission yourself."

The Settings Save/read is the backend port the composition root registers (its parity with the
Settings page is test_owner_settings_backend.py); here a recording fake stands in for it. Every
change goes through the R9 gate (test_delegated_action.py): one grant, an owner trigger, one
audit record, revocable. Human-only (class d) requests return needs_owner_action and destructive
or external (b/c) requests are refused, both without side effects. A channel toggle reports its
preconditions first and never requests a 大象 login approval by itself.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from tests.support.dx_overlay import requires_dx

from lib.lifecycle_approval import DelegationRefused
from lib.router import owner_settings
from tests.router.test_delegated_action import ROUTER, approvals, delegation, standing
from tests.router.test_standing_instructions import OWNER

__all__ = ('approvals', 'delegation', 'standing')

pytestmark = pytest.mark.unit

OWNER_TRIGGER = {'reason': 'owner asked', 'source_turn_id': 'owner-turn'}
CURRENT = {
    'model_defaults': {'default_model': 'claude-opus-5', 'fallback_model': 'gpt-5.6-terra'},
    'hidden_models': ['old-model'],
    'hidden_ig_models': [],
    'search': {'skip_domains': ['spam.example'], 'gateway': {'enabled': True, 'has_key': True},
               'gateway_catalog': []},
    'network': {'proxy_bypass_domains': ['corp.example']},
    'mt_provider': {'provider': 'niutrans', 'api_key': 'MT-SECRET-VALUE', 'enabled': True},
    'feishu': {'enabled': True, 'app_id_masked': '***1234', 'has_secret': True},
}


class FakeSettings:
    """The registered Settings Save/read port: records each Save body and serves the next read."""

    def __init__(self):
        self.current = deepcopy(CURRENT)
        self.saved = []
        self.answer = None

    def read(self):
        return deepcopy(self.current)

    def save(self, body):
        self.saved.append(deepcopy(body))
        if self.answer is not None:
            return dict(self.answer)
        for key, value in body.items():
            if key == 'search':
                self.current['search']['skip_domains'] = sorted(set(value['skip_domains']))
            elif key == 'proxy_bypass_domains':
                self.current['network']['proxy_bypass_domains'] = value
            else:
                self.current[key] = value
        return {'ok': True, 'status': 200, 'changes': sorted(body), 'needs_restart': False}


@pytest.fixture
def s(delegation, monkeypatch):
    delegation.settings = FakeSettings()
    monkeypatch.setattr(owner_settings, '_SETTINGS_BACKEND', delegation.settings)
    return delegation


def _set(s, path, value, **trigger):
    return owner_settings.call(s.task, s.scope, 'set',
                               {'path': path, 'value': value, **(trigger or OWNER_TRIGGER)})


# ── authority: grant, trigger, revocation, audit ─────────────────────────────

def test_without_the_grant_a_change_is_refused_and_nothing_is_saved(s):
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'model_defaults.default_model', 'claude-opus-5.5')
    assert refused.value.code == 'owner_delegation_not_granted'
    assert refused.value.execution_started is False
    assert s.settings.saved == [] and s.audit() == []


def test_a_change_needs_an_owner_trigger_and_the_refusal_is_audited(s):
    s.grant()
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'model_defaults.default_model', 'claude-opus-5.5', reason='owner asked')
    assert refused.value.code == 'owner_delegation_trigger_missing'
    assert s.settings.saved == []
    [record] = s.audit()
    assert (record['status'], record['scope']) == ('refused', 'owner_settings')


def test_an_unrelated_synthetic_turn_is_still_refused(s):
    """REFUSALS: under the grant too, a channel event or relayed notice is not the owner."""
    s.grant()
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'hidden_models', [], reason='owner asked', source_turn_id='event-turn')
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    s.source = PermissionError('the current input is a channel event, not the owner')
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'hidden_models', [])
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    assert s.settings.saved == []
    assert [record['status'] for record in s.audit()] == ['refused', 'refused']


def test_revoking_the_grant_stops_further_changes(s):
    s.grant()
    _set(s, 'hidden_models', ['old-model', 'noisy-model'])
    s.revoke()
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'hidden_models', [])
    assert refused.value.code == 'owner_delegation_not_granted'
    assert len(s.settings.saved) == 1


# ── the Settings Save body, its validation and the read-back ─────────────────

def test_a_change_is_the_settings_save_body_read_back_and_audited(s):
    s.grant()
    result = _set(s, 'model_defaults.default_model', 'claude-opus-5.5')
    # The Save replaces model_defaults whole, so the fallback is carried from the Settings read.
    assert s.settings.saved == [{'model_defaults': {'default_model': 'claude-opus-5.5',
                                                    'fallback_model': 'gpt-5.6-terra'}}]
    assert result['value'] == 'claude-opus-5.5' and result['execution_started'] is True
    [record] = s.audit()
    assert record['status'] == 'completed' and record['id'] == result['action_id']
    assert record['target'] == {'kind': 'owner_settings', 'path': 'model_defaults.default_model'}
    assert record['trigger'] == {'kind': 'owner_message', 'turnId': 'owner-turn',
                                 'text': '把默认模型换成 claude-opus-5.5'}
    assert record['requester'] == {**ROUTER, 'turnId': 'router-turn', 'attemptId': 'router-attempt'}


def test_a_skip_domain_change_never_resends_the_masked_gateway(s):
    s.grant()
    result = _set(s, 'search.skip_domains', ['b.example', 'a.example'])
    assert s.settings.saved == [{'search': {'skip_domains': ['b.example', 'a.example']}}]
    assert result['value'] == ['a.example', 'b.example']


def test_a_bypass_change_reads_back_from_the_network_section(s):
    s.grant()
    assert _set(s, 'proxy_bypass_domains', ['corp.example', 'lab.example'])['value'] == [
        'corp.example', 'lab.example']


def test_a_rejected_value_carries_the_settings_error_verbatim_and_settles_failed(s):
    s.grant()
    s.settings.answer = {'ok': False, 'status': 400,
                         'error': {'kind': 'bad_request',
                                   'message': 'proxy_bypass_domains: proxy_bypass_domains must be an array'}}
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'proxy_bypass_domains', 'corp.example')
    assert refused.value.code == 'owner_settings_rejected'
    assert 'proxy_bypass_domains: proxy_bypass_domains must be an array' in str(refused.value)
    [record] = s.audit()
    assert (record['status'], record['code']) == ('failed', 'owner_settings_rejected')


def test_a_save_that_changes_nothing_is_not_reported_as_applied(s):
    s.grant()
    s.settings.answer = {'ok': True, 'status': 200, 'changes': []}
    with pytest.raises(DelegationRefused) as refused:
        _set(s, 'hidden_models', ['noisy-model'])
    assert refused.value.code == 'owner_settings_not_applied'
    assert s.audit()[0]['status'] == 'failed'


@pytest.mark.parametrize(('path', 'value'), [
    ('search.skip_domains', 'spam.example'),  # the Save would persist it and lose the ban list
    ('hidden_models', 'noisy-model'),         # the Save would ignore it silently
    ('model_defaults.default_model', ''),
    ('model_defaults.fallback_model', None),
])
def test_a_value_in_a_shape_the_settings_page_never_saves_is_refused_before_any_save(s, path, value):
    s.grant()
    with pytest.raises(ValueError):
        _set(s, path, value)
    assert s.settings.saved == [] and s.audit() == []


# ── reads expose only writable settings, never a secret ─────────────────────

def test_list_and_get_expose_only_writable_settings(s):
    s.grant()
    listed = owner_settings.call(s.task, s.scope, 'list', {})
    assert sorted(listed['settings']) == sorted(owner_settings.WRITABLE_PATHS)
    assert listed['settings']['proxy_bypass_domains'] == ['corp.example']
    assert 'MT-SECRET-VALUE' not in json.dumps(listed)
    got = owner_settings.call(s.task, s.scope, 'get', {'path': 'model_defaults.fallback_model'})
    assert (got['value'], got['found']) == ('gpt-5.6-terra', True)
    assert s.audit() == []


@pytest.mark.parametrize('path', ['mt_provider', 'mt_provider.api_key', 'search.gateway.api_key', 'feishu'])
def test_a_secret_bearing_setting_never_reads_back(s, path):
    s.grant()
    result = owner_settings.call(s.task, s.scope, 'get', {'path': path})
    assert result['code'] == 'needs_owner_action'
    assert 'MT-SECRET-VALUE' not in json.dumps(result)


def test_reads_need_the_grant(s):
    with pytest.raises(DelegationRefused) as refused:
        owner_settings.call(s.task, s.scope, 'list', {})
    assert refused.value.code == 'owner_delegation_not_granted'


def test_without_a_settings_backend_nothing_is_read_or_changed(s, monkeypatch):
    s.grant()
    monkeypatch.setattr(owner_settings, '_SETTINGS_BACKEND', None)
    with pytest.raises(DelegationRefused) as refused:
        owner_settings.call(s.task, s.scope, 'get', {'path': 'hidden_models'})
    assert refused.value.code == 'owner_settings_unavailable'


# ── classes (b), (c), (d) are never performed ────────────────────────────────

@pytest.mark.parametrize('path', ['mt_provider.enabled', 'feishu.enabled', 'search.gateway.api_key',
                                  'proxy_pool', 'oauth.session', 'channels.dx.qr',
                                  'credentials_vault.github_token'])
def test_a_human_only_request_returns_needs_owner_action_without_side_effects(s, path):
    s.grant()
    result = _set(s, path, 'NEW-SECRET-VALUE')
    assert result['code'] == 'needs_owner_action' and result['execution_started'] is False
    assert 'Settings' in result['remedy'] and 'NEW-SECRET-VALUE' not in json.dumps(result)
    assert s.settings.saved == [] and s.audit() == []


@pytest.mark.parametrize('path', ['channels.dx.send', 'channels.outbox.approve', 'server.restart',
                                  'data.delete_all', 'conversations.export'])
def test_a_destructive_or_external_request_is_refused_without_an_explicit_instruction(s, path):
    s.grant()
    result = _set(s, path, True)
    assert result['code'] == 'owner_settings_not_delegated' and result['execution_started'] is False
    assert s.settings.saved == [] and s.audit() == []


def test_an_unsupported_setting_names_what_the_router_can_change(s):
    s.grant()
    result = _set(s, 'cost_experiment', {'lifecycle': 'running'})
    assert result['code'] == 'owner_settings_unsupported'
    assert result['writable'] == sorted(owner_settings.WRITABLE_PATHS)
    assert s.settings.saved == []


# ── channel toggles: preconditions first, never a login request of its own ──

def _preconditions(**overrides):
    base = {'channel': 'dx', 'enabled': False, 'status': {'state': 'disabled', 'detail': ''},
            'running_session': False, 'sso_login_state': 'usable', 'relaunch_would_push_login_card': False}
    return {**base, **overrides}


@pytest.fixture
def channel(s, monkeypatch):
    s.updates = []
    s.preconditions = _preconditions()
    monkeypatch.setattr(owner_settings, 'channel_preconditions', lambda owner, name: dict(s.preconditions))
    monkeypatch.setattr(owner_settings, '_update_channel',
                        lambda owner, name, update: s.updates.append((name, update)) or {'enabled': True})
    s.grant()
    return s


def _toggle(s, enabled, **trigger):
    return owner_settings.call(s.task, s.scope, 'channel',
                               {'channel': 'dx', 'enabled': enabled, **(trigger or OWNER_TRIGGER)})


def test_channel_status_reports_preconditions_without_a_change_or_audit(channel):
    result = owner_settings.call(channel.task, channel.scope, 'channel', {'channel': 'dx'})
    assert result['preconditions']['sso_login_state'] == 'usable'
    assert result['execution_started'] is False
    assert channel.updates == [] and channel.audit() == []


def test_channel_status_needs_the_grant(channel):
    channel.revoke()
    with pytest.raises(DelegationRefused) as refused:
        owner_settings.call(channel.task, channel.scope, 'channel', {'channel': 'dx'})
    assert refused.value.code == 'owner_delegation_not_granted'


def test_enabling_never_requests_a_login_approval_by_itself(channel):
    channel.preconditions = _preconditions(sso_login_state='approval_required',
                                           relaunch_would_push_login_card=True)
    with pytest.raises(DelegationRefused) as refused:
        _toggle(channel, True)
    assert refused.value.code == 'needs_owner_action'
    assert 'login' in str(refused.value).lower() and 'no login was requested' in str(refused.value)
    assert channel.updates == [] and channel.audit() == []


def test_enabling_without_a_login_account_needs_the_owner(channel):
    channel.preconditions = _preconditions(sso_login_state='no_account')
    with pytest.raises(DelegationRefused) as refused:
        _toggle(channel, True)
    assert refused.value.code == 'needs_owner_action'
    assert channel.updates == []


def test_a_kicked_account_reconnects_only_on_the_owners_own_message(channel):
    channel.preconditions = _preconditions(status={'state': 'kicked', 'detail': 'another login took it'})
    with pytest.raises(DelegationRefused) as refused:
        _toggle(channel, True, reason='standing instruction', instruction_id='instr-1', event_id='evt-1')
    assert refused.value.code == 'needs_owner_action'
    assert channel.updates == []
    result = _toggle(channel, True)
    assert channel.updates == [('dx', {'enabled': True})]
    assert result['execution_started'] is True


def test_a_synthetic_turn_cannot_toggle_a_channel(channel):
    channel.preconditions = _preconditions(enabled=True, running_session=True,
                                           status={'state': 'connected', 'detail': ''})
    channel.source = PermissionError('the current input is a channel event, not the owner')
    with pytest.raises(DelegationRefused) as refused:
        _toggle(channel, False)
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    assert channel.updates == []


def test_an_enabled_live_session_is_left_alone(channel):
    channel.preconditions = _preconditions(enabled=True, running_session=True,
                                           status={'state': 'connected', 'detail': ''})
    result = _toggle(channel, True)
    assert result['execution_started'] is False and 'connector_owned' in result['detail']
    assert channel.updates == [] and channel.audit() == []


def test_disabling_is_the_settings_channel_update_and_is_audited(channel):
    channel.preconditions = _preconditions(enabled=True, running_session=True,
                                           status={'state': 'connected', 'detail': ''})
    result = _toggle(channel, False)
    assert channel.updates == [('dx', {'enabled': False})]
    assert result['preconditions']['running_session'] is True and result['execution_started'] is True
    [record] = channel.audit()
    assert record['status'] == 'completed'
    assert record['target'] == {'kind': 'owner_settings', 'channel': 'dx', 'action': 'disable'}


def _login_session(mis):
    from tofu_meituan.dx.login import LoginSession
    return LoginSession(mis, {})


@requires_dx
def test_dx_preconditions_ask_the_supervisors_own_login_question(s, monkeypatch):
    asked = []
    monkeypatch.setattr(owner_settings, '_channel_view', lambda owner, name: {
        'enabled': False, 'loginMis': 'owner01', 'status': {'state': 'needs_login', 'detail': 'expired'}})
    monkeypatch.setattr('tofu_meituan.dx.adapter.launcher_session',
                        lambda owner, mis: asked.append((owner, mis)) or _login_session('owner01'))
    monkeypatch.setattr('tofu_meituan.dx.login.approval_required', lambda owner, session: True)
    monkeypatch.setattr('tofu_meituan.dx.supervisor.running_adapter', lambda owner: None)
    result = owner_settings.channel_preconditions(OWNER, 'dx')
    assert asked == [(OWNER, 'owner01')]
    assert result['sso_login_state'] == 'approval_required'
    assert result['relaunch_would_push_login_card'] is True and result['running_session'] is False
    assert result['status'] == {'state': 'needs_login', 'detail': 'expired'}


# ── reachability and the tool-name budget ────────────────────────────────────

def test_owner_settings_is_a_router_tool_outside_the_direct_core():
    from lib.router.policy import ROUTER_DIRECT_TOOLS, ROUTER_ONLY_TOOLS, ROUTER_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    from lib.tools.coordinator import ROUTER_TOOL_SCHEMAS
    assert 'owner_settings' in ROUTER_ONLY_TOOLS and 'owner_settings' in ROUTER_TOOLS
    assert 'owner_settings' not in ROUTER_DIRECT_TOOLS and 'owner_settings' not in COORDINATOR_TOOLS
    assert 'owner_settings' in {tool['function']['name'] for tool in ROUTER_TOOL_SCHEMAS}


def test_a_worker_never_receives_owner_settings():
    from lib.project_dispatch.repository import CONTRACT
    assert 'owner_settings' in CONTRACT['workerDeniedTools']


def test_owner_settings_is_dispatched_by_exact_name(s, monkeypatch):
    from lib.router import tools as router_tools
    seen = []
    monkeypatch.setattr(owner_settings, 'call',
                        lambda task, scope, action, arguments: seen.append((action, arguments)) or {})
    router_tools.execute_router_tool(s.task, s.scope, 'owner_settings',
                                     {'action': 'get', 'path': 'model_defaults.default_model'})
    assert seen == [('get', {'path': 'model_defaults.default_model'})]


def test_owner_settings_arguments_validate_against_the_contract():
    from lib.router.contract import validate
    validate({'action': 'set', 'path': 'hidden_models', 'value': [], **OWNER_TRIGGER}, 'ownerSettings')
    validate({'action': 'channel', 'channel': 'wechat'}, 'ownerSettings')
    for invalid in ({'action': 'restart', 'reason': 'x'}, {'action': 'channel', 'channel': 'feishu'}):
        with pytest.raises(ValueError):
            validate(invalid, 'ownerSettings')
