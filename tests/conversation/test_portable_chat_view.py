"""The offline archive remains usable independently of database import."""
import hashlib

import pytest

from lib.conv_ref.portable import PortableChatError
from lib.conv_ref.portable_view import read_archive_chat
from tests.conversation.test_portable_chat_package import source  # noqa: F401
__all__ = ['source']

pytestmark = pytest.mark.unit


def test_archive_view_is_inert_and_reads_only_selected_originals(source):
    export, chats, _, _, original = source
    chats['chat-a'][1]['content'] = '<img src="https://example.invalid/no-fetch">'
    raw = export(('chat-a', 'chat-b'))
    display, blobs = read_archive_chat(raw, 'chat-a')
    assert display['source'] == 'synthetic-origin'
    assert display['messages'][1]['text'] == chats['chat-a'][1]['content']
    assert blobs == {hashlib.sha256(original).hexdigest(): original}
    assert read_archive_chat(raw, 'chat-b')[1] == {}
    with pytest.raises(PortableChatError, match='conversation_not_in_package'):
        read_archive_chat(raw, 'unknown')
    with pytest.raises(PortableChatError):
        read_archive_chat(raw[:-1], 'chat-b')
