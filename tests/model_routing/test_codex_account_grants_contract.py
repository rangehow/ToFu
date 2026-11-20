"""Schema and admission contract for exact account-bound Codex wire grants."""
import copy
import json
from pathlib import Path

import jsonschema
import pytest

from lib.model_routing import ModelRoutingError, normalize_document, plan_legacy_migration
from lib.oauth.outbound import _managed_oauth_entry

pytestmark = pytest.mark.unit


def _document(monkeypatch):
    monkeypatch.setattr('lib.oauth.codex_catalog.cached_codex_provider_models', lambda: [])
    doc = plan_legacy_migration({'providers': [_managed_oauth_entry('codex', 'free')]}).document
    return normalize_document(doc)


def _scoped(doc):
    return doc['credentials'][0]['authorization']


def test_exact_account_grants_survive_normalization(monkeypatch):
    doc = _document(monkeypatch)
    expected = {'a' * 64: [{'creator_id': 'openai', 'model_id': 'gpt-5.5', 'wire_model_id': 'codex-wire'}]}
    _scoped(doc)['codex_account_grants'] = expected
    actual = normalize_document(doc)
    assert _scoped(actual)['codex_account_grants'] == expected
    assert _scoped(actual)['models'] == _scoped(doc)['models']
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/model_routing_v2.schema.json').read_text())
    jsonschema.validate(actual, schema)


@pytest.mark.parametrize('invalid', ['mixed_case', 'wrong_kind', 'wrong_provider', 'duplicate_wire', 'other_creator', 'no_wire'])
def test_invalid_account_grants_fail_closed(monkeypatch, invalid):
    doc = _document(monkeypatch)
    grant = {'creator_id': 'openai', 'model_id': 'gpt-5.5', 'wire_model_id': 'codex-wire'}
    account = 'A' * 64 if invalid == 'mixed_case' else 'a' * 64
    if invalid == 'wrong_kind':
        doc['credentials'][0]['kind'] = 'api_key'
    if invalid == 'wrong_provider':
        doc['providers'][0]['provider_id'] = doc['provider_accesses'][0]['provider_id'] = 'custom-codex'
    if invalid == 'other_creator':
        grant['creator_id'] = 'anthropic'
    if invalid == 'no_wire':
        del grant['wire_model_id']
    grants = [grant, dict(copy.deepcopy(grant), model_id='gpt-6-astra')] if invalid == 'duplicate_wire' else [grant]
    _scoped(doc)['codex_account_grants'] = {account: grants}
    with pytest.raises(ModelRoutingError, match='codex_account_grants'):
        normalize_document(doc)
