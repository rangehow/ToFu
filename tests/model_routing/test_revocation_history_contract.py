"""Revocation records survive absent live model-routing entities."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.model_routing import ModelRoutingError, empty_document, normalize_document

pytestmark = pytest.mark.unit


def test_history_normalizes_independently_of_live_entities():
    doc = empty_document()
    doc['revocation_history'] = [{
        'credential_id': ' credential ', 'creator_id': 'creator', 'model_id': 'model',
        'model_revoked': True, 'excluded_wire_model_ids': ['wire-z', 'wire-a'],
    }]
    normalized = normalize_document(doc)
    assert normalized['revocation_history'][0] == {
        'credential_id': 'credential', 'creator_id': 'creator', 'model_id': 'model',
        'model_revoked': True, 'excluded_wire_model_ids': ['wire-a', 'wire-z'],
    }
    assert normalize_document(normalized) == normalized
    schema = json.loads((Path(__file__).parents[2] / 'contracts/model_routing_v2.schema.json').read_text())
    Draft202012Validator(schema).validate(normalized)


def test_history_rejects_duplicate_keys_and_invalid_entries():
    doc = empty_document()
    entry = {'credential_id': 'c', 'creator_id': 'a', 'model_id': 'm',
             'model_revoked': True, 'excluded_wire_model_ids': []}
    doc['revocation_history'] = [entry, copy.deepcopy(entry)]
    with pytest.raises(ModelRoutingError):
        normalize_document(doc)
    doc['revocation_history'] = [{**entry, 'model_revoked': 1}]
    with pytest.raises(ModelRoutingError):
        normalize_document(doc)
    doc['revocation_history'] = [{**entry, 'excluded_wire_model_ids': ['w', 'w']}]
    with pytest.raises(ModelRoutingError):
        normalize_document(doc)
