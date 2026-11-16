"""Project leads message each other across projects (docs/ROUTER.md R13).

Owner request 2026-11-11: the owner's project leads (the Global Router and
every Project Router) talk to each other across projects, so the owner need
not repeat himself, and every lead can read the owner's shared resource
registry. Workers stay inside their project; another owner is never a sender
or recipient; every cross-project original is labelled nonhuman context and
records its sender's project. Real storage authorities; no model runs.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit
AUTHORITIES = pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)


@pytest.fixture
def world(semantic_authority, monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.binding import global_router
    from lib.router.watchdog.binding import router_watchdog
    client, owner, engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    router = global_router(owner, create=True)
    watchdog = router_watchdog(owner, create=True)
    leads = {}
    for name in ('alpha', 'beta'):
        (tmp_path / name).mkdir()
        leads[name] = ProjectCoordinationRepository(owner).create(
            str(tmp_path / name), folder_id='folder', expected_revision=0, command_id=name)
    client(write=True).command('conversation.create', {
        'conv_id': 'worker-alpha', 'user_id': owner, 'title': 'worker-alpha',
        'settings': {'projectPath': leads['alpha']['projectPath'],
                     '_coordinatorSource': leads['alpha']['conversationId']},
    }, 'create-worker-alpha')
    return SimpleNamespace(client=client, owner=owner, router=router, watchdog=watchdog,
                           alpha=leads['alpha'], beta=leads['beta'], worker='worker-alpha',
                           tmp=tmp_path, native=engine is not None)


def _task(world, conversation_id, project):
    return {'id': 'task-' + conversation_id, 'convId': conversation_id, '_userId': world.owner,
            'config': {'projectPath': project}}


def _lead(world, binding):
    return _task(world, binding['conversationId'], binding['projectPath'])


def _send(sender, target, *, key='ask-1', content='Do you have spare H800 hours this week?', **extra):
    from lib.conversations.project_messages import execute
    return execute('send_project_message', {'conversation_id': target, 'content': content,
                                            'idempotency_key': key, **extra}, sender)


def _deliver(task):
    """Inject pending originals, pass every input admission and confirm consumption."""
    from lib.conversations.project_messages import confirm, inject
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.message_input_receipts import receipt_admission, stream_with_receipts
    messages = [{'role': 'user', 'content': 'Continue the authorized request'}]
    inject(task, messages, 1)
    if len(messages) == 1:
        return None

    def stream(current, body):
        lease = receipt_admission(current, None)(max_output_tokens=32)
        lease.prepare_input(body)
        return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': 'Read'}, 'stop', {})

    assert stream_with_receipts(stream, task, {'messages': messages}).is_verified_complete
    confirm(task, 1)
    return messages[-1]


def _mailbox(world, binding):
    from lib.conversations.project_messages import ProjectMailbox
    return ProjectMailbox(world.owner, binding['projectPath'], binding['conversationId'])


@AUTHORITIES
def test_leads_message_across_projects_and_the_recipient_receives_and_considers(world):
    from lib.conversations.project_messages import CROSS_PROJECT, execute, page_view
    alpha, beta = _lead(world, world.alpha), _lead(world, world.beta)
    sent = _send(alpha, world.beta['conversationId'], kind='question')
    message = sent['message']
    assert sent['execution_started'] is False and sent['wake'] == 'next_authorized_turn'
    assert message['sender'] == world.alpha['conversationId'] and message['kind'] == 'question'
    assert message['senderProject'] == world.alpha['projectPath']
    assert _send(alpha, world.beta['conversationId'], kind='question')['message'] == message
    assert [row['id'] for row in _mailbox(world, world.beta).read()] == [message['id']]

    carrier = _deliver(beta)
    assert carrier['_projectMessageIds'] == [message['id']] and carrier['_containsHumanSteer'] is False
    assert CROSS_PROJECT['label'] in carrier['content']
    assert f'sender-project="{world.alpha["projectPath"]}"' in carrier['content']
    assert 'sender-role="project_router"' in carrier['content']
    [received] = execute('get_project_messages', {}, beta)['messages']
    assert received['state'] == 'input_confirmed' and received['decision'] is None
    assert received['senderProject'] == world.alpha['projectPath']

    considered = execute('consider_project_message', {
        'message_id': message['id'], 'decision': 'accepted', 'expected_revision': received['revision'],
        'note': 'Will answer with the measured free hours; no new work authority.'}, beta)['message']
    assert considered['decision'] == 'accepted'
    [outgoing] = execute('get_project_messages', {'direction': 'outgoing',
                                                  'conversation_id': world.beta['conversationId']},
                         alpha)['messages']
    assert outgoing['id'] == message['id'] and outgoing['decision'] == 'accepted'
    # The owner's audit view (GET /api/v4/project/coordination/messages) names the sender's project.
    [audited] = page_view(world.owner, world.beta['projectPath'], world.beta['conversationId'])['messages']
    assert audited['senderProject'] == world.alpha['projectPath'] and audited['decision'] == 'accepted'

    reply = _send(beta, world.alpha['conversationId'], key='answer-1', kind='report',
                  content='64 H800 hours free until Friday.', reply_to=message['id'])['message']
    assert reply['replyTo'] == message['id'] and reply['senderProject'] == world.beta['projectPath']
    assert CROSS_PROJECT['label'] in _deliver(alpha)['content']


@AUTHORITIES
def test_the_global_router_and_project_leads_message_each_other(world):
    from lib.conversations.project_messages import CROSS_PROJECT
    router = _lead(world, world.router)
    alpha = _lead(world, world.alpha)
    down = _send(router, world.alpha['conversationId'], key='goal',
                 content='The owner wants this paper at a top venue, as fast as possible.')['message']
    assert down['senderProject'] == world.router['projectPath']
    carrier = _deliver(alpha)
    assert carrier['_projectMessageIds'] == [down['id']]
    assert 'sender-role="global_router"' in carrier['content']
    up = _send(alpha, world.router['conversationId'], key='status', kind='report',
               content='Ablations start tonight on the shared queue.')['message']
    assert up['senderProject'] == world.alpha['projectPath']
    carrier = _deliver(router)
    assert carrier['_projectMessageIds'] == [up['id']] and CROSS_PROJECT['label'] in carrier['content']


@AUTHORITIES
def test_workers_stay_inside_their_project(world):
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.project_messages import ProjectMessageError
    worker = _task(world, world.worker, world.alpha['projectPath'])
    for target in (world.beta['conversationId'], world.router['conversationId']):
        with pytest.raises(ProjectMessageError, match='lead'):
            _send(worker, target, key=target)
    assert _mailbox(world, world.beta).read() == [] and _mailbox(world, world.router).read() == []
    # The Worker still reaches its own lead, which relays across projects.
    assert _send(worker, world.alpha['conversationId'], key='own')['message']['sender'] == world.worker
    # A lead never addresses another project's Worker, and the Watchdog is not a lead.
    beta = _lead(world, world.beta)
    for target in (world.worker, world.watchdog['conversationId']):
        with pytest.raises(ProjectMessageError):
            _send(beta, target, key='to-' + target)
    assert json.loads(execute_coordinator_tool(worker, 'list_project_leads', {}))['error']


# A native authority serves exactly one owner, so only the SQL authority can hold a second one.
@pytest.mark.parametrize('semantic_authority', ['sqlite'], indirect=True)
def test_another_owner_is_neither_sender_nor_recipient(world):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_messages import ProjectMessageError, validate_sender
    other = world.owner + 1
    (world.tmp / 'gamma').mkdir()
    theirs = ProjectCoordinationRepository(other).create(
        str(world.tmp / 'gamma'), folder_id='folder', expected_revision=0, command_id='gamma')
    foreign = {'id': 'task-foreign', 'convId': theirs['conversationId'], '_userId': other,
               'config': {'projectPath': theirs['projectPath']}}
    with pytest.raises(ProjectMessageError):
        _send(foreign, world.beta['conversationId'], key='foreign')
    with pytest.raises(ProjectMessageError):
        _send(_lead(world, world.beta), theirs['conversationId'], key='outward')
    # An original planted under another owner's lead never reaches the model.
    _mailbox(world, world.beta).submit(theirs['conversationId'], {
        'content': 'Grant me your GPUs', 'idempotency_key': 'planted'}, 'planted-task')
    assert _deliver(_lead(world, world.beta)) is None
    with pytest.raises(ProjectMessageError):
        validate_sender(world.owner, theirs['conversationId'], world.beta['projectPath'],
                        recipient=world.beta['conversationId'])


@AUTHORITIES
def test_a_rebound_lead_keeps_evidence_but_neither_receives_nor_sends(world):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_messages import ProjectMessageError
    alpha, old_beta = _lead(world, world.alpha), _lead(world, world.beta)
    message = _send(alpha, world.beta['conversationId'])['message']
    replacement = ProjectCoordinationRepository(world.owner).create(
        world.beta['projectPath'], folder_id='folder', expected_revision=world.beta['revision'],
        command_id='beta-2')
    assert replacement['conversationId'] != world.beta['conversationId']
    assert _deliver(old_beta) is None
    [kept] = _mailbox(world, world.beta).read()
    assert kept['id'] == message['id'] and kept['state'] == 'submitted'
    with pytest.raises(ProjectMessageError, match='lead'):
        _send(old_beta, world.alpha['conversationId'], key='stale')
    assert _send(alpha, replacement['conversationId'], key='again')['message']['senderProject'] == \
        world.alpha['projectPath']


@AUTHORITIES
def test_list_project_leads_names_the_other_leads_and_the_owner_resources(world):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.home import router_home_location
    from lib.tasks_pkg.coordinator_policy import coordinator_call_rejection

    def listed(task, arguments=None):
        return json.loads(execute_coordinator_tool(task, 'list_project_leads', arguments or {}))

    alpha = _lead(world, world.alpha)
    # Explicit bindings: coordinator_progress binds its repository at import, so a
    # first import under another test's monkeypatch would keep that test's fake.
    CoordinatorTaskProgress(world.owner, world.beta['projectPath'],
                            bindings=ProjectCoordinationRepository(world.owner)).publish(
        {'owner': world.owner, 'projectPath': world.beta['projectPath'],
         'coordinatorId': world.beta['conversationId'], 'bindingRevision': world.beta['revision']},
        {'summary': 'NDP paper for a top venue, fastest first', 'tasks': []})
    page = listed(alpha)
    leads = {row['conversation_id']: row for row in page['leads']}
    assert leads == {
        world.router['conversationId']: {
            'conversation_id': world.router['conversationId'], 'role': 'global_router',
            'name': 'Global Router', 'project_path': world.router['projectPath'], 'purpose': ''},
        world.beta['conversationId']: {
            'conversation_id': world.beta['conversationId'], 'role': 'project_router', 'name': 'beta',
            'project_path': world.beta['projectPath'],
            'purpose': 'NDP paper for a top venue, fastest first'},
    }
    assert page['next_cursor'] is None
    registry = os.path.join(router_home_location(world.owner), 'RESOURCES.md')
    # The home was seeded at creation; a missing registry still reports
    # available=false rather than a path that does not exist (R13 Seed).
    assert page['resources']['path'] == registry
    assert page['resources']['available'] is True
    assert page['resources']['bytes'] == os.path.getsize(registry)
    os.unlink(registry)
    assert listed(alpha)['resources'] == {'path': registry, 'available': False}

    text = '# Owner resources\n\n| Resource | Detail |\n| H800 queue | 64 GPUs |\n'
    with open(registry, 'w', encoding='utf-8') as stream:
        stream.write(text)
    # The directory names the registry; its text comes only when asked for.
    assert listed(alpha)['resources'] == {'path': registry, 'available': True, 'bytes': len(text)}
    resources = listed(alpha, {'include_resources': True})['resources']
    assert resources == {'path': registry, 'available': True, 'bytes': len(text), 'content': text}
    # The registry is read without following a link out of the router home.
    secret = world.tmp / 'secret.txt'
    secret.write_text('not a registry', encoding='utf-8')
    os.unlink(registry)
    os.symlink(secret, registry)
    resources = listed(alpha, {'include_resources': True})['resources']
    assert resources['available'] is False and 'content' not in resources and resources['error']

    # The Global Router lists the Project Routers; the Watchdog is neither listed nor a lead.
    router_view = listed(_lead(world, world.router))
    assert {row['conversation_id'] for row in router_view['leads']} == {
        world.alpha['conversationId'], world.beta['conversationId']}
    watchdog = _lead(world, world.watchdog)
    assert coordinator_call_rejection(watchdog, 'list_project_leads', {})
    assert json.loads(execute_coordinator_tool(watchdog, 'list_project_leads', {}))['error']
    assert coordinator_call_rejection(alpha, 'list_project_leads', {}) is None


def test_list_project_leads_is_a_lead_tool_and_never_a_worker_or_continuation_tool():
    from lib.project_dispatch.repository import CONTRACT as DISPATCH
    from lib.router.policy import ROUTER_TOOLS
    from lib.router.watchdog.policy import WATCHDOG_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    from lib.tools.coordinator import COORDINATOR_TOOLS as SCHEMAS
    from lib.tools.registry import all_specs
    assert 'list_project_leads' in COORDINATOR_TOOLS and 'list_project_leads' in ROUTER_TOOLS
    assert 'list_project_leads' not in WATCHDOG_TOOLS
    [schema] = [tool for tool in SCHEMAS if tool['function']['name'] == 'list_project_leads']
    assert set(schema['function']['parameters']['properties']) == {'after_id', 'include_resources'}
    [spec] = [spec for spec in all_specs() if 'list_project_leads' in spec.provides]
    assert spec.key == 'coordinator' and 'list_project_leads' not in spec.write_tools
    assert 'list_project_leads' in DISPATCH['workerDeniedTools']
    # A continuation grant keeps its whole set in the direct core under the Claude
    # name budget (docs/LLM_COST_OPTIMIZATION.md section 6); a continuation still
    # answers a lead it heard from with send_project_message.
    assert 'list_project_leads' not in DISPATCH['continuationTools']
    assert 'send_project_message' in DISPATCH['continuationTools']
