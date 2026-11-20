#!/usr/bin/env python3
"""Project and check the complete first-party HTTP/WebSocket route index.

Authority: contracts/api_v1.yaml. The index file is rendered from its
``x-tofu-route`` records by ``tofu-xtask contracts``; this script checks
that projection against production routes.register_all plus per-handler
@api_meta and declared blueprint response contracts (``generate``), so a route
registered in code but absent from the contract, or the reverse, fails.
Entry points: route_app, generate, the check-only CLI. Assembly starts no
storage, workers or optional plugin discovery; the index describes the
released mount, including factory routes.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'contracts/api_endpoint_index_v1.json'
# Rendered into the file by tofu-xtask (packages/tofu-xtask/src/api_v1.rs INDEX_COMMENT).
INDEX_COMMENT = ('AUTO-GENERATED from contracts/api_v1.yaml (x-tofu-route records) by '
                 'tofu-xtask contracts (packages/tofu-xtask); checked against production route '
                 'assembly and handler metadata by scripts/gen_api_endpoint_index.py. DO NOT EDIT.')


def route_app():
    from lib.api_v4 import configure_api_version_policy
    from lib.generated_api_v4_constants import API_MAJOR
    from lib.app.app_factory import create_base_app
    from routes import register_all

    app = create_base_app('api-endpoint-index', {
        'ACTIVE_API_MAJOR': API_MAJOR, 'API_V4_ENABLED': True,
    })
    configure_api_version_policy(app)
    register_all(app, discover_plugins=False)
    return app


def generate(app=None) -> dict:
    from lib.api_v4 import NativeContractBlueprint
    from lib.generated_api_v4_constants import API_MAJOR

    app = route_app() if app is None else app
    endpoints = []
    unresolved = []
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            raise ValueError(f'Registered rule has no view: {rule.endpoint}')
        handler = inspect.unwrap(view)
        try:
            filename = inspect.getsourcefile(handler)
        except TypeError:
            filename = None
        if filename is None or not Path(filename).is_relative_to(ROOT):
            unresolved.append({'path': rule.rule, 'endpoint': rule.endpoint})
            continue
        blueprint = app.blueprints.get(rule.endpoint.rsplit('.', 1)[0])
        meta = getattr(view, '_api_meta', None) or {}
        contract_id = (blueprint.native_contract_id
                       if isinstance(blueprint, NativeContractBlueprint)
                       else 'tofu.api/v4' if getattr(blueprint, 'tofu_v4_success_envelope', False)
                       else None)
        for method in sorted(set(rule.methods or ()) - {'HEAD', 'OPTIONS'}):
            endpoints.append({
                'method': method, 'path': rule.rule,
                'transport': 'websocket' if rule.websocket else 'http',
                'responseContract': contract_id,
                **({'rawSuccess': True} if getattr(view, 'tofu_raw_success_response', False) else {}),
                'summary': meta.get('summary', ''), 'tags': meta.get('tags', []),
                'scope': meta.get('scope'), 'public': meta.get('public'),
                'requiredScopes': getattr(view, '_required_scopes', None),
                'documented': bool(meta),
                'owner': str(Path(filename).relative_to(ROOT)),
                'handler': handler.__qualname__,
            })
    endpoints.sort(key=lambda item: (item['path'], item['method'], item['transport']))
    unresolved.sort(key=lambda item: (item['path'], item['endpoint']))
    return {
        '$comment': INDEX_COMMENT,
        'contract': 'tofu.api-endpoint-index/v1', 'apiMajor': API_MAJOR,
        'scope': 'first-party; optional plugin routes are owned by their packages',
        'endpoints': endpoints, 'unresolved': unresolved,
    }


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split('\n', 1)[0]).parse_args()
    document = generate()
    if document['unresolved']:
        raise ValueError(f'Unresolved route owners: {document["unresolved"]}')
    if json.loads(OUTPUT.read_text(encoding='utf-8')) != document:
        print('Registered routes differ from contracts/api_endpoint_index_v1.json; run '
              'python3 scripts/gen_api_v1_contract.py for the difference to '
              'contracts/api_v1.yaml, then make contracts', file=sys.stderr)
        return 1
    print(f'API endpoint index matches the registered routes: {len(document["endpoints"])} operations')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
