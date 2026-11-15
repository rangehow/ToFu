"""Perception receipts admit the same translation recovery as a snapshot read.

Problem this pins (conv mwcuhvl0800rdd, 2026-11-12): a whole-turn translation
failed with ``Upstream rate-limit attempt budget exhausted (8/8)`` and recorded
its ``_translateFailed`` verdict. Open-path recovery
(``schedule_translation_recovery``, see ``test_translate_open_recovery.py``)
was admitted only by ``GET /api/v4/conversations/<id>/sync``, but the tab that
created the conversation and stayed on it follows live events: access.log held
0 snapshot reads and 104 perception receipts for it, so the failed turn was
never retried while the reader kept the conversation open.

Contract under test: a successful ``POST .../turns/<turn>/perception`` admits
recovery for that conversation and the request's owner; the debounce, the
eligibility rules and the sweep stay with the translation owner, off the
request. A failed or rejected receipt admits nothing, and a raising scheduler
never changes the perception response.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

_PATH = '/api/v4/conversations/conv-a/turns/turn-a/perception'
_RECEIPT = {
    'observationId': 'page-a:paint:1',
    'attemptId': 'attempt-a',
    'kind': 'terminal_painted',
    'clientId': 'page-a',
    'phase': 'waiting_model',
    'serverEmittedAt': 1000,
    'receivedAt': 1125,
    'paintedAt': 1160,
    'visibility': 'visible',
}


@pytest.fixture(autouse=True)
def _clear_recovery_debounce():
    import lib.translate.terminal as terminal

    with terminal._recovery_guard:
        terminal._recovery_last_attempt.clear()
    yield
    with terminal._recovery_guard:
        terminal._recovery_last_attempt.clear()


@pytest.fixture
def recorded(monkeypatch):
    from lib.conversation_sync.command_service import CommandOutcome
    from routes import conversation_sync_v3 as sync_routes

    calls = []

    def record_perception(conversation_id, turn_id, user_id, body):
        calls.append((conversation_id, turn_id, user_id, dict(body)))
        return CommandOutcome({'conversationRevision': 12})

    monkeypatch.setattr(
        sync_routes.conversation_turn_commands, 'record_perception',
        record_perception)
    return calls


@pytest.fixture
def admitted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        'lib.translate.terminal.schedule_translation_recovery',
        lambda conversation_id, **kwargs: calls.append(
            (conversation_id, kwargs)) or True)
    return calls


def test_receipt_admits_recovery_for_its_conversation_and_owner(
    flask_client, recorded, admitted,
):
    response = flask_client.post(_PATH, json=_RECEIPT)

    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'conversationRevision': 12}
    assert recorded == [('conv-a', 'turn-a', 1, _RECEIPT)]
    # The same authenticated owner that recorded the receipt, as on /sync.
    assert admitted == [('conv-a', {'user_id': 1})]


def test_raising_scheduler_never_changes_the_perception_response(
    flask_client, recorded, monkeypatch,
):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError('translation runtime unavailable')

    monkeypatch.setattr(
        'lib.translate.terminal.schedule_translation_recovery', unavailable)

    response = flask_client.post(_PATH, json=_RECEIPT)

    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'conversationRevision': 12}
    assert len(recorded) == 1


def test_failed_or_rejected_receipt_admits_no_recovery(
    flask_client, admitted, monkeypatch,
):
    from lib.turn_lifecycle import LifecycleNotFound
    from routes import conversation_sync_v3 as sync_routes

    def missing_turn(*_args, **_kwargs):
        raise LifecycleNotFound('turn not found')

    monkeypatch.setattr(
        sync_routes.conversation_turn_commands, 'record_perception',
        missing_turn)

    assert flask_client.post(_PATH, json=_RECEIPT).status_code == 404
    content_bearing = flask_client.post(
        _PATH, json={**_RECEIPT, 'content': 'must never be captured'})
    assert content_bearing.status_code == 400
    assert admitted == []


def test_recovery_sweep_runs_after_the_receipt_is_answered(
    flask_client, recorded, monkeypatch,
):
    import lib.translate.terminal as terminal

    entered, release, finished = (
        threading.Event(), threading.Event(), threading.Event())

    def blocked_sweep(conversation_id, *, user_id):
        entered.set()
        release.wait(timeout=5)
        finished.set()
        return 0

    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations', blocked_sweep)
    try:
        response = flask_client.post(_PATH, json=_RECEIPT)

        assert response.status_code == 200
        # Answered while the admitted sweep is still blocked: it runs detached.
        assert not finished.is_set()
        assert entered.wait(timeout=10)
    finally:
        release.set()
