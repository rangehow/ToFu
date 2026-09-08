"""Ratchet between the API v1 model-routing contract and the live routes.

Guards three seams of contracts/api_v1_model_routing.yaml:

* Route surface: every operation declared in the generated contract exists on
  the api_v1_providers blueprint with the same method and path, and vice
  versa. Adding a route without declaring it (or dropping one) fails here.
* Live conformance: success envelopes produced by the running routes decode
  against the generated response schemas, fail-closed (undeclared fields are
  violations on both sides).
* Request boundary: bodies rejected by the generated request schemas return
  400 before any repository mutation.
"""

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import re
import unittest

from unittest import mock

import pytest

from lib.api_v1_model_routing_generated import OPERATIONS, decode_response
from tests.support.model_routing import (
    clear_test_model_routing,
    native_test_provider_bundle,
)


pytestmark = pytest.mark.unit

OWNER = 12_031


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _flask_path(rule: str) -> str:
    return re.sub(r'<(?:[^:>]+:)?([^>]+)>', r'{\1}', rule)


class RouteSurfaceSyncTest(unittest.TestCase):
    """The blueprint and the contract must describe the same surface."""

    def test_blueprint_matches_contract_operations(self):
        from quart import Quart

        app = Quart(__name__, static_folder=None)
        from routes.api_v1.providers import api_v1_providers_bp
        app.register_blueprint(api_v1_providers_bp)

        live = set()
        for rule in app.url_map.iter_rules():
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                live.add((method, _flask_path(rule.rule)))

        declared = {
            (spec['method'], spec['path']) for spec in OPERATIONS.values()
        }
        self.assertEqual(
            live, declared,
            f'route surface drift: only-live={sorted(live - declared)} '
            f'only-contract={sorted(declared - live)}',
        )

    def test_every_operation_has_a_response_schema_and_success_code(self):
        for operation_id, spec in OPERATIONS.items():
            self.assertTrue(spec['response'], f'{operation_id} has no response')
            self.assertIn(spec['success'], (200, 201), operation_id)


class LiveConformanceTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)

        from routes.api_v1.providers import api_v1_providers_bp
        cls.app.register_blueprint(api_v1_providers_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(
            owner_user_id=OWNER, name='contract', scopes=['providers'])

    def setUp(self):
        clear_test_model_routing(owner_user_id=OWNER)

    @staticmethod
    async def _data(response, operation_id):
        body = await response.get_json()
        data = {key: value for key, value in body.items() if key != 'ok'}
        decode_response(operation_id, data)
        return data

    def test_live_responses_decode_against_the_contract(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}

            authority = await cli.get('/api/v1/model-routing', headers=headers)
            self.assertEqual(authority.status_code, 200)
            data = await self._data(authority, 'getModelRouting')
            revision = data['revision']

            replaced = await cli.put(
                '/api/v1/model-routing',
                headers=headers,
                json={
                    'expected_revision': revision,
                    'model_routing': data['model_routing'],
                },
            )
            self.assertEqual(
                replaced.status_code, 200,
                await replaced.get_data(as_text=True))
            data = await self._data(replaced, 'putModelRouting')
            revision = data['revision']

            templates = await cli.get(
                '/api/v1/providers/templates', headers=headers)
            self.assertEqual(templates.status_code, 200)
            await self._data(templates, 'listProviderTemplates')

            compiled = await cli.post(
                '/api/v1/providers/templates/compile',
                headers=headers,
                json={'template_key': 'meituan',
                      'selected_model_ids': ['LongCat-2.0']},
            )
            self.assertEqual(
                compiled.status_code, 200,
                await compiled.get_data(as_text=True))
            await self._data(compiled, 'compileProviderTemplate')

            created = await cli.post(
                '/api/v1/providers',
                headers=headers,
                json=native_test_provider_bundle(
                    expected_revision=revision,
                    provider_id='contract-provider',
                    base_url='http://127.0.0.1:1/v1'),
            )
            self.assertEqual(
                created.status_code, 201,
                await created.get_data(as_text=True))
            data = await self._data(created, 'createProvider')
            revision = data['revision']
            bundle = data['provider']

            listed = await cli.get('/api/v1/providers', headers=headers)
            self.assertEqual(listed.status_code, 200)
            await self._data(listed, 'listProviders')

            fetched = await cli.get(
                '/api/v1/providers/contract-provider', headers=headers)
            self.assertEqual(fetched.status_code, 200)
            await self._data(fetched, 'getProvider')

            updated = await cli.patch(
                '/api/v1/providers/contract-provider',
                headers=headers,
                json={
                    'expected_revision': revision,
                    'provider': bundle['provider'],
                    'provider_access': bundle['provider_access'],
                    'connections': bundle['connections'],
                    'credentials': bundle['credentials'],
                    'offerings': bundle['offerings'],
                    'deployments': bundle['deployments'],
                },
            )
            self.assertEqual(
                updated.status_code, 200,
                await updated.get_data(as_text=True))
            data = await self._data(updated, 'updateProvider')
            revision = data['revision']

            revealed = await cli.post(
                '/api/v1/model-routing/credentials/'
                'contract-provider-credential/secret/reveal',
                headers=headers)
            self.assertEqual(
                revealed.status_code, 200,
                await revealed.get_data(as_text=True))
            data = await self._data(revealed, 'revealCredentialSecret')
            self.assertEqual(data['credential_id'], 'contract-provider-credential')
            self.assertEqual(data['secret'], 'sk-internal-secret')

            deleted = await cli.delete(
                '/api/v1/providers/contract-provider',
                headers=headers,
                json={'expected_revision': revision},
            )
            self.assertEqual(
                deleted.status_code, 200,
                await deleted.get_data(as_text=True))
            await self._data(deleted, 'deleteProvider')

            missing = await cli.post(
                '/api/v1/model-routing/credentials/'
                'contract-provider-credential/secret/reveal',
                headers=headers)
            self.assertEqual(missing.status_code, 404)

        _new_loop_run(go())

    def test_undeclared_field_is_rejected_before_mutation(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            authority = await cli.get('/api/v1/model-routing', headers=headers)
            revision = (await authority.get_json())['revision']
            payload = native_test_provider_bundle(
                expected_revision=revision,
                provider_id='contract-sneaky',
                base_url='http://127.0.0.1:1/v1')
            payload['surprise'] = 'not-in-the-contract'
            rejected = await cli.post(
                '/api/v1/providers', headers=headers, json=payload)
            self.assertEqual(rejected.status_code, 400)
            body = await rejected.get_json()
            self.assertIn('surprise', str(body))

            listed = await cli.get('/api/v1/providers', headers=headers)
            providers = (await listed.get_json())['providers']
            self.assertNotIn(
                'contract-sneaky',
                {row['provider']['provider_id'] for row in providers},
            )

        _new_loop_run(go())


class DispatcherHotReloadTest(unittest.TestCase):
    """Committed authority mutations hot-reload the dispatcher slot pool.

    Regression pin for the provider-template card: saving the disable toggle
    (PUT /api/v1/model-routing) committed the v2 authority but left the
    cached slot pool untouched until process restart.
    """

    @classmethod
    def setUpClass(cls):
        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)

        from routes.api_v1.providers import api_v1_providers_bp
        cls.app.register_blueprint(api_v1_providers_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(
            owner_user_id=OWNER, name='hot-reload', scopes=['providers'])

    def setUp(self):
        clear_test_model_routing(owner_user_id=OWNER)
        import lib.llm_dispatch.health_local as health_local
        self.rebuilds = []
        patcher = mock.patch.object(
            health_local,
            '_rebuild_dispatcher_slots',
            lambda: self.rebuilds.append(True))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _authority(self, cli, headers):
        response = await cli.get('/api/v1/model-routing', headers=headers)
        self.assertEqual(response.status_code, 200)
        return await response.get_json()

    def test_put_model_routing_rebuilds_slots(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            data = await self._authority(cli, headers)
            replaced = await cli.put(
                '/api/v1/model-routing',
                headers=headers,
                json={
                    'expected_revision': data['revision'],
                    'model_routing': data['model_routing'],
                },
            )
            self.assertEqual(
                replaced.status_code, 200,
                await replaced.get_data(as_text=True))
            self.assertEqual(len(self.rebuilds), 1)

        _new_loop_run(go())

    def test_rejected_put_does_not_rebuild(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            data = await self._authority(cli, headers)

            conflict = await cli.put(
                '/api/v1/model-routing',
                headers=headers,
                json={
                    'expected_revision': data['revision'] + 1,
                    'model_routing': data['model_routing'],
                },
            )
            self.assertEqual(conflict.status_code, 409)

            invalid = await cli.put(
                '/api/v1/model-routing',
                headers=headers,
                json={'model_routing': data['model_routing']},
            )
            self.assertEqual(invalid.status_code, 400)

            self.assertEqual(self.rebuilds, [])

        _new_loop_run(go())

    def test_provider_and_secret_mutations_rebuild(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            data = await self._authority(cli, headers)
            revision = data['revision']

            created = await cli.post(
                '/api/v1/providers',
                headers=headers,
                json=native_test_provider_bundle(
                    expected_revision=revision,
                    provider_id='hot-reload',
                    base_url='http://127.0.0.1:1/v1'),
            )
            self.assertEqual(
                created.status_code, 201,
                await created.get_data(as_text=True))
            body = await created.get_json()
            revision = body['revision']
            self.assertEqual(len(self.rebuilds), 1)

            bundle = body['provider']
            updated = await cli.patch(
                '/api/v1/providers/hot-reload',
                headers=headers,
                json={
                    'expected_revision': revision,
                    'provider': bundle['provider'],
                    'provider_access': bundle['provider_access'],
                    'connections': bundle['connections'],
                    'credentials': bundle['credentials'],
                    'offerings': bundle['offerings'],
                    'deployments': bundle['deployments'],
                },
            )
            self.assertEqual(
                updated.status_code, 200,
                await updated.get_data(as_text=True))
            revision = (await updated.get_json())['revision']
            self.assertEqual(len(self.rebuilds), 2)

            rotated = await cli.put(
                '/api/v1/model-routing/credentials/'
                'hot-reload-credential/secret',
                headers=headers,
                json={
                    'expected_revision': revision,
                    'secret': 'sk-rotated-secret',
                },
            )
            self.assertEqual(
                rotated.status_code, 200,
                await rotated.get_data(as_text=True))
            revision = (await rotated.get_json())['revision']
            self.assertEqual(len(self.rebuilds), 3)

            deleted = await cli.delete(
                '/api/v1/providers/hot-reload',
                headers=headers,
                json={'expected_revision': revision},
            )
            self.assertEqual(
                deleted.status_code, 200,
                await deleted.get_data(as_text=True))
            self.assertEqual(len(self.rebuilds), 4)

        _new_loop_run(go())


class LegacyKeyHintRefreshTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)

        from routes.api_v1.providers import api_v1_providers_bp
        cls.app.register_blueprint(api_v1_providers_bp)

        from lib.api_keys import create_key
        _row, cls.token = create_key(
            owner_user_id=OWNER, name='hint-refresh', scopes=['providers'])

    def setUp(self):
        clear_test_model_routing(owner_user_id=OWNER)

    def test_get_lazily_reseals_legacy_ellipsis_key_hints(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            authority = await cli.get('/api/v1/model-routing', headers=headers)
            revision = (await authority.get_json())['revision']
            created = await cli.post(
                '/api/v1/providers',
                headers=headers,
                json=native_test_provider_bundle(
                    expected_revision=revision,
                    provider_id='hint-refresh',
                    base_url='http://127.0.0.1:1/v1',
                    secret='sk-internal-secret'),
            )
            self.assertEqual(
                created.status_code, 201,
                await created.get_data(as_text=True))

            # Simulate a credential persisted before the asterisk mask format.
            from lib.model_routing import ModelRoutingRepository, OwnerBoundary
            repository = ModelRoutingRepository()
            boundary = OwnerBoundary.create(OWNER)
            stale = repository.get(boundary)
            document = stale.document
            document['credentials'][0]['key_hint'] = 'sk-i…cret'
            repository.compare_and_swap(
                boundary, document, expected_revision=stale.revision)

            expected = 'sk-i' + '*' * 10 + 'cret'
            served = await cli.get('/api/v1/model-routing', headers=headers)
            self.assertEqual(served.status_code, 200)
            body = await served.get_json()
            hints = [
                row.get('key_hint')
                for row in body['model_routing']['credentials']
            ]
            self.assertEqual(hints, [expected])
            persisted = repository.get(boundary).document['credentials'][0]
            self.assertEqual(persisted['key_hint'], expected)

        _new_loop_run(go())

if __name__ == '__main__':
    unittest.main()
