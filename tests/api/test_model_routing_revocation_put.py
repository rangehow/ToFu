"""Explicit aggregate PUT records only CAS-protected owner grant changes."""
import copy
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


def grant(model, *excluded):
    return {'creator_id': 'creator', 'model_id': model,
            'excluded_wire_model_ids': list(excluded)}


def credential(cid, *models):
    return {'credential_id': cid, 'authorization': {'models': list(models)}}


def entry(cid, model, revoked=False, wires=()):
    return {'credential_id': cid, 'creator_id': 'creator', 'model_id': model,
            'model_revoked': revoked, 'excluded_wire_model_ids': list(wires)}


class Repository:
    def __init__(self, document):
        self.document = document
        self.revision = 5
        self.calls = 0
        self.conflict = False

    def get(self, boundary):
        return SimpleNamespace(revision=self.revision, document=copy.deepcopy(self.document))

    def compare_and_swap(self, boundary, document, *, expected_revision):
        from lib.model_routing import ModelRoutingError
        self.calls += 1
        if self.conflict:
            raise ModelRoutingError('changed', kind='model_routing_revision_conflict')
        assert expected_revision == self.revision
        self.document = copy.deepcopy(document)
        self.revision += 1
        return SimpleNamespace(revision=self.revision, document=self.document, public_document=lambda: self.document)


@pytest.fixture
def setup_route(monkeypatch):
    import routes.api_v1.providers as providers
    import lib.model_routing.registration as registration
    repo = Repository({'credentials': [credential('a', grant('one'), grant('two', 'old')),
                                        credential('b', grant('other'))],
                       'revocation_history': [entry('b', 'historic', True)]})
    monkeypatch.setattr(providers, '_repository', lambda: repo)
    monkeypatch.setattr(providers, '_contract_rejection', lambda *_args: None)
    monkeypatch.setattr(providers, '_hot_reload_dispatcher_slots', lambda: None)
    monkeypatch.setattr(registration, 'prepare_registration_document',
                        lambda document, previous: copy.deepcopy(document))
    return repo


def put(client, repo, candidate, revision=None):
    return client.put('/api/v4/model-routing', json={
        'expected_revision': repo.revision if revision is None else revision,
        'model_routing': candidate})


def test_removal_exclusions_tampering_and_regrant(flask_client, setup_route):
    repo = setup_route
    candidate = copy.deepcopy(repo.document)
    candidate['revocation_history'] = [entry('attacker', 'forged', True)]
    candidate['credentials'][0]['authorization']['models'] = [grant('two', 'old', 'new')]
    response = put(flask_client, repo, candidate)
    assert response.status_code == 200
    assert repo.document['revocation_history'] == [
        entry('a', 'one', True), entry('a', 'two', wires=['new']),
        entry('b', 'historic', True)]
    candidate = copy.deepcopy(repo.document)
    candidate['revocation_history'] = []
    candidate['credentials'][0]['authorization']['models'] = [grant('one'), grant('two', 'old')]
    assert put(flask_client, repo, candidate).status_code == 200
    assert repo.document['revocation_history'] == [entry('b', 'historic', True)]


def test_stale_revision_and_racing_cas_do_not_commit(flask_client, setup_route):
    repo = setup_route
    candidate = copy.deepcopy(repo.document)
    candidate['credentials'][0]['authorization']['models'] = []
    before = copy.deepcopy(repo.document)
    assert put(flask_client, repo, candidate, revision=4).status_code == 409
    assert repo.calls == 0
    repo.conflict = True
    assert put(flask_client, repo, candidate).status_code == 409
    assert repo.calls == 1
    assert repo.document == before


def test_real_admission_and_repository_cas(flask_client, monkeypatch):
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from tests.model_routing.test_model_routing_contract import _full_document
    import routes.api_v1.providers as providers

    repo = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(81234)
    repo.compare_and_swap(boundary, _full_document(), expected_revision=0)
    monkeypatch.setattr(providers, '_repository', lambda: repo)
    monkeypatch.setattr(providers, '_boundary', lambda: boundary)
    monkeypatch.setattr(providers, '_hot_reload_dispatcher_slots', lambda: None)
    current = repo.get(boundary)
    candidate = current.public_document()
    candidate['credentials'][0]['authorization']['models'].pop(0)
    candidate['revocation_history'] = [entry('forged', 'fake', True)]
    response = flask_client.put('/api/v4/model-routing', json={
        'expected_revision': current.revision, 'model_routing': candidate})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert repo.get(boundary).document['revocation_history'] == [
        {'credential_id': 'credential-a', 'creator_id': 'creator',
         'model_id': 'alpha', 'model_revoked': True, 'excluded_wire_model_ids': []}]


def test_seen_history_is_not_client_authored(flask_client, setup_route):
    repo = setup_route
    history = [{'provider_access_id': 'absent', 'credential_id': 'absent',
                'account_fingerprint': 'a' * 64, 'complete': False,
                'wire_model_ids': ['old'], 'models': [], 'pending_wire_model_ids': []}]
    repo.document['codex_seen_history'] = copy.deepcopy(history)
    candidate = copy.deepcopy(repo.document)
    candidate['codex_seen_history'] = [{'forged': True}]
    assert put(flask_client, repo, candidate).status_code == 200
    assert repo.document['codex_seen_history'] == history


@pytest.mark.parametrize('operation', ['unchanged', 'add', 'remove', 'remove_model', 'remove_credential', 'regrant'])
def test_put_patch_revocation_parity(flask_client, setup_route, monkeypatch, operation):
    import routes.api_v1.providers as providers

    repo = setup_route
    repo.document['revocation_history'] = [entry('a', 'two', wires=['historic', 'old'])]
    if operation == 'regrant':
        repo.document['credentials'][0]['authorization']['models'] = [grant('one')]
        repo.document['revocation_history'] = [entry('a', 'two', True, ['historic'])]
    original = copy.deepcopy(repo.document)
    candidate = copy.deepcopy(original)
    models = candidate['credentials'][0]['authorization']['models']
    if operation == 'add':
        models[1] = grant('two', 'old', 'new')
    elif operation == 'remove':
        models[1] = grant('two')
    elif operation == 'remove_model':
        models.pop(1)
    elif operation == 'remove_credential':
        candidate['credentials'].pop(0)
    elif operation == 'regrant':
        models.append(grant('two'))
    candidate['revocation_history'] = [entry('forged', 'fake', True)]
    assert put(flask_client, repo, candidate).status_code == 200
    expected = copy.deepcopy(repo.document['revocation_history'])
    repo.document = original
    repo.revision = 5
    monkeypatch.setattr(providers, '_provider_bundle', lambda document, provider_id: {})
    monkeypatch.setattr(providers, 'remove_provider_resources_in_place', lambda document, provider_id: [])
    def apply_bundle(repository, boundary, document, body, **kwargs):
        document['credentials'] = copy.deepcopy(candidate['credentials'])
        document['revocation_history'] = copy.deepcopy(candidate['revocation_history'])
        return 'provider'
    monkeypatch.setattr(providers, '_apply_bundle', apply_bundle)
    response = flask_client.patch('/api/v4/providers/provider', json={'expected_revision': 5})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert repo.document['revocation_history'] == expected
    if operation == 'remove_model' or operation == 'remove_credential':
        assert entry('a', 'two', True, ['historic', 'old']) in expected
    if operation == 'add':
        assert entry('a', 'two', wires=['historic', 'new', 'old']) in expected
    if operation == 'remove':
        assert entry('a', 'two', wires=['historic']) in expected
    if operation == 'regrant':
        assert not expected
