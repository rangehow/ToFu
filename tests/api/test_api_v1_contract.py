"""contracts/api_v1.yaml is the HTTP authority; routes and the index cannot drift from it."""
from __future__ import annotations

import copy
import json

import pytest

from scripts import gen_api_v1_contract as contract_gen
from scripts.gen_api_endpoint_index import OUTPUT as INDEX_OUTPUT, route_app

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def app():
    return route_app()


@pytest.fixture(scope='module')
def contract():
    return contract_gen.load_contract()


@pytest.fixture(scope='module')
def registry(app):
    return contract_gen.registry_document(app)


def test_registered_routes_match_the_contract_exactly(contract, registry):
    assert contract_gen.registry_drift(contract, registry) == []
    assert contract == registry


def test_contract_covers_every_registered_operation(app, contract):
    registered = sorted(
        (method, rule.rule, 'websocket' if rule.websocket else 'http')
        for rule in app.url_map.iter_rules()
        for method in set(rule.methods or ()) - {'HEAD', 'OPTIONS'})
    declared = sorted(_contract_operations(contract))
    assert declared == registered
    assert len(declared) == len(set(declared))


def _contract_operations(contract):
    for item in contract['paths'].values():
        for method, operation in item.items():
            if 'x-tofu-route' in operation:
                record = operation['x-tofu-route']
                yield method.upper(), record['rule'], record['transport']
    for record in contract['x-tofu-unpublished-routes']:
        yield record['method'], record['rule'], record['transport']


def test_endpoint_index_lists_the_contract_operations(contract):
    # Byte freshness of the rendered index is tofu-xtask's check (make contracts-check).
    index = json.loads(INDEX_OUTPUT.read_text())
    assert sorted((e['method'], e['path'], e['transport']) for e in index['endpoints']) \
        == sorted(_contract_operations(contract))


def test_served_openapi_document_is_the_contract_without_route_records(app, contract):
    from lib.openapi import build_spec

    published = copy.deepcopy(contract)
    for item in published['paths'].values():
        for operation in item.values():
            operation.pop(contract_gen.ROUTE_KEY)
    for key in ('x-tofu-contract', 'x-tofu-api-major', 'x-tofu-scope',
                contract_gen.UNPUBLISHED_KEY):
        published.pop(key)
    assert published == build_spec(app)


def test_route_in_code_but_not_in_contract_fails(contract, registry):
    trimmed = copy.deepcopy(contract)
    path = '/api/v4/keys'
    operation = trimmed['paths'][path].pop('get')
    problems = contract_gen.registry_drift(trimmed, registry)
    assert len(problems) == 1
    assert problems[0].startswith(f'GET {path}: registered in code but missing')
    # The message carries the exact fragment to restore.
    assert operation['operationId'] in problems[0]


def test_route_in_contract_but_not_in_code_fails(contract, registry):
    extended = copy.deepcopy(contract)
    extended['paths']['/api/v4/never-registered'] = {
        'get': copy.deepcopy(contract['paths']['/api/v4/keys']['get'])}
    assert contract_gen.registry_drift(extended, registry) == [
        'GET /api/v4/never-registered: in contracts/api_v1.yaml but no registered route serves it']


def test_changed_route_record_and_unpublished_route_fail(contract, registry):
    changed = copy.deepcopy(contract)
    changed['paths']['/api/v4/keys']['get'][contract_gen.ROUTE_KEY]['scope'] = 'chat'
    changed[contract_gen.UNPUBLISHED_KEY].pop()
    problems = contract_gen.registry_drift(changed, registry)
    assert any(problem.startswith('GET /api/v4/keys: contract differs') for problem in problems)
    assert any(problem.startswith(contract_gen.UNPUBLISHED_KEY) for problem in problems)


def test_new_handler_without_contract_entry_is_reported():
    from quart import Blueprint
    from lib.app.app_factory import create_base_app

    app = create_base_app('api-v1-contract-drift')
    blueprint = Blueprint('contract_drift_example', __name__)

    @blueprint.get('/api/v4/contract-drift-example')
    async def example():
        return 'example'

    app.register_blueprint(blueprint)
    registry = contract_gen.registry_document(app)
    empty = {**copy.deepcopy(registry), 'paths': {}}
    problems = contract_gen.registry_drift(empty, registry)
    assert any(problem.startswith('GET /api/v4/contract-drift-example: registered in code')
               for problem in problems)


def test_index_check_cli_refuses_index_drift(monkeypatch, capsys, tmp_path):
    from scripts import gen_api_endpoint_index

    index = json.loads(INDEX_OUTPUT.read_text())
    removed = index['endpoints'].pop(0)
    trimmed = tmp_path / 'api_endpoint_index_v1.json'
    trimmed.write_text(json.dumps(index))
    monkeypatch.setattr(gen_api_endpoint_index, 'OUTPUT', trimmed)
    monkeypatch.setattr('sys.argv', ['gen_api_endpoint_index.py'])
    assert gen_api_endpoint_index.main() == 1
    assert 'differ from contracts/api_endpoint_index_v1.json' in capsys.readouterr().err
    assert removed in json.loads(INDEX_OUTPUT.read_text())['endpoints']
