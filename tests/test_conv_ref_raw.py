"""tests/test_conv_ref_raw.py — raw-mode get_conversation.

Pins the debugging surface of ``get_conversation(raw=True)``: it must emit the
full DB record — the row-level metadata columns (created_at, updated_at,
msg_count, rev), the raw settings, and EVERY field of every message
(finishReason, usage, model, _msgId, toolRounds, …) — as a structured JSON
dump, instead of the lossy prose transcript. Also pins that the readable
transcript still works and that backend-agnostic JSON coercion tolerates an
already-decoded (PG-style) messages value.

Seeds rows directly via the shared ``upsert`` path (mirrors
test_conv_ref_scope.py) so it runs on whichever backend the test DB uses.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conv_ref import build_conversation_digest, get_conversation
from lib.conv_ref._detail import _coerce_json


@pytest.mark.api
class TestGetConversationRaw:
    @pytest.fixture(autouse=True)
    def seed(self, flask_client):
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database._core_schema import CONVERSATIONS, upsert

        now = int(time.time() * 1000)
        tag = f'{now}'
        self.cid = f'cvraw-{tag}'
        self.messages = [
            {'role': 'user', 'content': 'hello there', '_msgId': 'u-1',
             'timestamp': now},
            {'role': 'assistant', 'content': 'hi!', '_msgId': 'a-1',
             'model': 'test-model-x', 'finishReason': 'stop',
             'usage': {'input_tokens': 10, 'output_tokens': 3},
             'modifiedFileList': ['lib/foo.py'],
             'toolRounds': [{'toolName': 'read_files', 'status': 'done',
                             'args': {'path': 'lib/foo.py'}}]},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            'id': self.cid, 'user_id': 1, 'title': 'Raw Test',
            'messages': json.dumps(self.messages),
            'created_at': now, 'updated_at': now + 5,
            'settings': json.dumps({'preset': 'sonnet', 'projectPath': '/tmp/x'}),
            'msg_count': 2, 'search_text': 'hello there hi',
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                        'updated_at', 'settings', 'msg_count', 'search_text'],
           retry=True)
        yield
        db.execute('DELETE FROM conversations WHERE id=?', (self.cid,))
        db.commit()

    def test_raw_dump_preserves_message_metadata(self):
        out = get_conversation(self.cid, raw=True)
        assert 'Raw Conversation Record' in out
        # The metadata fields the prose transcript drops must all appear.
        for token in ('finishReason', 'test-model-x', '_msgId', 'usage',
                      'modifiedFileList', 'toolRounds', 'input_tokens'):
            assert token in out, f'missing {token!r} in raw dump'
        # Row-level columns must appear too.
        assert 'msg_count' in out and 'rev' in out and 'updated_at' in out
        # It's a fenced JSON block that round-trips.
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        record = json.loads(body)
        assert record['id'] == self.cid
        assert record['msg_count'] == 2
        assert record['settings']['preset'] == 'sonnet'
        assert record['messages'][1]['finishReason'] == 'stop'

    def test_prose_transcript_still_default(self):
        out = get_conversation(self.cid)
        assert 'Referenced Conversation' in out
        assert 'hello there' in out
        # Prose mode does NOT dump the raw metadata keys verbatim.
        assert 'finishReason' not in out

    def test_coerce_json_tolerates_decoded_value(self):
        # PG driver could hand back an already-decoded list/dict — the fallback.
        already = [{'role': 'user', 'content': 'x'}]
        assert _coerce_json(already, default=[]) is already
        assert _coerce_json('{"a": 1}', default={}) == {'a': 1}
        assert _coerce_json(None, default=[]) == []

    def test_raw_missing_conversation(self):
        out = get_conversation('does-not-exist-xyz', raw=True)
        assert 'not found' in out.lower()

    def test_build_digest_structure(self):
        d = build_conversation_digest(self.cid)
        assert d is not None
        assert d['convId'] == self.cid
        assert d['title'] == 'Raw Test'
        assert d['preset'] == 'sonnet'
        assert d['msgCount'] == 2
        assert len(d['messages']) == 2
        # Row-level timestamps are now carried (the "multi-query DB" ask).
        assert d['createdAt'] > 0 and d['updatedAt'] > 0
        assert d['updatedAt'] >= d['createdAt']
        assert d['truncated'] is False and d['omitted'] == 0
        # user row: text preview, per-message timestamp, no tools
        u = d['messages'][0]
        assert u['role'] == 'user' and 'hello there' in u['text']
        assert u['ts'] > 0  # message-level timestamp surfaced
        assert 'tools' not in u
        # assistant row: tools are now rich descriptors (name + arg + status),
        # NOT bare tool-name strings.
        a = d['messages'][1]
        assert a['role'] == 'assistant'
        assert isinstance(a['tools'], list) and len(a['tools']) == 1
        td = a['tools'][0]
        assert td['name'] == 'read_files'
        assert td['arg'] == 'lib/foo.py'   # primary arg extracted from args.path
        assert td['status'] == 'done'

    def test_build_digest_self_reference_is_none(self):
        # Digesting the CURRENT conversation is a no-op (caller falls back).
        assert build_conversation_digest(self.cid, current_conv_id=self.cid) is None

    def test_build_digest_missing_is_none(self):
        assert build_conversation_digest('does-not-exist-xyz') is None

    def test_build_digest_long_preview(self, flask_client):
        # A message longer than the 400-char preview must be previewed to ~400
        # and carry an (expandable) `full` that is longer than the preview.
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database._core_schema import CONVERSATIONS, upsert
        now = int(time.time() * 1000)
        cid = f'cvlong-{now}'
        body = 'word ' * 400  # ~2000 chars
        msgs = [{'role': 'user', 'content': body, 'timestamp': now}]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            'id': cid, 'user_id': 1, 'title': 'Long', 'messages': json.dumps(msgs),
            'created_at': now, 'updated_at': now, 'settings': '{}',
            'msg_count': 1, 'search_text': 'word',
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                        'updated_at', 'settings', 'msg_count', 'search_text'],
           retry=True)
        try:
            d = build_conversation_digest(cid)
            row = d['messages'][0]
            # Preview is bounded (400 + ellipsis), full is much longer.
            assert 380 <= len(row['text']) <= 402
            assert row['text'].endswith('…')
            assert 'full' in row and len(row['full']) > len(row['text'])
        finally:
            db.execute('DELETE FROM conversations WHERE id=?', (cid,))
            db.commit()

    def test_build_digest_head_tail_omission(self, flask_client):
        # A conversation longer than head+tail keeps the first `head` and last
        # `tail`, drops the middle, and inserts a single {omitted: X} marker
        # between the head slice and the tail slice.
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database._core_schema import CONVERSATIONS, upsert
        now = int(time.time() * 1000)
        cid = f'cvht-{now}'
        # 20 messages; digest head=3, tail=5 → 12 omitted.
        msgs = [{'role': 'user' if i % 2 == 0 else 'assistant',
                 'content': f'msg-{i}', 'timestamp': now + i} for i in range(20)]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            'id': cid, 'user_id': 1, 'title': 'HT', 'messages': json.dumps(msgs),
            'created_at': now, 'updated_at': now + 100, 'settings': '{}',
            'msg_count': 20, 'search_text': 'msg',
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                        'updated_at', 'settings', 'msg_count', 'search_text'],
           retry=True)
        try:
            d = build_conversation_digest(cid, head=3, tail=5)
            assert d['msgCount'] == 20
            assert d['truncated'] is True and d['omitted'] == 12
            content_rows = [m for m in d['messages'] if 'omitted' not in m]
            marker_rows = [m for m in d['messages'] if 'omitted' in m]
            # Exactly one omission marker, carrying the omitted count.
            assert len(marker_rows) == 1 and marker_rows[0]['omitted'] == 12
            # 3 head + 5 tail content rows, original indices preserved.
            assert len(content_rows) == 8
            assert [m['index'] for m in content_rows[:3]] == [1, 2, 3]
            assert [m['index'] for m in content_rows[-5:]] == [16, 17, 18, 19, 20]
            # The head slice must contain msg-0 and the tail slice msg-19.
            assert 'msg-0' in content_rows[0]['text']
            assert 'msg-19' in content_rows[-1]['text']
        finally:
            db.execute('DELETE FROM conversations WHERE id=?', (cid,))
            db.commit()
