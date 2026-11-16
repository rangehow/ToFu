"""A replay cursor never silently skips a change it did not see (executable specification).

A cursor names the last change its holder observed. A reader can see a commit
before its write is durable; a crash then erases that change, and restart
recovery commits new changes that reuse its sequence before browsers reconnect
(WRITE-AMP-1's kill tests, DB-5's visibility gap). Replaying after such a
cursor would skip the new change and keep the lost one, so the stream must
answer ``sync.reset_required`` instead. An ordinary reconnect, including one
across a clean restart, still resumes without a reset, and a cursor issued
before the stream carried lineage resets exactly once.

Each crash is reproduced on both authorities by restoring the store copied at
the durable point and reopening it: exactly the state recovery leaves. The
specifications drive the real events route and snapshot service with cursor
strings, so they run unchanged against every cursor format.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlencode

import pytest

pytestmark = pytest.mark.unit

OWNER = 1


class _Authority:
    """One real storage authority whose store can be cut back to a durable point."""

    def __init__(self, runtime, root: Path):
        self._runtime = runtime
        self._data = root / 'data'
        self._durable = root / 'durable-point'

    def restart(self):
        """A clean stop and reopen: the same lineage under a new authority open."""
        self._runtime.stop()
        self._runtime.start()

    def mark_durable_point(self):
        self._runtime.stop()
        shutil.copytree(self._data, self._durable)
        self._runtime.start()

    def crash_to_durable_point(self):
        """Lose every commit after the durable point, then recover by reopening."""
        self._runtime.stop()
        shutil.rmtree(self._data)
        shutil.copytree(self._durable, self._data)
        self._runtime.start()


@pytest.fixture(params=['sqlite', 'tofudb'])
def authority(tmp_path, monkeypatch, request):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    if request.param == 'tofudb':
        from scripts.check_tofudb_engine_drift import resolve_deployment_binary
        monkeypatch.setenv('TOFUDB_BIN', resolve_deployment_binary(Path(__file__).resolve().parents[2]))
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    supervisor = StorageSupervisor(project_root=tmp_path, backend=request.param, startup_timeout=60)
    runtime = StorageRuntime(supervisor, auto_restart=False)
    install_runtime_for_test(runtime)
    try:
        runtime.start()
        yield _Authority(runtime, tmp_path)
    finally:
        try:
            runtime.stop()
        finally:
            install_runtime_for_test(None)


@pytest.fixture
def conversation(authority):
    return f'cursor-identity-{uuid.uuid4().hex[:12]}'


def _commit(conversation_id: str, text: str) -> None:
    """Commit one settled Turn through the canonical ingestion boundary."""
    from lib import turn_lifecycle

    turn_lifecycle.append_settled_turn(
        conversation_id, command_id=f'{conversation_id}-{text}', actor='assistant',
        projection={'content': text}, user_id=OWNER,
        conversation_defaults={'allowCreate': True, 'title': 'cursor identity', 'settings': {}},
    )


def _snapshot(conversation_id: str) -> dict:
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService

    return ConversationSyncService(SidecarConversationSyncRepository()).snapshot(
        conversation_id, OWNER)


def _frame(encoded: str) -> dict:
    fields = dict(line.split(': ', 1) for line in encoded.strip().split('\n'))
    return {'id': fields.get('id'), 'event': fields['event'], 'data': json.loads(fields['data'])}


def _resume(monkeypatch, conversation_id: str, *, after: str | None = None,
            last_event_id: str | None = None, cursor_source: str | None = None) -> list[dict]:
    """Open the events route like a browser and read until it settles.

    Reading stops at the first heartbeat (the stream caught up and confirmed
    its cursor) or reset (the stream ended); the subscription is then closed.
    """
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from routes import conversation_sync_v3 as routes

    class PromptHeartbeatService(ConversationSyncService):
        heartbeat_interval_ms = 1

    monkeypatch.setattr(routes, '_service', PromptHeartbeatService(SidecarConversationSyncRepository()))
    monkeypatch.setattr(routes, 'sse_response', lambda body, **_kwargs: body)
    query = {'streamClientId': 'cursor-identity', 'streamGeneration': '1',
             'diagnosticPayload': 'none'}
    if after is not None:
        query['after'] = after
    if cursor_source is not None:
        query['cursorSource'] = cursor_source
    headers = {'Last-Event-ID': last_event_id} if last_event_id else {}

    async def read() -> list[dict]:
        app = Quart(__name__)
        path = f'/api/v4/conversations/{conversation_id}/events?{urlencode(query)}'
        async with app.test_request_context(path, headers=headers):
            g.auth_ctx = local_admin_context()
            g.auth_ctx.user_id = OWNER
            g.rate_decision = None
            stream = await routes.conversation_sync_events(conversation_id)
            assert not isinstance(stream, tuple) and hasattr(stream, '__anext__'), stream
            frames = []
            try:
                async for encoded in stream:
                    frames.append(_frame(encoded))
                    if frames[-1]['event'] in {'sync.heartbeat', 'sync.reset_required'}:
                        break
            finally:
                await stream.aclose()
            return frames

    return asyncio.run(read())


def _history_page(monkeypatch, conversation_id: str, *, sync_seq: int,
                  cursor: str | None) -> tuple[int, dict]:
    """Read one main-lane history page through the real route, like a browser."""
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from routes import conversation_sync_v3 as routes

    monkeypatch.setattr(routes, '_service', ConversationSyncService(SidecarConversationSyncRepository()))
    query = {'laneId': 'main', 'syncSeq': str(sync_seq), 'limit': '8'}
    if cursor is not None:
        query['cursor'] = cursor

    async def read() -> tuple[int, dict]:
        app = Quart(__name__)
        # The matched route owns the response contract (history_page_stale's fields).
        app.register_blueprint(routes.conversation_sync_v3_bp)
        path = f'/api/v4/conversations/{conversation_id}/turns/history?{urlencode(query)}'
        async with app.test_request_context(path):
            g.auth_ctx = local_admin_context()
            g.auth_ctx.user_id = OWNER
            g.rate_decision = None
            response, status = await routes.conversation_turn_history_page(conversation_id)
            return status, json.loads(await response.get_data(as_text=True))

    return asyncio.run(read())


def _events(frames: list[dict]) -> list[str]:
    return [frame['event'] for frame in frames]


def _sequences(frames: list[dict]) -> list[int]:
    return [frame['data']['syncSeq'] for frame in frames if 'syncSeq' in frame['data']]


def test_a_cursor_whose_sequence_was_reused_after_crash_recovery_resets(
        authority, conversation, monkeypatch):
    _commit(conversation, 'durable')
    authority.mark_durable_point()
    _commit(conversation, 'lost')
    seen = _snapshot(conversation)
    authority.crash_to_durable_point()
    _commit(conversation, 'recovered')
    assert _snapshot(conversation)['syncSeq'] >= seen['syncSeq'], 'recovery must reuse the seen sequence'

    frames = _resume(monkeypatch, conversation, after=seen['cursor'])

    assert _events(frames) == ['sync.reset_required'], (
        'the reused sequence was replayed past as if its new change had been seen')
    assert frames[0]['data']['reason'] == 'cursor_invalid'


def test_a_native_reconnect_naming_a_lost_event_resets(authority, conversation, monkeypatch):
    _commit(conversation, 'durable')
    durable = _snapshot(conversation)
    authority.mark_durable_point()
    _commit(conversation, 'lost')
    replayed = _resume(monkeypatch, conversation, after=durable['cursor'],
                       cursor_source='snapshot')
    lost_event_id = [frame for frame in replayed if frame['event'] != 'sync.heartbeat'][-1]['id']
    authority.crash_to_durable_point()
    _commit(conversation, 'recovered')

    frames = _resume(monkeypatch, conversation, after=durable['cursor'], last_event_id=lost_event_id)

    assert _events(frames) == ['sync.reset_required']


def test_a_history_page_never_answers_a_replica_of_a_replaced_lineage(
        authority, conversation, monkeypatch):
    """A history page is fenced by the reader's lineage, not its bare head.

    The reader names its applied head with the cursor it holds there. After
    recovery gave that head to another change, a page read at it would carry
    Turns of a lineage the reader never replayed; the read is refused without
    a head to catch up to, so the browser repairs from one snapshot.
    """
    _commit(conversation, 'durable')
    authority.mark_durable_point()
    _commit(conversation, 'lost')
    seen = _snapshot(conversation)
    authority.crash_to_durable_point()
    _commit(conversation, 'recovered')
    recovered = _snapshot(conversation)
    assert recovered['syncSeq'] >= seen['syncSeq'], 'recovery must reuse the seen head'

    status, body = _history_page(monkeypatch, conversation, sync_seq=seen['syncSeq'],
                                 cursor=seen['cursor'])
    assert status == 409, (
        f'a page of the replaced lineage was served to a replica of the lost one: {body}')
    assert body['error'] == 'history_page_stale'
    assert 'currentSyncSeq' not in body, 'no head of this lineage can repair that replica'

    status, page = _history_page(monkeypatch, conversation, sync_seq=recovered['syncSeq'],
                                 cursor=recovered['cursor'])
    assert status == 200, page
    assert page['syncSeq'] == recovered['syncSeq']
    assert page['cursor'] == recovered['cursor']


def test_a_history_page_answers_the_same_lineage_across_a_clean_restart(
        authority, conversation, monkeypatch):
    _commit(conversation, 'one')
    held = _snapshot(conversation)
    authority.restart()

    status, page = _history_page(monkeypatch, conversation, sync_seq=held['syncSeq'],
                                 cursor=held['cursor'])
    assert status == 200, page
    assert page['syncSeq'] == held['syncSeq']
    assert [turn['turnId'] for turn in page['turns']] == [
        turn['turnId'] for turn in held['turns']]

    _commit(conversation, 'two')
    status, ahead = _history_page(monkeypatch, conversation, sync_seq=held['syncSeq'],
                                  cursor=held['cursor'])
    assert status == 200, ahead
    assert ahead['syncSeq'] == _snapshot(conversation)['syncSeq'], (
        'a reader behind the head still gets the page at the newest head of its lineage')


def test_a_cursor_beyond_the_recovered_head_resets(authority, conversation, monkeypatch):
    _commit(conversation, 'durable')
    authority.mark_durable_point()
    _commit(conversation, 'lost')
    seen = _snapshot(conversation)
    authority.crash_to_durable_point()
    assert _snapshot(conversation)['syncSeq'] < seen['syncSeq']

    frames = _resume(monkeypatch, conversation, after=seen['cursor'])

    assert _events(frames) == ['sync.reset_required']
    assert frames[0]['data']['reason'] == 'cursor_invalid'


def test_an_ordinary_reconnect_resumes_without_a_reset_across_a_clean_restart(
        authority, conversation, monkeypatch):
    _commit(conversation, 'one')
    first = _snapshot(conversation)
    _commit(conversation, 'two')
    replayed = _resume(monkeypatch, conversation, after=first['cursor'], cursor_source='snapshot')
    head = _snapshot(conversation)['syncSeq']
    assert _sequences(replayed) == list(range(first['syncSeq'] + 1, head + 1))
    assert _events(replayed)[-1] == 'sync.heartbeat'
    last_event_id = replayed[-1]['id']

    authority.restart()
    _commit(conversation, 'three')
    resumed = _resume(monkeypatch, conversation, after=first['cursor'], last_event_id=last_event_id)
    latest = _snapshot(conversation)['syncSeq']
    assert 'sync.reset_required' not in _events(resumed)
    assert _sequences(resumed) == list(range(head + 1, latest + 1))

    from_snapshot = _resume(monkeypatch, conversation, after=first['cursor'])
    assert 'sync.reset_required' not in _events(from_snapshot)
    assert _sequences(from_snapshot) == list(range(first['syncSeq'] + 1, latest + 1))


def _cursor_issued_before_lineage(conversation_id: str, sequence: int) -> str:
    """The exact pre-lineage cursor a browser that loaded before the deploy holds."""
    scope = hashlib.sha256(f'{OWNER}:{conversation_id}'.encode()).hexdigest()[:16]
    digits, value = '', sequence
    while True:
        value, remainder = divmod(value, 36)
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'[remainder] + digits
        if not value:
            break
    return f'c1.{scope}.{digits}'


def test_a_cursor_issued_before_the_deploy_resets_exactly_once(
        authority, conversation, monkeypatch):
    _commit(conversation, 'before-deploy')
    held = _cursor_issued_before_lineage(conversation, _snapshot(conversation)['syncSeq'])
    authority.restart()

    first = _resume(monkeypatch, conversation, last_event_id=held)
    assert _events(first) == ['sync.reset_required']
    assert first[0]['data']['reason'] == 'protocol_mismatch'

    recovered = _snapshot(conversation)
    resumed = _resume(monkeypatch, conversation, after=recovered['cursor'], cursor_source='snapshot')
    assert _events(resumed) == ['sync.heartbeat']
    assert resumed[0]['data']['cursor'] == resumed[0]['id'] == recovered['cursor']
    reconnected = _resume(monkeypatch, conversation, after=recovered['cursor'],
                          last_event_id=resumed[0]['id'])
    assert _events(reconnected) == ['sync.heartbeat']
