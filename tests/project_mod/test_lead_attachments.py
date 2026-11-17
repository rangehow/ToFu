"""A lead's message attachments reuse the human attachment pipeline, behind one boundary.

Runs ``lib.conversations.lead_attachments`` against a real knowledge Sidecar:
what a sender may read becomes the same retained media attachment a human's
chat upload is, and the recipient's model projection shows images, document
text and every other file's stored original; anything outside the sender's
readable scope is refused before a record exists.
"""
from __future__ import annotations

import io
import os

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)

OWNER = 1


def _png() -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (30, 120, 200)).save(buffer, format='PNG')
    return buffer.getvalue()


def _pdf(text: str) -> bytes:
    import fitz
    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    return document.tobytes()


@pytest.fixture
def lead(tmp_path, isolated_knowledge):
    root = tmp_path / 'project'
    root.mkdir()
    task = {'id': 'lead-task', 'convId': 'lead', '_userId': OWNER,
            'config': {'projectPath': str(root)}, 'messages': []}
    return task, root


def _admit(task, references):
    from lib.conversations.lead_attachments import admit
    return admit(task, OWNER, references)


def _projection(records, query=''):
    from lib.conversations.lead_attachments import model_blocks
    return model_blocks(records, owner=OWNER, query=query, model='')


def test_an_image_arrives_viewable_as_a_human_attachment(lead):
    from lib.attachments import resolve_attachment
    from lib.conversations.lead_attachments import turn_attachments
    task, root = lead
    (root / 'screens').mkdir()
    (root / 'screens' / 'login.png').write_bytes(_png())
    [record] = _admit(task, ['screens/login.png'])
    assert record['name'] == 'login.png' and record['mimeType'] == 'image/png'
    [turn_ref] = turn_attachments([record], owner=OWNER)
    assert turn_ref['kind'] == 'document' and turn_ref['status'] == 'ready'
    assert turn_ref['sourceUrl'] == f'/api/v4/media/attachments/{record["attachmentId"]}/source'
    blocks = _projection([record])
    assert any(block['type'] == 'image_url' for block in blocks)
    seen = resolve_attachment(f'att_media_{record["attachmentId"]}', user_id=OWNER)
    assert seen['kind'] == 'image' and seen['raw'] == _png()


def test_documents_arrive_readable_and_retained(lead, isolated_knowledge):
    from lib.attachments import read_text_attachment_ref
    task, root = lead
    (root / 'spec.pdf').write_bytes(_pdf('Quarterly revenue grew twelve percent'))
    (root / 'notes.md').write_text('# Plan\nShip the attachment boundary.\n', encoding='utf-8')
    pdf, notes = _admit(task, [str(root / 'spec.pdf'), 'notes.md'])
    assert pdf['mimeType'] == 'application/pdf'
    text = '\n'.join(block.get('text', '') for block in _projection([pdf, notes]))
    assert 'Quarterly revenue grew twelve percent' in text and 'Ship the attachment boundary.' in text
    assert f'att_media_{pdf["attachmentId"]}' in text
    read = read_text_attachment_ref(f'att_media_{notes["attachmentId"]}', user_id=OWNER)
    assert 'Ship the attachment boundary.' in read['text']
    document = isolated_knowledge.get_document_metadata(notes['attachmentId'], user_id=OWNER)
    assert document['scope'] == 'attachment'
    assert document['media_metadata']['origin'] == 'agent_message'
    assert document['media_metadata']['sender_conversation'] == 'lead'


def test_any_other_file_type_arrives_as_its_stored_original(lead):
    task, root = lead
    payload = bytes(range(256)) * 16
    (root / 'model.weights').write_bytes(payload)
    [record] = _admit(task, ['model.weights'])
    [header] = [block['text'] for block in _projection([record]) if block['type'] == 'text']
    assert f'att_media_{record["attachmentId"]}' in header
    original = header.split('original file: ', 1)[1].split(']', 1)[0]
    with open(original, 'rb') as stream:
        assert stream.read() == payload


def test_an_owner_attachment_and_a_conversation_image_are_readable_references(lead, monkeypatch, tmp_path):
    from lib import attachments
    task, root = lead
    (root / 'a.md').write_text('first', encoding='utf-8')
    [record] = _admit(task, ['a.md'])
    assert _admit(task, [f'att_media_{record["attachmentId"]}']) == [record]
    images = tmp_path / 'images'
    images.mkdir()
    (images / 'shot.png').write_bytes(_png())
    monkeypatch.setattr(attachments, '_images_dir', lambda: str(images))
    task['messages'] = [{'role': 'user', 'content': 'see', 'images': [
        {'url': '/proxy/15000/api/images/shot.png', 'preview': '/proxy/15000/api/images/shot.png'}]}]
    [image] = _admit(task, ['/api/images/shot.png'])
    assert image['mimeType'] == 'image/png'
    # One file named twice is one attachment.
    assert len(_admit(task, ['a.md', str(root / 'a.md')])) == 1


def _steer(image: str, author) -> dict:
    record = {'blockId': 'injection:user-steer:s1', 'commandId': 's1', 'deliveryState': 'delivered',
              'previews': [{'text': 'like this', 'images': [{'preview': image, 'url': image}]}]}
    if author is not None:
        record['author'] = author
    return record


_OWNER_AUTHOR = {'actor': 'human', 'text': 'like this'}


@pytest.mark.parametrize('lane', ['delivered_here', 'committed_by_another_process'])
def test_an_image_the_owner_steered_into_the_running_turn_is_attachable(lead, monkeypatch, tmp_path, lane):
    """2026-11-17: a Coordinator could not forward the screenshot the owner steered
    into its running turn: a steer never enters ``task['messages']``."""
    from lib import attachments
    from lib.tasks_pkg import committed_steers
    task, _root = lead
    images = tmp_path / 'images'
    images.mkdir()
    (images / 'steered.png').write_bytes(_png())
    monkeypatch.setattr(attachments, '_images_dir', lambda: str(images))
    record = _steer('/api/images/steered.png', _OWNER_AUTHOR)
    if lane == 'delivered_here':
        task['_userSteerInjects'] = [record]
        monkeypatch.setattr(committed_steers, 'live_turn_steer_records', lambda task: [])
    else:
        monkeypatch.setattr(committed_steers, 'live_turn_steer_records', lambda task: [record])
    [image] = _admit(task, ['/api/images/steered.png'])
    assert image['mimeType'] == 'image/png'


@pytest.mark.parametrize('author', [
    None,
    {'actor': 'assistant', 'text': 'like this'},
    {'actor': 'human', 'text': 'like this', 'origin': {'initiator': 'human', 'forwardedFrom': {
        'conversationId': 'router', 'turnId': 't'}}},
], ids=['nonhuman_steer', 'agent_author', 'router_forward'])
def test_an_image_steered_by_anyone_but_the_owner_here_is_refused(lead, monkeypatch, tmp_path, author):
    from lib import attachments
    from lib.conversations.lead_attachments import AttachmentRefused
    from lib.tasks_pkg import committed_steers
    task, _root = lead
    images = tmp_path / 'images'
    images.mkdir()
    (images / 'steered.png').write_bytes(_png())
    monkeypatch.setattr(attachments, '_images_dir', lambda: str(images))
    task['_userSteerInjects'] = [_steer('/api/images/steered.png', author)]
    monkeypatch.setattr(committed_steers, 'live_turn_steer_records', lambda task: [])
    with pytest.raises(AttachmentRefused, match='not an image shown in this conversation'):
        _admit(task, ['/api/images/steered.png'])


@pytest.mark.parametrize('case', [
    'outside', 'traversal', 'symlink', 'directory', 'missing', 'foreign_attachment', 'unseen_image',
])
def test_the_boundary_refuses_what_the_lead_cannot_read(lead, tmp_path, monkeypatch, case):
    from lib import attachments
    from lib.conversations.lead_attachments import AttachmentRefused
    from lib.knowledge import list_documents
    task, root = lead
    secret = tmp_path / 'secret.txt'
    secret.write_text('not yours', encoding='utf-8')
    (root / 'sub').mkdir()
    os.symlink(secret, root / 'link.txt')
    images = tmp_path / 'images'
    images.mkdir()
    (images / 'other.png').write_bytes(_png())
    monkeypatch.setattr(attachments, '_images_dir', lambda: str(images))
    reference = {
        'outside': str(secret), 'traversal': 'sub/../../secret.txt', 'symlink': 'link.txt',
        'directory': 'sub', 'missing': 'absent.txt',
        'foreign_attachment': 'att_media_' + 'f' * 32, 'unseen_image': '/api/images/other.png',
    }[case]
    with pytest.raises(AttachmentRefused) as refused:
        _admit(task, [reference])
    assert refused.value.code == 'attachment_refused' and refused.value.execution_started is False
    assert 'not yours' not in str(refused.value)
    assert not list_documents(user_id=OWNER)


def test_another_owners_attachment_is_refused(lead):
    from lib.conversations.lead_attachments import AttachmentRefused, admit
    task, root = lead
    (root / 'mine.md').write_text('owner one', encoding='utf-8')
    [record] = _admit(task, ['mine.md'])
    with pytest.raises(AttachmentRefused):
        admit({**task, '_userId': 2}, 2, [f'att_media_{record["attachmentId"]}'])


@pytest.fixture
def project_lead(lead, tmp_path, monkeypatch):
    """The lead task is a Project Router: it reads registered projects and its own Workers' checkouts."""
    from lib.conversations import attachment_secrets
    from lib.conversations import lead_attachments
    task, root = lead
    scope = {'user_id': OWNER, 'project_path': str(root), 'conversation_id': 'lead', 'binding_revision': 1}
    monkeypatch.setattr(lead_attachments, 'lead_scope', lambda t: dict(scope) if t.get('convId') == 'lead' else None)
    other = tmp_path / 'knowledge'
    other.mkdir()
    monkeypatch.setattr('lib.router.registered_projects.registered_project_roots',
                        lambda owner, client=None: [str(root), str(other)])
    checkout = tmp_path / 'checkouts' / 'own'
    checkout.mkdir(parents=True)
    rows = {'own': {'id': 'own', 'request': {'coordinatorId': 'lead'}},
            'theirs': {'id': 'theirs', 'request': {'coordinatorId': 'another-lead'}}}

    class Repository:
        def __init__(self, owner, project):
            self.owner, self.project = owner, project

        def read(self, assignment_id):
            from lib.project_dispatch.repository import ProjectDispatchError
            if assignment_id not in rows:
                raise ProjectDispatchError('Dispatch assignment not found in owned project')
            return rows[assignment_id]

        def workspace_task_id(self, row):
            return 'project-worker:' + row['id']

        def get_workspace(self, project_root, task_id, *, user_id):
            return {'workspace_path': str(checkout), 'state': 'active'} if task_id.endswith(':own') else None
    monkeypatch.setattr('lib.project_dispatch.repository.ProjectDispatchRepository', Repository)
    monkeypatch.setattr(attachment_secrets, '_vault_values', lambda: [('hope_token', b'vault-secret-0042')])
    return task, root, other, checkout


def test_a_lead_attaches_files_of_any_registered_project_and_its_own_workers_checkout(project_lead):
    """Lead audit 2026-11-18 #5 (A5): leads copied files into their own home through Workers first."""
    from lib.conversations.lead_attachments import AttachmentRefused
    task, root, other, checkout = project_lead
    (other / 'paper.md').write_text('# Paper A\n', encoding='utf-8')
    (checkout / 'mockup.md').write_text('# Mockup\n', encoding='utf-8')
    paper, mockup = _admit(task, [str(other / 'paper.md'), 'worker:own/mockup.md'])
    assert (paper['name'], mockup['name']) == ('paper.md', 'mockup.md')
    for reference in ('worker:theirs/mockup.md', 'worker:missing/mockup.md', 'worker:own/../../secret.txt'):
        with pytest.raises(AttachmentRefused):
            _admit(task, [reference])


def test_a_worker_or_ordinary_conversation_gains_no_registered_roots(project_lead):
    from lib.conversations.lead_attachments import AttachmentRefused
    task, root, other, checkout = project_lead
    (other / 'paper.md').write_text('# Paper A\n', encoding='utf-8')
    with pytest.raises(AttachmentRefused):
        _admit({**task, 'convId': 'worker-a'}, [str(other / 'paper.md')])
    with pytest.raises(AttachmentRefused):
        _admit({**task, 'convId': 'worker-a'}, ['worker:own/mockup.md'])


@pytest.mark.parametrize('name, content', [
    ('.env', b'MODE=prod\n'), ('deploy.pem', b'harmless'), ('id_ed25519', b'key'),
    ('run.log', b'using vault-secret-0042 for the job\n'),
    ('notes.txt', b'-----BEGIN RSA PRIVATE KEY-----\nMIIE\n'),
])
def test_a_lead_never_attaches_a_file_carrying_a_credential(project_lead, name, content):
    from lib.conversations.lead_attachments import AttachmentRefused
    from lib.knowledge import list_documents
    task, root, other, checkout = project_lead
    (other / name).write_bytes(content)
    with pytest.raises(AttachmentRefused) as refused:
        _admit(task, [str(other / name)])
    assert 'credential' in str(refused.value) or 'key' in str(refused.value)
    assert 'vault-secret-0042' not in str(refused.value)
    assert not list_documents(user_id=OWNER)
