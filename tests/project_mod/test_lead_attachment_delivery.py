"""Every lead-to-Worker message path carries attachments the way a human message does.

The delegation brief, a follow_up instruction and a project message share one
admission (``lead_attachments.admit``, specified on a real Sidecar in
``test_lead_attachments.py``); here the ports are stubbed so each path is shown
to deliver the admitted records: a Turn input carries canonical ``attachments``
(rendered like a human's), a mailbox message reaches the running model with the
attachment projection, and a refusal sends nothing.
"""
from copy import deepcopy

import pytest

from lib.conversations import lead_attachments
from tests.project_mod.test_coordinator_session_control import _ended_worker
from tests.project_mod.test_coordinator_workers import runtime as worker_runtime, base_runtime, context
from tests.project_mod.test_project_messages import setup

__all__ = ('worker_runtime', 'base_runtime', 'context', 'setup')

pytestmark = pytest.mark.unit

RECORD = {'attachmentId': 'a' * 32, 'name': 'design.pdf', 'mimeType': 'application/pdf'}


def _canonical(record):
    return {'attachmentId': record['attachmentId'], 'kind': 'document', 'name': record['name'],
            'status': 'ready', 'mimeType': record['mimeType'], 'sizeBytes': 10,
            'sourceUrl': f'/api/v4/media/attachments/{record["attachmentId"]}/source'}


@pytest.fixture
def ports(monkeypatch):
    admitted = []

    def admit(task, owner, references):
        if not references:
            return []
        if any(reference.startswith('/etc/') for reference in references):
            raise lead_attachments.AttachmentRefused(f'{references[0]} is outside the files you can read')
        admitted.append((task['convId'], owner, list(references)))
        return [dict(RECORD)]
    monkeypatch.setattr(lead_attachments, 'admit', admit)
    monkeypatch.setattr(lead_attachments, 'turn_attachments',
                        lambda records, *, owner: [_canonical(record) for record in records])
    monkeypatch.setattr(lead_attachments, 'model_blocks', lambda records, **kw: [
        {'type': 'text', 'text': f'[Attachment 1: {record["name"]}; attachment ref: att_media_{record["attachmentId"]}]'}
        for record in records] + [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}])
    return admitted


def _capture_turns(r):
    inputs = []
    original = r.service.commands.create_turn

    def create_turn(conv, owner, request, **kwargs):
        inputs.append(deepcopy(request['inputTurn']))
        return original(conv, owner, request, **kwargs)
    r.service.commands.create_turn = create_turn
    return inputs


def test_a_delegation_brief_carries_its_attachments_into_the_worker_input(worker_runtime, ports):
    r = worker_runtime
    inputs = _capture_turns(r)
    worker = r.call('delegate_project_work', **r.args, attachments=['docs/design.pdf'])
    assert 'error' not in worker, worker
    assert ports == [('coordinator', 7, ['docs/design.pdf'])]
    [turn_input] = inputs
    assert turn_input['attachments'] == [_canonical(RECORD)]
    assert r.c.repo.read(worker['assignment_id'])['request']['attachments'] == [RECORD]
    # The same arguments replay the same Worker.
    assert r.call('delegate_project_work', **r.args, attachments=['docs/design.pdf'])['assignment_id'] \
        == worker['assignment_id']


@pytest.mark.parametrize('denial', ['plan_mode', 'stale_attempt', 'revoked_replay'])
def test_denied_delegation_does_not_admit_attachments(worker_runtime, ports, denial):
    r = worker_runtime
    if denial == 'plan_mode':
        r.task['config']['planMode'] = True
    elif denial == 'stale_attempt':
        r.attempts['coordinator-attempt']['status'] = 'interrupted'
    else:
        worker = r.call('delegate_project_work', **r.args)
        r.c.repo.revoke(worker['assignment_id'], 1)
    before = len(r.calls), len(r.created)
    refused = r.call('delegate_project_work', **r.args, attachments=['docs/design.pdf'])
    assert 'error' in refused, refused
    assert not ports
    assert (len(r.calls), len(r.created)) == before


def test_attachment_preflight_does_not_replace_the_workers_live_authority(worker_runtime, monkeypatch, ports):
    r = worker_runtime
    admit = lead_attachments.admit

    def interrupted_during_retention(*args):
        records = admit(*args)
        r.attempts['coordinator-attempt']['status'] = 'interrupted'
        return records

    monkeypatch.setattr(lead_attachments, 'admit', interrupted_during_retention)
    refused = r.call('delegate_project_work', **r.args, attachments=['docs/design.pdf'])
    assert 'current owned Coordinator attempt' in refused['error'], refused
    assert len(ports) == 1 and not r.calls and not r.created


def test_a_refused_attachment_creates_no_worker(worker_runtime, ports):
    r = worker_runtime
    refused = r.call('delegate_project_work', **r.args, attachments=['/etc/passwd'])
    assert refused['code'] == 'attachment_refused' and refused['execution_started'] is False, refused
    assert 'att_media_' in refused['recovery']
    assert not r.calls


def test_a_follow_up_instruction_carries_its_attachments(worker_runtime, monkeypatch, ports):
    from types import SimpleNamespace
    from lib.conversation_sync import runtime
    r = worker_runtime
    worker, row = _ended_worker(r, monkeypatch)
    execution = row['execution']
    created = []

    class Commands:
        def create_turn(self, conv, owner, body, **kwargs):
            created.append(deepcopy(body['inputTurn']))
            r.turns['follow-up-turn'] = {'conversationId': conv, 'turnId': 'follow-up-turn',
                                         'actor': 'assistant', 'projectionRevision': 1}
            r.attempts['follow-up-attempt'] = {'conversationId': conv, 'turnId': 'follow-up-turn',
                'attemptId': 'follow-up-attempt', 'taskId': '', 'status': 'pending',
                'commandId': body['commandId']}
            kwargs['trusted_dispatch'].continue_execution('follow-up-turn', 'follow-up-attempt')
            return SimpleNamespace(value={'turn': r.turns['follow-up-turn'],
                                          'attempt': r.attempts['follow-up-attempt']})

    r.service.commands = Commands()
    monkeypatch.setattr(runtime, 'conversation_turn_commands', Commands())
    args = dict(conversation_id=worker['conversation_id'], turn_id=execution['turnId'],
                attempt_id=execution['attemptId'], expected_revision=3, idempotency_key='next',
                instruction='Use the attached design.')
    refused = r.call('control_conversation', **args, action='follow_up', attachments=['/etc/shadow'])
    assert refused['code'] == 'attachment_refused' and not created, refused
    misplaced = r.call('control_conversation', **args, action='continue', attachments=['design.pdf'])
    assert 'follow_up instruction only' in misplaced['error'] and not created, misplaced
    result = r.call('control_conversation', **args, action='follow_up', attachments=['design.pdf'])
    assert 'error' not in result, result
    [turn_input] = created
    assert turn_input['attachments'] == [_canonical(RECORD)]
    assert 'Use the attached design.' in turn_input['content']


def test_a_project_message_delivers_its_attachments_to_the_running_model(setup, ports):
    from lib.conversations import project_messages as pm
    from lib.tasks_pkg.message_input_receipts import matched_ids
    sent = pm.execute('send_project_message', {'conversation_id': 'recipient', 'content': 'See the design',
                                               'idempotency_key': 'one', 'attachments': ['design.pdf']},
                      setup.sender)['message']
    assert sent['attachments'] == [RECORD]
    with pytest.raises(lead_attachments.AttachmentRefused):
        pm.execute('send_project_message', {'conversation_id': 'recipient', 'content': 'x',
                                            'idempotency_key': 'two', 'attachments': ['/etc/passwd']},
                   setup.sender)
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(setup.recipient, messages, 0)
    carrier = messages[-1]
    assert isinstance(carrier['content'], list)
    *media, text = carrier['content']
    assert [block['type'] for block in media] == ['text', 'image_url']
    assert f'attachment_ref="att_media_{RECORD["attachmentId"]}"' in text['text']
    assert 'name="design.pdf"' in text['text'] and carrier['_containsHumanSteer'] is False
    # The carrier's own text is its input witness, so delivery confirms once.
    setup.recipient['_verifiedMessageInputIds'] = matched_ids(setup.recipient, {'messages': messages})
    pm.confirm(setup.recipient, 0)
    [incoming] = pm.execute('get_project_messages', {}, setup.recipient)['messages']
    assert incoming['state'] == 'input_confirmed' and incoming['attachments'] == [RECORD]


def test_both_message_contracts_declare_one_attachment_shape():
    """send_project_message lives in its own contract; its attachment shapes are the dispatch ones.

    Its attachments description is the one full definition; the dispatch
    reference only points to it (tests/tasks_pkg/test_lead_schema_single_definitions.py).
    """
    from lib.conversations.project_messages import CONTRACT as messages
    from lib.project_dispatch.repository import CONTRACT as dispatch
    from lib.tools.coordinator import COORDINATOR_TOOLS
    send = next(tool for tool in messages['tools'] if tool['function']['name'] == 'send_project_message')
    send_attachments = dict(send['function']['parameters']['properties']['attachments'])
    dispatch_references = dict(dispatch['$defs']['leadAttachmentReferences'])
    assert 'send_project_message' in dispatch_references.pop('description')
    assert 'through the shared attachment pipeline' in send_attachments.pop('description')
    assert send_attachments == dispatch_references
    assert messages['$defs']['attachment'] == dispatch['$defs']['leadAttachment']
    tools = {tool['function']['name']: tool['function']['parameters']['properties'] for tool in COORDINATOR_TOOLS}
    for name in ('delegate_project_work', 'control_conversation'):
        assert tools[name]['attachments']['items'] == dispatch['$defs']['leadAttachmentReferences']['items']
