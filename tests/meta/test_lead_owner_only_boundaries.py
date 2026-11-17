"""What stays the owner's own while every lead acts for them (docs/ROUTER.md R9).

Owner decision 2026-11-18 handed the owner's authority to every lead, including
messaging the owner, files from every registered project and the owner's
answers as requests (lead audit #5). The lines that did not move are executable
here: login approvals (CIBA/SSO/QR), credentials, payments and external
publication need the owner. A change that makes one of these delegable needs a
new explicit owner decision recorded in docs/ROUTER.md, never an agent's judgment.
"""
import re

import pytest

pytestmark = pytest.mark.unit

# Owner-only actions, by the words (underscore-separated) their tools, scopes or settings would carry.
_OWNER_ONLY = re.compile(r'(?:^|_)(?:login|ciba|sso|qr|pair|pairing|credentials?|vault|secrets?|password|'
                         r'apikey|api_key|token|payments?|pay|billing|wallet|purchase|invoice|publish|'
                         r'publication|visibility|force_push)(?:_|$)')


def _lead_tools():
    from lib.router.policy import ROUTER_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    return set(COORDINATOR_TOOLS) | set(ROUTER_TOOLS)


def test_no_lead_ceiling_names_a_login_credential_payment_or_publication_tool():
    assert sorted(name for name in _lead_tools() if _OWNER_ONLY.search(name)) == []


def test_the_owners_delegation_covers_no_owner_only_scope():
    from lib.lifecycle_approval import DELEGATION_SCOPES
    assert [scope for scope in DELEGATION_SCOPES if _OWNER_ONLY.search(scope)] == []


def test_a_project_router_reaches_channels_only_through_channel_send_to_the_owner():
    """Its one channel tool; lib.channels.tools refuses every thread but the owner's command chat
    (tests/channels/test_lead_owner_chat.py) and every owner decision (approve, configure,
    subscribe) stays a genuine human Turn's."""
    from lib.channels.tools import CHANNEL_TOOL_NAMES, HUMAN_TURN_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    assert set(CHANNEL_TOOL_NAMES) & set(COORDINATOR_TOOLS) == {'channel_send'}
    assert {'channel_outbox_approve', 'channel_configure_thread', 'channel_subscribe',
            'channel_unsubscribe'} <= HUMAN_TURN_TOOLS


@pytest.mark.parametrize('path', ['oauth.claude', 'providers.openai.api_key', 'channels.dx.login',
                                  'channels.wechat.qr', 'feishu.app_secret', 'search.gateway.token'])
def test_secrets_logins_and_qr_pairing_stay_the_owners_in_settings(path):
    from lib.router.owner_settings import _classify
    assert _classify(path)['code'] == 'needs_owner_action'


@pytest.mark.parametrize('path', ['channels.outbox.approve', 'github.publish', 'channels.dx.send'])
def test_externally_visible_settings_are_never_a_delegated_change(path):
    from lib.router.owner_settings import _classify
    assert _classify(path)['code'] == 'owner_settings_not_delegated'


def test_no_lead_answers_the_owners_questions():
    """An owner answer counts as a request only because no agent can write one: control_conversation
    has no answer operation and no lead tool answers a card."""
    from lib.tools.coordinator import COORDINATOR_TOOLS as SCHEMAS
    control = next(tool['function'] for tool in SCHEMAS if tool['function']['name'] == 'control_conversation')
    assert 'answer_guidance' not in control['parameters']['properties']['action']['enum']
    assert not [name for name in _lead_tools() if 'answer' in name or 'guidance' in name]


def test_no_lead_attachment_carries_a_credential(monkeypatch):
    from lib.conversations import attachment_secrets
    monkeypatch.setattr(attachment_secrets, '_vault_values', lambda: [('hope_token', b'vault-value-77')])
    for name, content in (('run.log', b'with vault-value-77'), ('.netrc', b'machine x'),
                          ('k.txt', b'-----BEGIN PRIVATE KEY-----')):
        with pytest.raises(attachment_secrets.AttachmentSecretRefused):
            attachment_secrets.refuse_secrets(name, content)
