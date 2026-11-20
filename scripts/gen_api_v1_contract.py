#!/usr/bin/env python3
"""Check the registered routes against contracts/api_v1.yaml.

Authority: ``contracts/api_v1.yaml`` (OpenAPI 3.1; Rust rewrite Phase 0, see
docs/RUST_REWRITE_EVIDENCE.md "Contracts as the single source of Rust types").
It was extracted from the Python owners (production ``routes.register_all``,
per-handler ``@api_meta``, blueprint response contracts and the native
Conversation Sync / v4 documents). Change the contract first, then the route.

The ``/v1`` is the contract-document version, independent of the mounted API
major (``x-tofu-api-major``), like ``api_endpoint_index_v1.json``.

Every operation carries ``x-tofu-route``: the framework rule, transport, owner
module, handler and access metadata of the Python handler that serves it.
Registered routes outside the published OpenAPI surface (static assets, the
documentation UI, the spec itself) are listed under ``x-tofu-unpublished-routes``
with the same record, so the contract covers every registered operation.

The script fails when a registered route/method exists in code but not in the
contract, or vice versa, or its record or OpenAPI operation differs; the
message prints the exact contract fragment the code implies. It writes
nothing: ``contracts/api_endpoint_index_v1.json`` and
``packages/tofu-contracts/src/generated_api_v1.rs`` are rendered from the
contract by ``tofu-xtask contracts`` (packages/tofu-xtask; ``make contracts`` /
``make contracts-check``).

Usage:
    python3 scripts/gen_api_v1_contract.py   # routes == contract, else exit 1
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT = ROOT / 'contracts' / 'api_v1.yaml'
CONTRACT_ID = 'tofu.api-contract/v1'
ROUTE_KEY = 'x-tofu-route'
UNPUBLISHED_KEY = 'x-tofu-unpublished-routes'
_METHODS = ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace')


# ── YAML I/O ────────────────────────────────────────────────────────────

def _yaml():
    import yaml
    return yaml


def load_contract() -> dict:
    yaml = _yaml()
    with CONTRACT.open(encoding='utf-8') as handle:
        document = yaml.load(handle, Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
    if document.get('x-tofu-contract') != CONTRACT_ID:
        raise ValueError(f'{CONTRACT} is not {CONTRACT_ID}')
    return document


def dump_contract(document: dict) -> str:
    yaml = _yaml()
    header = (
        '# Authoritative HTTP and WebSocket contract (OpenAPI 3.1). Edit this file\n'
        '# first, then the Python route; scripts/gen_api_v1_contract.py fails on any\n'
        '# route, method, record or operation present on one side only. tofu-xtask\n'
        '# contracts derives contracts/api_endpoint_index_v1.json and\n'
        '# packages/tofu-contracts/src/generated_api_v1.rs from this file.\n'
    )
    body = yaml.dump(document, Dumper=getattr(yaml, 'CSafeDumper', yaml.SafeDumper),
                     sort_keys=False, allow_unicode=True, width=100)
    return header + body


# ── Registry projection (what the Python server implements today) ──────

def _route_records(app) -> list[tuple[str, str, dict]]:
    """(OpenAPI path, method, x-tofu-route) per registered operation, index order."""
    from lib.api_v4 import NativeContractBlueprint
    from lib.openapi._paths import _flask_to_openapi_path
    from lib.openapi._wire import native_contract_path
    from scripts.gen_api_endpoint_index import generate

    index = generate(app)
    if index['unresolved']:
        raise ValueError(f'Unresolved route owners: {index["unresolved"]}')
    by_key = {(item['path'], item['method'], item['transport']): item
              for item in index['endpoints']}
    records = []
    for rule in app.url_map.iter_rules():
        blueprint = app.blueprints.get(rule.endpoint.rsplit('.', 1)[0])
        path = _flask_to_openapi_path(str(rule.rule))
        if isinstance(blueprint, NativeContractBlueprint):
            path = native_contract_path(blueprint.native_contract_id, path)
        transport = 'websocket' if rule.websocket else 'http'
        for method in sorted(set(rule.methods or ()) - {'HEAD', 'OPTIONS'}):
            entry = dict(by_key.pop((rule.rule, method, transport)))
            record = {'rule': entry.pop('path'), **{key: value for key, value in entry.items()
                                                    if key != 'method'}}
            records.append((path, method.lower(), record))
    if by_key:
        raise ValueError(f'Index entries without a registered rule: {sorted(by_key)}')
    return records


def registry_document(app=None) -> dict:
    """The contract as the production route assembly implements it."""
    from lib.generated_api_v4_constants import API_MAJOR
    from lib.openapi import build_spec
    from lib.openapi._paths import _skip_rule
    from scripts.gen_api_endpoint_index import route_app

    app = route_app() if app is None else app
    spec = build_spec(app)
    unpublished: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for path, method, record in _route_records(app):
        if (path, method) in seen:
            raise ValueError(f'Two registered rules publish {method.upper()} {path}')
        seen.add((path, method))
        if _skip_rule(path):
            unpublished.append({'method': method.upper(), **record})
            continue
        spec['paths'][path][method][ROUTE_KEY] = record
    missing = [(path, method) for path, item in spec['paths'].items()
               for method, operation in item.items() if ROUTE_KEY not in operation]
    if missing:
        raise ValueError(f'Published operations without a registered rule: {missing}')
    unpublished.sort(key=lambda item: (item['rule'], item['method'], item['transport']))
    return {
        'openapi': spec['openapi'],
        'x-tofu-contract': CONTRACT_ID,
        'x-tofu-api-major': API_MAJOR,
        'x-tofu-scope': 'first-party; optional plugin routes are owned by their packages',
        **{key: value for key, value in spec.items() if key != 'openapi'},
        UNPUBLISHED_KEY: unpublished,
    }


def _operations(document: dict) -> dict[tuple[str, str], dict]:
    return {(path, method): operation
            for path, item in document.get('paths', {}).items()
            for method, operation in item.items() if method in _METHODS}


def registry_drift(contract: dict, registry: dict | None = None) -> list[str]:
    """Differences between contract and code; empty when they agree exactly."""
    registry = registry_document() if registry is None else registry
    if contract == registry:
        return []
    yaml = _yaml()

    def fragment(value) -> str:
        text = yaml.dump(value, Dumper=getattr(yaml, 'CSafeDumper', yaml.SafeDumper),
                         sort_keys=False, allow_unicode=True, width=100)
        return '      ' + text.rstrip('\n').replace('\n', '\n      ')

    problems: list[str] = []
    in_contract, in_code = _operations(contract), _operations(registry)
    for path, method in sorted(in_code.keys() - in_contract.keys()):
        problems.append(f'{method.upper()} {path}: registered in code but missing from '
                        f'contracts/api_v1.yaml; the code implies paths[{path!r}].{method}:\n'
                        + fragment(in_code[(path, method)]))
    for path, method in sorted(in_contract.keys() - in_code.keys()):
        problems.append(f'{method.upper()} {path}: in contracts/api_v1.yaml but no '
                        'registered route serves it')
    for key in sorted(in_code.keys() & in_contract.keys()):
        if in_code[key] != in_contract[key]:
            path, method = key
            problems.append(f'{method.upper()} {path}: contract differs from the code, which '
                            f'implies paths[{path!r}].{method}:\n' + fragment(in_code[key]))
    contract_unpublished = {(item['method'], item['rule'], item['transport']): item
                            for item in contract.get(UNPUBLISHED_KEY, [])}
    code_unpublished = {(item['method'], item['rule'], item['transport']): item
                        for item in registry[UNPUBLISHED_KEY]}
    if contract_unpublished != code_unpublished:
        problems.append(f'{UNPUBLISHED_KEY} differs; the code implies:\n'
                        + fragment(registry[UNPUBLISHED_KEY]))
    for key in registry:
        if key not in ('paths', UNPUBLISHED_KEY) and contract.get(key) != registry[key]:
            problems.append(f'top-level {key!r} differs; the code implies:\n'
                            + fragment(registry[key]))
    for key in contract.keys() - registry.keys():
        problems.append(f'top-level {key!r} is not produced by the code')
    for path in contract.get('paths', {}).keys() - registry['paths'].keys():
        if not any(method in _METHODS for method in contract['paths'][path]):
            problems.append(f'path {path!r} has no operations')
    return problems


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split('\n', 1)[0]).parse_args()
    problems = registry_drift(load_contract())
    if problems:
        print('HTTP contract drift between contracts/api_v1.yaml and the registered routes:',
              file=sys.stderr)
        for problem in problems:
            print(f'  - {problem}', file=sys.stderr)
        return 1
    print('registered routes match contracts/api_v1.yaml')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

