"""The owner changes one session's model over HTTP; nothing else about it changes."""
from __future__ import annotations

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def flask_app(chat_sidecar):
    import server
    server.app.config.update(TESTING=True)
    return server.app


def _create_conversation(flask_client, conversation_id='session-target'):
    from lib.storage import get_storage_client
    get_storage_client(write=True).command('conversation.create', {
        'conv_id': conversation_id, 'user_id': 1, 'title': 'Long supervision session',
        'settings': {'projectPath': '/project'},
    }, 'session-model-spec:' + conversation_id)
    return conversation_id


def test_session_model_change_is_route_admitted_audited_and_reversible(flask_client, chat_sidecar,
                                                                       monkeypatch):
    from lib.project_dispatch import delegation
    from lib.conversations import session_model
    admitted = []
    monkeypatch.setattr(delegation, 'admit_worker_model',
                        lambda owner, model, **kwargs: admitted.append(model) or model)
    audited = []
    monkeypatch.setattr(session_model, 'audit_log', lambda event, **fields: audited.append((event, fields)))
    conversation_id = _create_conversation(flask_client)
    endpoint = '/api/v4/router/session-model'

    refused = flask_client.post(endpoint, json={'conversationId': conversation_id, 'model': 'x',
                                                'reason': ''}, headers={'Idempotency-Key': 'bad'})
    assert refused.status_code == 400
    assert flask_client.post(endpoint, json={'conversationId': conversation_id, 'model': 'x',
                                             'reason': 'refusal'}).status_code == 400  # no idempotency key

    chosen = flask_client.post(endpoint, json={'conversationId': conversation_id, 'model': 'model-b',
                                               'reason': 'Anthropic content-filter refusal'},
                               headers={'Idempotency-Key': 'route-1'})
    assert chosen.status_code == 200, chosen.get_json()
    assert chosen.get_json()['model'] == 'model-b'
    assert session_model.session_model(conversation_id, owner=1) == 'model-b'
    assert audited and audited[0][0] == 'session_model_changed'
    assert audited[0][1]['reason'] == 'Anthropic content-filter refusal'

    replay = flask_client.post(endpoint, json={'conversationId': conversation_id, 'model': 'model-b',
                                               'reason': 'Anthropic content-filter refusal'},
                               headers={'Idempotency-Key': 'route-1'})
    assert replay.status_code == 200
    assert admitted == ['model-b']  # the replay did not admit or write again

    cleared = flask_client.post(endpoint, json={'conversationId': conversation_id, 'model': None,
                                                'reason': 'Back to the original model'},
                                headers={'Idempotency-Key': 'route-2'})
    assert cleared.get_json()['model'] == ''
    assert session_model.session_model(conversation_id, owner=1) == ''

    unknown = flask_client.post(endpoint, json={'conversationId': 'no-such-session', 'model': 'model-b',
                                                'reason': 'refusal'}, headers={'Idempotency-Key': 'route-3'})
    assert unknown.status_code == 403
