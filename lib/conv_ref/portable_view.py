"""Read-only local view of a selected-chat archive; no storage/renderer integrations."""
from __future__ import annotations

import json

from .portable import PortableChatError, validate_package


def read_archive_chat(raw: bytes, conversation_id: str) -> tuple[dict, dict[str, bytes]]:
    """Return plain display data and exact original bytes, without extracting files.

    Callers must display prose as text, not HTML; attachment digests are labels,
    never paths. Validates the whole archive, even when displaying one chat.
    """
    package = validate_package(raw)
    for chat in package.chats:
        if chat.conversation_id == conversation_id:
            document = json.loads(chat.document)
            document['source'] = chat.source_id
            selected = {value for message in document['messages'] for value in message['originals']}
            return document, {key: value for key, value in package.originals if key in selected}
    raise PortableChatError('conversation_not_in_package')
