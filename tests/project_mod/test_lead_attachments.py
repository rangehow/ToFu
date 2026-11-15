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
