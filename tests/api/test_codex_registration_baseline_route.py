"""Codex registration consent binds the acting owner and revision."""
from types import SimpleNamespace

import pytest

from lib.model_routing import ModelRoutingError, OwnerBoundary, empty_document
from lib.oauth import codex_baseline
from routes.api_v1 import providers


pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
PATH = '/api/v4/model-routing'


@pytest.fixture
def baseline_command(monkeypatch):
    repo = object()
    boundary = OwnerBoundary.create(81234, 'codex-test-tenant')
    calls = []

    def execute(enabled, repository, owner, expected_revision):
        calls.append((enabled, repository, owner, expected_revision))
        if expected_revision != 4:
            raise ModelRoutingError('model-routing revision changed', kind='model_routing_revision_conflict')
        return SimpleNamespace(revision=5, public_document=empty_document)

    def establish(repository, owner, *, expected_revision):
        return execute(True, repository, owner, expected_revision)

    def disable(repository, owner, *, expected_revision):
        return execute(False, repository, owner, expected_revision)

    monkeypatch.setattr(providers, '_repository', lambda: repo)
    monkeypatch.setattr(providers, '_boundary', lambda: boundary)
    monkeypatch.setattr(codex_baseline, 'establish_owner_codex_baseline', establish, raising=False)
    monkeypatch.setattr(codex_baseline, 'disable_owner_codex_registration', disable, raising=False)
    return repo, boundary, calls


@pytest.mark.parametrize('enabled', [True, False])
def test_command_uses_authenticated_owner_and_expected_revision(flask_client, baseline_command, enabled):
    repo, boundary, calls = baseline_command
    response = flask_client.put(PATH, json={'expected_revision': 4, 'codex_registration_enabled': enabled})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['revision'] == 5
    assert calls == [(enabled, repo, boundary, 4)]


@pytest.mark.parametrize('extra', [{}, {'codex_registration_enabled': 1},
                                   {'codex_registration_enabled': True, 'account_fingerprint': 'a' * 64},
                                   {'codex_registration_enabled': True, 'owner_user_id': 42},
                                   {'codex_registration_enabled': True, 'model_routing': {}}])
def test_invalid_payload_does_not_call_service(flask_client, baseline_command, extra):
    assert flask_client.put(PATH, json={'expected_revision': 4, **extra}).status_code == 400
    assert not baseline_command[2]


def test_conflict_is_not_retried(flask_client, baseline_command):
    repo, boundary, calls = baseline_command
    assert flask_client.put(PATH, json={'expected_revision': 3, 'codex_registration_enabled': True}).status_code == 409
    assert calls == [(True, repo, boundary, 3)]
