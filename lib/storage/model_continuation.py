"""Durable derived model context, never transcript authority.

ModelContinuationStore.get/put mirror ModelReplayCache's keyword API. A single
latest checkpoint per owner/conversation/lane survives process and cache loss.
Turn settlement carries prepared candidates atomically. Standalone put remains
for explicit settled imports; restore validates the full prefix and policy.
"""
from __future__ import annotations

import hashlib
import json
import uuid

MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 16384
MAX_TURN_WITNESSES = 4096
_DISPLAY_ONLY_FIELDS = frozenset({
    'timingTrace', 'translation', 'apiRounds', 'usage', 'cost', 'lastRoundUsage',
    'activityTimeline', 'modifiedFiles', 'modifiedFileList', 'fileChanges', 'provenance',
})


def settled_witness(row):
    """Version-one model-visible Turn witness (display-only updates excluded)."""
    source = {key: row.get(key) for key in ('actor', 'laneId', 'parentTurnId', 'status')}
    source['projection'] = {key: value for key, value in (row.get('projection') or {}).items()
                            if key not in _DISPLAY_ONLY_FIELDS}
    digest = hashlib.sha256()
    for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).iterencode(source):
        digest.update(part.encode('utf-8'))
    return [row['turnId'], digest.hexdigest()]


def encode_projection(projection):
    """Return bounded JSON or None; malformed/oversized input never replaces state."""
    if not isinstance(projection, dict):
        return None
    messages, turns = projection.get('messages'), projection.get('turns')
    if (not isinstance(messages, list) or not 0 < len(messages) <= MAX_MESSAGES
            or not all(isinstance(message, dict) for message in messages)
            or messages[-1].get('role') != 'assistant'
            or not isinstance(turns, list) or not 0 < len(turns) <= MAX_TURN_WITNESSES):
        return None
    for witness in turns:
        if (not isinstance(witness, list) or len(witness) != 2
                or not isinstance(witness[0], str) or not 0 < len(witness[0]) <= 256
                or not isinstance(witness[1], str) or len(witness[1]) != 64
                or any(char not in '0123456789abcdef' for char in witness[1])):
            return None
    encoded = bytearray()
    try:
        for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).iterencode(projection):
            if len(part) + len(encoded) > MAX_ENTRY_BYTES:
                return None
            chunk = part.encode('utf-8')
            if len(chunk) + len(encoded) > MAX_ENTRY_BYTES:
                return None
            encoded.extend(chunk)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None
    return encoded.decode('utf-8')


class ModelContinuationStore:
    """Owner-scoped semantic storage port; selected-authority errors propagate."""

    def __init__(self, *, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is not None:
            return self._client
        from lib.storage import get_storage_client
        return get_storage_client()

    def get(self, *, user_id, conversation_id, lane_id):
        return self.client.query('model_continuation.get', {
            'user_id': user_id, 'conversation_id': conversation_id,
            'lane_id': lane_id}, deadline=5.0)

    def put(self, *, user_id, conversation_id, lane_id, projection):
        if encode_projection(projection) is None:
            return False
        return bool(self.client.command('model_continuation.put', {
            'user_id': user_id, 'conversation_id': conversation_id,
            'lane_id': lane_id, 'projection': projection},
            command_id='model-continuation:' + uuid.uuid4().hex, deadline=5.0))

    def delete(self, *, user_id, conversation_id, lane_id):
        return bool(self.client.command('model_continuation.delete', {
            'user_id': user_id, 'conversation_id': conversation_id,
            'lane_id': lane_id}, command_id='model-continuation-delete:' + uuid.uuid4().hex,
            deadline=5.0))
