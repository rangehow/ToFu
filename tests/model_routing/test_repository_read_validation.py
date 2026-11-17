"""A stored aggregate is walked once per exact content, never trusted by revision.

``ModelRoutingRepository.get`` reads and decodes its owner's revision on every
call (lib/model_routing/AGENTS.md); only a document byte-identical to one that
normalization already returned unchanged skips the walk.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

from lib.model_routing import repository as repository_module
from lib.model_routing.domain import ModelRoutingError, normalize_document
from lib.model_routing.repository import ModelRoutingRepository, OwnerBoundary

CAPABILITIES = ['text', 'tools']


def _document(models: int, *, revision: int = 3) -> dict:
    document = {
        'contract_version': 'tofu.model-routing/v2', 'revision': revision,
        'creators': [{'creator_id': 'anthropic', 'name': 'Anthropic'}], 'models': [],
        'providers': [{'provider_id': 'p', 'name': 'P', 'scope': 'owner'}],
        'provider_accesses': [{'provider_access_id': 'pa', 'provider_id': 'p', 'enabled': True,
                               'quota_policy': {}}],
        'connections': [{'connection_id': 'c', 'provider_access_id': 'pa', 'base_url': 'http://127.0.0.1:1',
                         'protocol': 'anthropic', 'enabled': True, 'priority': 0, 'extra_headers': {}}],
        'credentials': [], 'offerings': [], 'deployments': [],
    }
    for index in range(models):
        ref = {'creator_id': 'anthropic', 'model_id': f'claude-probe-{index}'}
        document['models'].append({
            **ref, 'display_name': f'Probe {index}', 'capabilities': CAPABILITIES,
            'context_window': 200_000, 'quality_rank': 1,
            'registration_metadata': {'release_date': '2026-11-12', 'checked_at': '2026-11-12',
                                      'source_urls': ['https://example.invalid/probe']},
            'list_pricing': {'input': 1, 'output': 2, 'currency': 'USD', 'unit': 'per_million_tokens'}})
        document['offerings'].append({
            'offering_id': f'o{index}', 'provider_access_id': 'pa', 'identity_state': 'confirmed',
            'model': dict(ref), 'enabled': True, 'capabilities': CAPABILITIES,
            'context_window': 200_000, 'priority': 0})
        document['deployments'].append({
            'deployment_id': f'd{index}', 'offering_id': f'o{index}', 'connection_id': 'c',
            'wire_model_id': ref['model_id'], 'enabled': True, 'identity_confidence': 'high',
            'probe_status': 'passed', 'priority': 0})
    return document


class _StoredRow:
    """A storage client whose every read decodes the stored JSON afresh, as the RPC does."""

    def __init__(self, document: dict):
        self.encoded = json.dumps({'revision': document['revision'], 'document': document,
                                   'updated_at': 1.0})

    def query(self, operation, payload):
        assert operation == 'model_routing.get'
        return json.loads(self.encoded)


@pytest.fixture
def walks(monkeypatch):
    monkeypatch.setattr(repository_module, '_CANONICAL_STORED_DIGESTS', {})
    calls = []

    def counted(raw, **kwargs):
        calls.append(raw)
        return normalize_document(raw, **kwargs)

    monkeypatch.setattr(repository_module, 'normalize_document', counted)
    return calls


def _repository(stored: _StoredRow) -> ModelRoutingRepository:
    repository = ModelRoutingRepository()
    repository._client = lambda write=False: stored
    return repository


def test_unchanged_canonical_document_is_walked_once_and_each_read_is_fresh(walks):
    canonical = normalize_document(_document(3))
    repository = _repository(_StoredRow(canonical))
    boundary = OwnerBoundary.create(1)

    first, second = repository.get(boundary), repository.get(boundary)

    assert len(walks) == 1
    assert first.document == second.document == canonical
    assert second.normalized and second.revision == 3
    assert first.document is not second.document
    second.document['models'].clear()
    assert repository.get(boundary).document == canonical


def test_changed_content_and_other_owners_are_walked_again(walks):
    stored = _StoredRow(normalize_document(_document(2)))
    repository = _repository(stored)
    owner, other = OwnerBoundary.create(1), OwnerBoundary.create(2)
    repository.get(owner)
    repository.get(other)
    assert len(walks) == 2

    stored.encoded = _StoredRow(normalize_document(_document(3, revision=4))).encoded
    assert repository.get(owner).revision == 4
    assert len(walks) == 3


def test_a_document_normalization_would_change_is_never_admitted_unwalked(walks):
    # quality_rank 1 == 1.0, but the canonical form is the float.
    raw = normalize_document(_document(2))
    for model in raw['models']:
        model['quality_rank'] = 1
    assert raw == normalize_document(raw)
    repository = _repository(_StoredRow(raw))
    boundary = OwnerBoundary.create(1)

    reads = [repository.get(boundary) for _ in range(3)]

    assert len(walks) == 3
    assert all(isinstance(model['quality_rank'], float)
               for read in reads for model in read.document['models'])


def test_invalid_stored_content_fails_on_every_read(walks):
    canonical = normalize_document(_document(2))
    stored = _StoredRow(canonical)
    repository = _repository(stored)
    boundary = OwnerBoundary.create(1)
    repository.get(boundary)

    broken = json.loads(stored.encoded)
    broken['document']['offerings'][0]['model']['model_id'] = 'missing'
    stored.encoded = json.dumps(broken)
    for _ in range(2):
        with pytest.raises(ModelRoutingError):
            repository.get(boundary)
    assert len(walks) == 3
