"""Provider edits retain owner revocation history across CAS and deletion."""
import asyncio

import pytest
from quart import Quart

from lib.model_routing import ModelRoutingRepository, OwnerBoundary
from tests.support.model_routing import clear_test_model_routing, install_native_test_model_route

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit


def test_patch_records_model_revocation_and_delete_preserves_it():
    from lib.api_keys import create_key
    from routes.api_v1.auth import bearer_auth_before_request
    from routes.api_v1.providers import api_v1_providers_bp

    owner = 712381
    clear_test_model_routing(owner_user_id=owner)
    install_native_test_model_route(owner_user_id=owner)
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    app.before_request(bearer_auth_before_request)
    app.register_blueprint(api_v1_providers_bp)
    _, token = create_key(owner_user_id=owner, name='revoke-test', scopes=['providers'])

    async def exercise():
        client = app.test_client()
        headers = {'Authorization': f'Bearer {token}'}
        response = await client.get('/api/v1/providers/test-provider', headers=headers)
        data = await response.get_json()
        bundle = data['provider']
        bundle['credentials'][0]['authorization']['models'] = []
        payload = {key: bundle[key] for key in (
            'provider', 'provider_access', 'connections', 'credentials', 'offerings', 'deployments')}
        payload['expected_revision'] = data['revision']
        response = await client.patch('/api/v1/providers/test-provider', headers=headers, json=payload)
        assert response.status_code == 200, await response.get_data(as_text=True)
        updated = await response.get_json()
        repository = ModelRoutingRepository()
        boundary = OwnerBoundary.create(owner)
        history = repository.get(boundary).document['revocation_history']
        assert history == [{'credential_id': 'test-local-identity', 'creator_id': 'tofu-test',
                            'model_id': 'stub-model', 'model_revoked': True,
                            'excluded_wire_model_ids': []}]
        response = await client.delete('/api/v1/providers/test-provider', headers=headers,
                                       json={'expected_revision': updated['revision']})
        assert response.status_code == 200, await response.get_data(as_text=True)
        assert repository.get(boundary).document['revocation_history'] == history

    try:
        asyncio.run(exercise())
    finally:
        clear_test_model_routing(owner_user_id=owner)
