"""Leads reach the owner directly (docs/ROUTER.md R9; lead audit 2026-11-18 #5, A5).

A Project Router sends to the owner's own command chat and to no one else, every
call audited; leads attach files of registered projects and of their own
Workers' checkouts; no attachment that carries a credential is ever queued.
"""
import pytest

from lib.conversations import attachment_secrets
from lib.channels import tools
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
GROUP = 'dx:gid:500'
LEAD = 'coordinator-tofu'
PROJECT = '/work/tofu'


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox, monkeypatch, turns):
    enabled(command_chat=COMMAND)
    monkeypatch.setattr('lib.channels.service._require_owned_conversation', lambda owner, conv: None)
    monkeypatch.setattr(attachment_secrets, '_vault_values', lambda: [])
    return store


@pytest.fixture
def lead(monkeypatch):
    """The calling conversation is the bound Project Router of PROJECT."""
    scope = {'user_id': OWNER, 'project_path': PROJECT, 'conversation_id': LEAD, 'binding_revision': 1}
    monkeypatch.setattr('lib.conversations.lead_attachments.lead_scope',
                        lambda task: dict(scope) if task.get('convId') == LEAD else None)
    monkeypatch.setattr(tools, '_is_project_router',
                        lambda owner, conversation, path: conversation == LEAD and path == PROJECT)
    return scope


@pytest.fixture
def audits(monkeypatch):
    records = []
    monkeypatch.setattr('lib.log.audit_log', lambda event, **fields: records.append((event, fields)))
    return records


def _task(conversation=LEAD, **extra):
    from lib.identity import PrincipalContext
    principal = PrincipalContext.user(subject_id=f'user:{OWNER}', owner_user_id=OWNER)
    return {'id': 'task-lead', 'convId': conversation, '_userId': OWNER, '_turnId': 'out-1',
            'config': {'projectPath': PROJECT}, '_principalContext': principal.to_payload(),
            '_attended': True, **extra}


def test_a_project_router_sees_channel_send_only_where_the_owner_has_a_command_chat(channel, lead):
    assert [t['function']['name'] for t in tools.visible_tools(OWNER, LEAD, PROJECT)] == ['channel_send']
    assert tools.visible_tools(OWNER, LEAD, '/work/other') == []
    assert tools.visible_tools(OWNER, 'worker-a', PROJECT) == []


def test_a_project_router_sends_to_the_owners_command_chat_and_it_is_audited(channel, lead, audits):
    result = tools.execute('channel_send', {'thread': COMMAND, 'text': 'Paper A is ready',
                                            'idempotency_key': 'paper-a'}, _task())
    assert result['item']['state'] == 'queued' and result['item']['origin']['conversationId'] == LEAD
    [(event, fields)] = audits
    assert event == 'lead_channel_send' and fields['outcome'] == 'accepted'
    assert fields['conversation_id'] == LEAD and fields['thread'] == COMMAND
    assert fields['item_id'] == result['item']['id']


@pytest.mark.parametrize('thread', [GROUP, 'dx:uid:2002'])
def test_a_project_router_never_messages_anyone_but_the_owner(channel, lead, audits, thread):
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': thread, 'text': 'hello team', 'idempotency_key': 'k'}, _task())
    assert refused.value.code == 'owner_chat_only'
    assert channel.active_outbox(ThreadKey.parse(thread)) == []
    [(event, fields)] = audits
    assert event == 'lead_channel_send' and fields['outcome'] == 'refused' and fields['code'] == 'owner_chat_only'


def test_a_project_router_has_no_other_channel_tool(channel, lead):
    for name, args in (('channel_threads', {}), ('channel_outbox_approve', {'item_id': 'x', 'decision': 'approve'}),
                       ('channel_subscribe', {'thread': GROUP, 'conversation_id': LEAD})):
        with pytest.raises(ChannelError) as refused:
            tools.execute(name, args, _task())
        assert refused.value.code == 'forbidden'


def test_a_non_lead_conversation_still_reports_to_its_lead(channel, lead):
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': COMMAND, 'text': 'x', 'idempotency_key': 'k'}, _task('worker-a'))
    assert refused.value.code == 'forbidden' and 'your lead' in str(refused.value)


def _dispatch_task(coordinator_scope, conversation):
    """A task running under a project-dispatch grant: a Coordinator's continuation Turn (its grant
    names the lead's binding) or a Worker's assignment (``coordinatorScope`` None)."""
    from lib.identity import PrincipalContext
    from lib.project_dispatch.admission import KEY, ROLE_KEY
    binding = {'assignmentId': 'assignment-1', 'owner': OWNER}
    principal = PrincipalContext.system(subject_id='project-dispatch:assignment-1', owner_user_id=OWNER,
                                        scopes={'chat'})
    return {'id': 'task-dispatch', 'convId': conversation, '_userId': OWNER, '_turnId': 'out-1',
            'config': {'projectPath': PROJECT, KEY: binding}, '_principalContext': principal.to_payload(),
            ROLE_KEY: {'binding': binding, 'coordinatorScope': coordinator_scope, 'mode': 'coordinator'}}


@pytest.fixture
def no_watchdog(monkeypatch):
    monkeypatch.setattr('lib.router.watchdog.binding.is_watchdog_location', lambda owner, path: False)


def test_a_coordinator_continuation_turn_is_still_the_lead(channel, no_watchdog, audits):
    """Live 2026-11-18 10:31: a woken Coordinator (continuation Turn under its own dispatch grant)
    was refused 'Only a project lead sends to channels'; the grant names the lead's binding."""
    scope = {'user_id': OWNER, 'project_path': PROJECT, 'conversation_id': LEAD, 'binding_revision': 1}
    result = tools.execute('channel_send', {'thread': COMMAND, 'text': 'Tap the Hope card now',
                                            'idempotency_key': 'hope'}, _dispatch_task(scope, LEAD))
    assert result['item']['state'] == 'queued' and result['item']['origin']['conversationId'] == LEAD
    [(event, fields)] = audits
    assert event == 'lead_channel_send' and fields['outcome'] == 'accepted'
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': GROUP, 'text': 'hello team', 'idempotency_key': 'g'},
                      _dispatch_task(scope, LEAD))
    assert refused.value.code == 'owner_chat_only'


def test_a_worker_under_a_dispatch_grant_is_refused(channel, no_watchdog, audits):
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': COMMAND, 'text': 'x', 'idempotency_key': 'w'},
                      _dispatch_task(None, 'worker-a'))
    assert refused.value.code == 'forbidden' and 'your lead' in str(refused.value)
    assert channel.active_outbox(ThreadKey.parse(COMMAND)) == []
    [(event, fields)] = audits
    assert fields['outcome'] == 'refused' and fields['code'] == 'forbidden'


def test_a_lead_attaches_a_file_of_its_own_workers_checkout(channel, lead, monkeypatch, tmp_path):
    checkout = tmp_path / 'checkout'
    (checkout / 'out').mkdir(parents=True)
    (checkout / 'out' / 'report.pdf').write_bytes(b'%PDF-1.4 the report')

    def worker_checkout(task, assignment_id):
        from lib.conversations.lead_attachments import AttachmentRefused
        if assignment_id != 'assignment-own':
            raise AttachmentRefused(f'Assignment {assignment_id} belongs to another lead')
        return str(checkout)
    monkeypatch.setattr('lib.conversations.lead_attachments.lead_worker_checkout', worker_checkout)
    result = tools.execute('channel_send', {
        'thread': COMMAND, 'text': 'The report', 'idempotency_key': 'report',
        'attachments': [{'path': 'out/report.pdf', 'worker': 'assignment-own'}]}, _task())
    assert [record['name'] for record in result['item']['attachments']] == ['report.pdf']
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {
            'thread': COMMAND, 'text': 'Theirs', 'idempotency_key': 'theirs',
            'attachments': [{'path': 'out/report.pdf', 'worker': 'assignment-other'}]}, _task())
    assert refused.value.code == 'attachment_worker_unknown'


@pytest.mark.parametrize('name, content', [
    ('notes.txt', b'deploy with\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n'),
    ('notes.txt', b'token ghp_' + b'a' * 36 + b' for CI\n'),
    ('creds.zip', b'PK\x03\x04 AKIA' + b'ABCDEFGHIJKLMNOP'),
    ('handoff.txt', b'curl -H "Authorization: Bearer abcdefghijklmnop0123456789"\n'),
])
def test_an_attachment_carrying_a_credential_is_refused_before_anything_is_queued(
        channel, lead, monkeypatch, tmp_path, name, content):
    (tmp_path / name).write_bytes(content)
    monkeypatch.setattr('lib.router.registered_projects.registered_project',
                        lambda owner, requested: str(tmp_path) if requested == str(tmp_path) else None)
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': COMMAND, 'text': 'see file', 'idempotency_key': 'k-' + name,
                                       'attachments': [{'path': name, 'project': str(tmp_path)}]}, _task())
    assert refused.value.code == 'attachment_secret'
    assert content.decode('latin-1') not in str(refused.value)  # the finding names the rule, never the value
    assert channel.active_outbox(ThreadKey.parse(COMMAND)) == []


def test_a_vault_value_anywhere_in_a_file_is_refused(channel, lead, monkeypatch, tmp_path):
    monkeypatch.setattr(attachment_secrets, '_vault_values', lambda: [('hope_token', b's3cr3t-value-123')])
    (tmp_path / 'log.txt').write_bytes(b'calling with s3cr3t-value-123 now\n')
    monkeypatch.setattr('lib.router.registered_projects.registered_project', lambda owner, requested: str(tmp_path))
    with pytest.raises(ChannelError) as refused:
        tools.execute('channel_send', {'thread': COMMAND, 'text': 'log', 'idempotency_key': 'log',
                                       'attachments': [{'path': 'log.txt', 'project': str(tmp_path)}]}, _task())
    assert 'vault entry hope_token' in str(refused.value) and 's3cr3t' not in str(refused.value)


def test_ordinary_source_and_documents_pass_the_scan(monkeypatch):
    """Generic key = value shapes are code, not credentials: only exact proofs refuse."""
    monkeypatch.setattr(attachment_secrets, '_vault_values', lambda: [])
    for name, content in (('app.py', b'token = os.environ["TOKEN"]\npassword = ask()\n'),
                          ('report.pdf', b'%PDF-1.4 Authorization is discussed in section 2'),
                          ('README.md', b'Set GITHUB_TOKEN in the vault, never here.')):
        assert attachment_secrets.secret_findings(name, content) == [], name
