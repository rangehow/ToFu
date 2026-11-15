"""The public core boots and serves with no company overlay installed.

Executable form of docs/PUBLIC_CORE.md, "Core boots without the overlay". A
child process (import state is process-global) refuses every overlay module
(``OVERLAY_MODULES``), points bundle discovery at an empty directory with no
installed ``tofu.bundles`` entry points, assembles the real HTTP application
and exercises it. Every gateway trait must then be absent: the FRIDAY host
gets no tool-name budget, no blocked-term sanitizer, no proprietary image API
and no gateway cache pricing, and no company provider template, site pack or
search gateway is offered.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from tests.meta.test_public_core_boundary import OVERLAY_MODULES

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent('''
    import asyncio, importlib.abc, json, logging, sys
    from pathlib import Path

    blocked, empty_bundles, static_dir = json.loads(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    refused = []

    class RefuseOverlay(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split('.')[0] in blocked:
                refused.append(name)
                raise ModuleNotFoundError(f'overlay module {name} is not installed')
            return None

    sys.meta_path.insert(0, RefuseOverlay())
    import tofu_extensions
    tofu_extensions.IN_TREE_BUNDLES_DIR = empty_bundles
    tofu_extensions._installed_roots = ()

    from lib.app.app_assembly import create_application
    app = create_application('public-core', static_dir=static_dir,
                             logger=logging.getLogger('public-core'),
                             secret_key='public-core', config={'TESTING': True})

    async def live():
        response = await app.test_client().get('/health/live')
        return response.status_code

    from lib.channels import service
    from lib.channels.adapters import installed_channel_adapters
    from lib.channels.model import ChannelError
    from lib.llm.gateway_shape import apply_gateway_anthropic_shape, gateway_anthropic_headers
    from lib.mcp.project_names import get_doc_url
    from lib.mcp.registry import get_catalog
    from lib.mcp.vendored import VENDORED_LAUNCHERS
    from lib.provider.local_credentials import local_credential_source
    from lib.search.search_bridge import _ChatuiMcpDocumentReader
    try:
        service.account_view(1)
        dx_account = 'served'
    except ChannelError as error:
        dx_account = error.code

    from lib.image_gen import _is_friday_provider
    from lib.llm.tool_wire import provider_tool_name_budget_bytes
    from lib.llm_sanitize import gateway_blocked_term_sanitize
    from lib.pricing._provider import PROVIDER_PRICING
    from lib.provider.gateway_profiles import gateway_profiles
    from lib.provider.provider_template_recipes import load_provider_templates
    from lib.search.search_gateway import load_gateway_catalog
    from lib.site_packs._loader import load_packs

    class Slot:
        base_url = 'https://api.openai.com/v1'

    print(json.dumps({
        'live': asyncio.run(live()),
        'blueprints': len(app.blueprints),
        'bundle_roots': [str(p) for p in tofu_extensions.bundle_roots()],
        'profiles': [p['id'] for p in gateway_profiles()],
        'tool_name_budget': provider_tool_name_budget_bytes(
            api_protocol='anthropic', base_url='https://api.openai.com/v1/anthropic',
            model='claude-opus-5'),
        'sanitize': [gateway_blocked_term_sanitize(p) for p in ('example-corp', 'example_corp_anthropic', '')],
        'friday_image_api': _is_friday_provider(Slot()),
        'gateway_pricing_providers': sorted(set(PROVIDER_PRICING) & {'yourprovider', 'example-corp'}),
        'template_keys': sorted(t.get('key', '') for t in load_provider_templates()),
        'site_packs': sorted(load_packs()),
        'search_gateways': sorted(load_gateway_catalog()),
        'channel_adapters': sorted(installed_channel_adapters()),
        'dx_account': dx_account,
        'catpaw_shape': apply_gateway_anthropic_shape({'system': 'x'}, 'https://mcli.internal.example.com'),
        'catpaw_headers': gateway_anthropic_headers('https://mcli.internal.example.com'),
        'catpaw_local_credential': local_credential_source('catpaw') is not None,
        'mcp_catalog_ids': sorted(entry['id'] for entry in get_catalog()),
        'launchers': sorted(VENDORED_LAUNCHERS),
        'km_reader': _ChatuiMcpDocumentReader().matches('https://km.internal.example.com/collabpage/1'),
        'km_doc_url': get_doc_url('2754681609', user_id=1),
        'refused': sorted(set(refused)),
        'loaded_overlay': sorted(m for m in sys.modules if m.split('.')[0] in blocked),
    }))
''')


def test_public_core_boots_and_serves_without_any_overlay(tmp_path):
    empty_bundles = tmp_path / 'no-bundles'
    empty_bundles.mkdir()
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    completed = subprocess.run(
        [sys.executable, '-c', _CHILD, json.dumps(OVERLAY_MODULES), str(empty_bundles),
         str(static_dir)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    assert completed.returncode == 0, completed.stderr[-4000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result['live'] == 200
    assert result['blueprints'] > 0
    assert result['loaded_overlay'] == []
    assert result['bundle_roots'] == []
    assert result['profiles'] == []
    assert result['tool_name_budget'] == 0
    assert result['sanitize'] == [False, False, False]
    assert result['friday_image_api'] is False
    assert result['gateway_pricing_providers'] == []
    assert 'yourprovider' not in result['template_keys']
    assert not {'friday', 'modelplaza'} & set(result['site_packs'])
    assert result['search_gateways'] == []
    # Increments 2-6: CatPaw, 大象, 学城, internal MCPs are absent with their overlay.
    assert result['channel_adapters'] == [] and result['dx_account'] == 'channel_unavailable'
    assert result['catpaw_shape'] == {'system': 'x'} and result['catpaw_headers'] == {}
    assert result['catpaw_local_credential'] is False
    assert 'catpaw' not in result['template_keys']
    assert not {'hope', 'llm', 'xuecheng'} & set(result['mcp_catalog_ids'])
    assert result['mcp_catalog_ids'], 'the public catalog still ships its own connectors'
    assert not {'hope-mcp', 'llm-mcp', 'xuecheng-mcp'} & set(result['launchers'])
    assert result['km_reader'] is False and result['km_doc_url'] == ''

