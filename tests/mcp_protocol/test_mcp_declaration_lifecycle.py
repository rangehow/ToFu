"""MCP authority through outages, losses and refresh; no MCP wire is declared.

MCP tools are search-only (``lib/tools/mcp_exposure.py``): assembly publishes
the live catalog as task authority and never consults the retired pre-request
selection or the durable conversation declarations. Store-level declaration
behavior is covered by ``tests/storage/test_mcp_declarations.py``.
"""
import json
from types import SimpleNamespace

import pytest

from lib.mcp import tool_search
from lib.storage.mcp_declarations import MCPDeclarationError, MCPDeclarationStore
from lib.tools.registry import _build
from tests.mcp_protocol.test_mcp_tool_search import _large_snapshot, _definition

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_caches():
    tool_search.clear_tool_search_caches()
    yield
    tool_search.clear_tool_search_caches()


def context():
    return SimpleNamespace(cfg={}, tid='test', task_id='test',
                           conv_id='durable', owner_user_id=7,
                           messages=[{'role': 'user', 'content': 'hope search'}])


def bridge(monkeypatch, snapshot=None, *, connected=True):
    import lib.mcp
    rows = _large_snapshot() if snapshot is None else snapshot
    value = SimpleNamespace(connected=connected, server_count=2,
        get_openai_tool_defs=lambda: [row['openai_def'] for row in rows],
        get_tool_catalog_snapshot=lambda: rows)
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: value)
    return value


def _authority(ctx):
    return [tool['function']['name'] for tool in ctx.cfg['_mcpAllowedToolCatalog']]


@pytest.mark.parametrize('connected', [False, True])
def test_disconnect_or_empty_catalog_clears_authority_and_declares_nothing(
        monkeypatch, connected):
    ctx = context()
    bridge(monkeypatch)
    assert _build._build_mcp(ctx) == []
    assert _authority(ctx) == [row['namespaced_name'] for row in _large_snapshot()]
    bridge(monkeypatch, [], connected=connected)
    assert _build._build_mcp(ctx) == []
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []


def test_explicit_empty_survives_clear_and_late_connection(monkeypatch):
    ctx = context()
    bridge(monkeypatch, connected=False)
    assert _build._build_mcp(ctx) == []
    tool_search.clear_tool_search_caches()
    bridge(monkeypatch)
    assert _build._build_mcp(ctx) == []
    assert ctx.cfg['_mcpAllowedToolCatalog']


def test_assembly_never_consults_selection_or_durable_declarations(
        monkeypatch, declaration_authority):
    def forbidden(*args, **kwargs):
        raise AssertionError('MCP search-only assembly consulted retired wire state')
    for name in ('select_active_mcp_tools', 'freeze_wire_definitions',
                 'frozen_wire_definitions', 'record_mcp_tool_used'):
        monkeypatch.setattr(tool_search, name, forbidden)
    monkeypatch.setattr(MCPDeclarationStore, 'get', forbidden)
    monkeypatch.setattr(MCPDeclarationStore, 'freeze', forbidden)
    ctx = context()
    bridge(monkeypatch)
    assert _build._build_mcp(ctx) == []
    assert _authority(ctx)
    assert declaration_authority.settings == {}


def registry_context(cfg):
    from lib.tools.registry import _spec
    return _spec.ToolContext(cfg=cfg, task_id='test', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False)


def test_assembly_propagates_declaration_errors_and_never_grants_old_wire(monkeypatch):
    from lib.tools.registry import _spec
    wire = [_definition('gone', 'read')]
    spec = _spec.ToolSpec('mcp', lambda ctx: wire, phase='base')
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [spec])
    ctx = registry_context({'_mcpAllowedToolCatalog': []})
    tools, _ = _spec.assemble_tool_list(ctx)
    assert tools == wire
    assert ctx.executable_tool_catalog == []
    def fail(ctx):
        raise MCPDeclarationError('declaration outage')
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [_spec.ToolSpec('mcp', fail, phase='base')])
    with pytest.raises(MCPDeclarationError, match='outage'):
        _spec.assemble_tool_list(registry_context({}))


def test_complete_resident_observations_and_durable_wire_footprint(monkeypatch, declaration_authority):
    from lib.storage.mcp_declarations import MAX_DECLARATION_BYTES, SETTING_KEY
    rows = _large_snapshot()
    for index in range(100):
        scope = tool_search.mcp_selection_scope_id(task_id='', conv_id=f'footprint-{index}', owner_user_id=7)
        tool_search.select_active_mcp_tools(rows, task_id='', selection_scope_id=scope, query='hope', limit=4)
    assert len(tool_search._SELECTION_STATE) == 100
    assert all('wire' not in state for state in tool_search._SELECTION_STATE.values())
    sizes = [len(json.dumps(settings[SETTING_KEY], separators=(',', ':')).encode('utf-8'))
             for settings in declaration_authority.settings.values()]
    assert len(sizes) == 100 and max(sizes) <= MAX_DECLARATION_BYTES
    print(f'footprint conversations=100 resident_states=100 resident_wires=0 '
          f'slot_bytes_each={max(sizes)} total_slot_bytes={sum(sizes)}')


def test_legacy_valid_checkpoint_cannot_resurrect_reset_wire(monkeypatch):
    from lib.tasks_pkg import model_replay
    config = {'model': 'test'}
    row = {'turnId': 'turn', 'status': 'completed', 'projection': {}}
    body = {'version': 1, 'policy': model_replay.policy_witness(config),
            'turns': [model_replay.turn_witness(row)],
            'messages': [{'role': 'assistant', 'content': 'done'}],
            'mcpWire': [_definition('obsolete', 'read')]}
    monkeypatch.setattr(model_replay.ModelContinuationStore, 'get', lambda *args, **kwargs: body)
    store = MCPDeclarationStore()
    store.freeze([_definition('old', 'read')], user_id=7, conversation_id='durable')
    store.reset(user_id=7, conversation_id='durable')
    assert model_replay.restore_prefix([row, {'turnId': 'target', 'projection': {}}], config,
                                      conversation_id='durable', user_id=7)[1] == 1
    assert store.get(user_id=7, conversation_id='durable') is None


def test_turn_assembly_does_not_hide_declaration_failure(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep
    def fail(*args, **kwargs):
        raise MCPDeclarationError('declaration authority unavailable')
    monkeypatch.setattr(prep, '_assemble_tool_list', fail)
    task = {'id': 'test', 'messages': [], '_executable_tool_catalog': [_definition('old', 'read')]}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off',
                search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
    with pytest.raises(MCPDeclarationError):
        prep.assemble_round_tools({}, task, mcfg)
    assert not task.get('_executable_tool_catalog')


@pytest.fixture(params=['get_openai_tool_defs', 'get_tool_catalog_projection'],
                ids=['early', 'partial_catalog'])
def catalog_outage(monkeypatch, request):
    def inject(live):
        def fail():
            raise OSError('catalog unavailable')
        monkeypatch.setattr(live, request.param, fail, raising=False)
    return inject


def test_catalog_outage_keeps_baseline_without_mcp_grants(
        monkeypatch, catalog_outage, declaration_authority):
    from lib.tools.registry import _spec

    ctx = registry_context({'tools.nativeExposure': 'full'})
    ctx.owner_user_id = 7
    ctx.conv_id = 'catalog-outage'
    live = bridge(monkeypatch)
    _build._build_mcp(ctx)
    assert ctx.cfg['_mcpAllowedToolCatalog']
    assert ctx.cfg['_mcpToolSearchTextByName']

    baseline = {'type': 'function', 'function': {
        'name': 'local_tool', 'description': 'Available without MCP',
        'parameters': {'type': 'object', 'properties': {}},
    }}
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [
        _spec.ToolSpec('local', lambda ctx: [baseline], phase='base'),
        _spec.ToolSpec('mcp', _build._build_mcp, phase='base'),
    ])
    catalog_outage(live)

    tools, _ = _spec.assemble_tool_list(ctx)

    assert tools == [baseline]
    assert ctx.executable_tool_catalog == [baseline]
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []
    assert ctx.cfg['_mcpToolSearchTextByName'] == {}
    assert declaration_authority.settings == {}


def test_catalog_exception_drops_grants_without_raising(monkeypatch, catalog_outage):
    ctx = context()
    live = bridge(monkeypatch)
    assert _build._build_mcp(ctx) == [] and _authority(ctx)
    catalog_outage(live)
    for conversation in ('durable', 'new-catalog-outage'):
        ctx.conv_id = conversation
        assert _build._build_mcp(ctx) == []
        assert ctx.cfg['_mcpAllowedToolCatalog'] == []
        assert ctx.cfg['_mcpToolSearchTextByName'] == {}
        assert MCPDeclarationStore().get(user_id=7, conversation_id=conversation) is None


@pytest.mark.parametrize('loss', ['disconnect', 'revoke', 'outage'])
def test_disclosed_mcp_loss_is_unavailable_not_hallucinated_and_recovers(monkeypatch, loss):
    import threading
    from lib.tools.disclosure_state import record_search_items
    from lib.tools.registry import _spec
    from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    name = 'mcp__llm__experiment_eval_result'
    schema = _definition('llm', 'experiment_eval_result')
    schema['function']['parameters'] = {
        'type': 'object', 'properties': {'experiment_id_or_name_or_url': {'type': 'string'}},
        'required': ['experiment_id_or_name_or_url'], 'additionalProperties': False}
    rows = [{'openai_def': schema, 'namespaced_name': name, 'meta': {}}]
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [
        _spec.ToolSpec('mcp', _build._build_mcp, phase='base')])
    def assemble():
        ctx = registry_context({})
        ctx.owner_user_id, ctx.conv_id = 7, 'unavailable-regression'
        wire, _ = _spec.assemble_tool_list(ctx)
        return wire, ctx

    bridge(monkeypatch, rows)
    initial, ctx = assemble()
    assert initial == [] and [tool['function']['name']
                              for tool in ctx.executable_tool_catalog] == [name]
    task = {'id': 'unavailable-test', 'convId': 'unavailable-regression',
            'model': 'test', 'events': [], 'events_lock': threading.Lock(),
            'toolRounds': [], 'aborted': False, 'config': {'autopilotEnabled': True}}
    # The model saw the tool only through a search result.
    record_search_items(task, [{'name': name}], catalog=ctx.executable_tool_catalog)
    live = bridge(monkeypatch, [], connected=loss != 'disconnect')
    if loss == 'outage':
        def fail():
            raise OSError('discovery unavailable')
        live.get_openai_tool_defs = fail
    wire, ctx = assemble()
    assert wire == []
    task.update(_tool_schema=wire, _executable_tool_catalog=ctx.executable_tool_catalog,
                _toolContractDocumentsByName=ctx.tool_contract_documents_by_name)
    assert name not in _known_tool_names(task)
    def parse(called=name):
        return parse_tool_calls({'content': '', 'tool_calls': [{
            'id': 'mcp-call', 'type': 'function', 'function': {
                'name': called, 'arguments': '{"experiment_id_or_name_or_url":"331785"}'}}]},
            task, round_num=6, tool_round_num=len(task['toolRounds']), project_enabled=False)[0][0]

    for _ in range(5):
        parsed = parse()
        assert parsed[5]['status'] == 'rejected'
        assert parsed[5]['_rejected']['kind'] == 'tool_unavailable'
        assert parsed[5]['_rejected']['executable'] is False
        assert 'not a real tool' not in str(parsed[5])
        assert not task['aborted']
    assert name not in _known_tool_names(task)
    invented = parse('mcp__llm__invented')
    assert invented[5]['_rejected']['kind'] == 'hallucinated'

    bridge(monkeypatch, rows)
    restored, ctx = assemble()
    task.update(_tool_schema=restored, _executable_tool_catalog=ctx.executable_tool_catalog,
                _toolContractDocumentsByName=ctx.tool_contract_documents_by_name)
    assert restored == [] and name in _known_tool_names(task)
    accepted = parse()
    assert accepted[1] == name
    assert accepted[5].get('status') != 'rejected'


def test_root_request_refresh_recovers_startup_mcp_without_widening(monkeypatch):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools, refresh_root_mcp_authority)
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    rows = _large_snapshot()[:2]
    name = rows[0]['namespaced_name']
    bridge(monkeypatch, rows)
    cfg = {'userId': 7, 'model': 'test',
           'memoryEnabled': False, 'schedulerEnabled': False,
           'preferencesEnabled': False, 'plugins': []}
    task = {'id': 'root-startup', 'convId': 'durable', '_userId': 7,
            'messages': [], 'config': cfg}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off',
                search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
    wire, _ = assemble_round_tools(cfg, task, mcfg)
    assert name in task['_rootMcpToolNames'] and name in _known_tool_names(task)
    assert not [tool for tool in wire if tool['function']['name'].startswith('mcp__')]
    # The startup catalog drops away (a parked or restarting server).
    bridge(monkeypatch, [], connected=True)
    refresh_root_mcp_authority(task)

    admitted_wire = list(task['_tool_schema'])

    from lib.tools.gateway import local_wire_tools
    task['_toolDiscoveryPolicyByName'][name] = 'searchable'
    task['_toolSearchCatalogSize'] = 100
    task['_toolSearchableCount'] = 1
    def provider_wire():
        return local_wire_tools(task['_tool_schema'],
            discovery_policy_by_name=task['_toolDiscoveryPolicyByName'],
            discovery_catalog_size=task['_toolSearchCatalogSize'],
            searchable_count=task['_toolSearchableCount'], apply_schema_budget=False)
    frozen_provider_wire = provider_wire()
    assert name not in _known_tool_names(task)
    before = [tool for tool in task['_executable_tool_catalog']
              if not tool['function']['name'].startswith('mcp__')]
    bridge(monkeypatch, rows + _large_snapshot()[2:4])
    from lib.tasks_pkg.orchestrator import _round_request_prep as request_prep
    monkeypatch.setattr(request_prep.orchestrator_ports, 'build_request_body',
                        lambda model, messages, **kw: {'messages': messages, 'tools': kw['tools']})
    monkeypatch.setattr(request_prep, 'emit_messages_snapshot_event', lambda *a, **kw: None)
    _, body = request_prep.build_round_request(
        task, SimpleNamespace(model='test', preset=None, thinking_enabled=False), [], wire,
        round_num=6, tid=task['id'], thinking_depth=None, temperature=None,
        max_tokens=100, response_format=None)
    assert name in {tool['function']['name'] for tool in body['_executable_tool_catalog']}
    assert name in _known_tool_names(task)
    assert name in task['_toolContractDocumentsByName']
    assert [tool for tool in task['_executable_tool_catalog']
            if not tool['function']['name'].startswith('mcp__')] == before
    assert task['_tool_schema'] == admitted_wire
    new_names = {row['namespaced_name'] for row in _large_snapshot()[2:4]}
    assert not new_names.intersection(_known_tool_names(task))
    bridge(monkeypatch, [], connected=True)
    refresh_root_mcp_authority(task)
    assert name not in _known_tool_names(task)
    assert name not in task['_toolContractDocumentsByName']

    assert provider_wire() == frozen_provider_wire
    bridge(monkeypatch, rows)
    refresh_root_mcp_authority(task)
    assert name in _known_tool_names(task)
    cfg['mcpEnabled'] = False
    refresh_root_mcp_authority(task)
    assert name not in _known_tool_names(task)


def test_root_mcp_refresh_never_reassembles_child_authority(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep
    from lib.tasks_pkg.invocation import InvocationPolicy
    task = {'_invocationPolicy': InvocationPolicy.child(parent_task_id='parent'),
            '_rootToolAssemblyConfig': {}, '_rootMcpToolNames': ['mcp__llm__read'],
            '_tool_schema': [_definition('llm', 'read')], '_executable_tool_catalog': []}
    def forbidden(*args, **kwargs):
        pytest.fail('child must never reassemble root inventory')
    monkeypatch.setattr(prep, 'assemble_round_tools', forbidden)
    prep.refresh_root_mcp_authority(task)
    assert task['_executable_tool_catalog'] == []


def test_text_only_turn_clears_root_mcp_recovery(monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep
    task = {'_rootToolAssemblyConfig': {}, '_rootMcpToolNames': ['mcp__llm__read']}
    prep._disable_tools_for_turn(task)
    def forbidden(*args, **kwargs):
        pytest.fail('text-only turn must not rebuild tool authority')
    monkeypatch.setattr(prep, 'assemble_round_tools', forbidden)
    prep.refresh_root_mcp_authority(task)
    assert task['_executable_tool_catalog'] == []
