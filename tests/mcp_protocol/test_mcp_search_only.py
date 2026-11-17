"""MCP tools are search-only on every surface (owner decision 2026-11-13).

Owner, 11-13 06:40 CST: "The MCP tools have not been disabled; they are all
accessed through tool search. There is no need to let a few slip through, as
that would compromise the clean context needed for headless benchmark
evaluation." A connected MCP tool therefore stays in the task's executable
catalog, ``search_tools`` finds it and ``execute_tools`` runs it, while no MCP
name, schema or listing reaches a direct tool list or an eager context block:
owner browser chat, Coordinator/Router, Worker grants and swarm children here;
headless runs (``tools: "*"`` and the lean prompt profile) against a loopback
MCP server in ``test_mcp_search_only_headless.py``. No project, conversation,
grant or request setting is an exception. Policy owner:
``lib/tools/mcp_exposure.py``.
"""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

OWNER = 7
# Twelve tools on two servers: more than the retired eight-schema "active"
# preselection, so a frozen subset and a late-tool listing would both show.
CATALOG = {
    'hope': (
        ('get_job_logs', 'Read the logs of a hope training job.'),
        ('submit_job', 'Submit a GPU training job to hope.'),
        ('list_jobs', 'List the hope training jobs of this user.'),
        ('get_job', 'Read the status of one hope training job.'),
        ('stop_job', 'Stop a running hope training job.'),
        ('diagnose_job', 'Diagnose a failed hope training job.'),
    ),
    'docs': (
        ('read_page', 'Read a knowledge base page by its link.'),
        ('search_pages', 'Search knowledge base pages by keyword.'),
        ('create_page', 'Create a knowledge base page.'),
        ('update_page', 'Update a knowledge base page.'),
        ('list_spaces', 'List knowledge base spaces.'),
        ('get_comments', 'Read the comments of a knowledge base page.'),
    ),
}
MCP_NAMES = sorted(f'mcp__{server}__{tool}' for server, tools in CATALOG.items()
                   for tool, _ in tools)
RETIRED_WRAPPERS = {'search_mcp_tools', 'call_mcp_read_tool', 'call_mcp_write_tool'}
COMPATIBLE = 'https://compatible.example/v1'
SANKUAI_ANTHROPIC = 'https://api.openai.com/v1/claude/aws'
# Settings that used to change MCP exposure, the superseded opt-in included.
FORMER_EXCEPTIONS = (
    {},
    {'mcpToolExposure': 'inline'},
    {'mcpToolExposure': 'all'},
    {'mcpToolExposure': 'progressive'},
    {'mcpToolExposure': 'delegate'},
    {'mcpToolExposure': 'auto', 'mcpActiveToolLimit': 50},
    {'mcpInlineToolLimit': 64},
    {'mcpEnabled': True},
)


def _definition(server, tool, description):
    return {'type': 'function', 'function': {
        'name': f'mcp__{server}__{tool}',
        'description': f'[MCP:{server}] {description}',
        'parameters': {'type': 'object', 'properties': {'id': {'type': 'string'}}}}}


class _Bridge:
    """A connected owner bridge; nothing here may switch exposure."""

    connected = True
    server_count = len(CATALOG)

    def _rows(self):
        return [(server, tool, _definition(server, tool, description))
                for server, tools in CATALOG.items() for tool, description in tools]

    def get_openai_tool_defs(self):
        return [definition for _, _, definition in self._rows()]

    def get_tool_catalog_snapshot(self):
        return [{'server_name': server, 'tool_name': tool,
                 'namespaced_name': definition['function']['name'],
                 'openai_def': definition, 'meta': {}, 'read_only_hint': True}
                for server, tool, definition in self._rows()]

    def is_mcp_tool(self, name):
        return name in MCP_NAMES


@pytest.fixture
def bridge(monkeypatch):
    import lib.mcp as mcp_module
    from lib.tasks_pkg.context_composer import _providers
    from lib.tools.routing import _reset_sticky_exposure_for_tests
    owner_bridge = _Bridge()
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: owner_bridge)
    _providers._reset_tail_transitions_for_tests()
    _reset_sticky_exposure_for_tests()
    yield owner_bridge
    _reset_sticky_exposure_for_tests()


def _names(tools):
    return [str((tool.get('function') or {}).get('name') or tool.get('name') or '')
            for tool in tools or () if isinstance(tool, dict)]


def _mcp(names):
    return sorted(name for name in names if str(name).startswith('mcp__'))


def _mcfg(project_path=''):
    return {'tid': 'mcp-search-only', 'model': 'model-a', 'project_path': project_path,
            'project_enabled': bool(project_path), 'search_mode': 'off',
            'search_enabled': False, 'fetch_enabled': False,
            'code_exec_enabled': bool(project_path), 'browser_enabled': False,
            'desktop_enabled': False, 'image_gen_enabled': False,
            'human_guidance_enabled': False, 'scheduler_enabled': False}


def _task(task_id, cfg, *, conv_id='', content='Read the logs of my hope job.'):
    task = {'id': task_id, '_userId': OWNER, 'config': cfg,
            'messages': [{'role': 'user', 'content': content}],
            'abort_event': threading.Event()}
    if conv_id:
        task['convId'] = conv_id
    return task


def _wire(task, tools, *, model='model-a', protocol='openai', base_url=COMPATIBLE):
    """The provider-visible request after the shared final projection."""
    from lib.llm import tool_wire
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    body = {'model': model, 'messages': task['messages'], 'tools': copy.deepcopy(tools)}
    attach_tool_request_metadata(task, body, task['messages'], tools,
                                 model=model, round_num=1, tid=task['id'])
    tool_wire.project_tool_wire(body, api_protocol=protocol, base_url=base_url)
    return body


def _context_text(task, wire, *, project_path=''):
    """Every block the composer would add this turn, system and tail alike."""
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer import _providers
    blocks = _providers.collect_context_blocks(task['messages'], ComposeRequest(
        conv_id=task.get('convId', ''), user_id=OWNER, task=task, has_real_tools=True,
        tool_names=frozenset(_names(wire['tools'])), model='model-a',
        project_enabled=bool(project_path), project_path=project_path))
    return '\n'.join(f'[{block.id}] {block.content or ""}' for block in blocks)


def _wire_text(wire):
    return json.dumps(wire['tools'], ensure_ascii=False)


def _assert_search_only(task, tools, wire, context):
    """No MCP anywhere eager; every MCP tool in authority and found by search."""
    from lib.tools.gateway import search_executable_catalog
    assert _mcp(_names(tools)) == []
    assert _mcp(_names(wire['tools'])) == []
    assert not RETIRED_WRAPPERS & set(_names(wire['tools']))
    assert 'mcp' not in _wire_text(wire).lower()
    assert 'mcp' not in context.lower(), context
    for field in ('_frontendSelectedToolNames', '_requiredDirectToolNames',
                  '_toolInitialVisibleNames'):
        assert _mcp(task.get(field) or ()) == [], field
    assert _mcp(name for name, policy in (task.get('_toolDiscoveryPolicyByName') or {}).items()
                if policy == 'eager') == []
    catalog = task['_executable_tool_catalog']
    assert _mcp(_names(catalog)) == MCP_NAMES
    assert {'search_tools', 'execute_tools'} <= set(_names(wire['tools']))
    found = search_executable_catalog(
        catalog, 'read the logs of a hope training job',
        namespace_by_name=task.get('_executableToolNamespaceByName') or {},
        search_text_by_name=task.get('_executableToolSearchTextByName') or {},
        contract_documents_by_name=task.get('_toolContractDocumentsByName') or {},
        visible_names=set(_names(wire['tools'])))
    assert found['status'] == 'ok' and found['execute_with'] == 'execute_tools'
    assert found['items'][0]['name'] == 'mcp__hope__get_job_logs'


def _owner_chat(workspace, task_id, extra=None):
    from lib.conv_config import resolve_conv_config
    cfg = resolve_conv_config({'model': 'model-a'}, {}, {'serverModel': 'model-a'},
                              is_active=True)
    cfg.update(userId=OWNER, projectPath=str(workspace), plugins=[],
               memoryEnabled=False, preferencesEnabled=False, schedulerEnabled=False)
    cfg.update(extra or {})
    return _task(task_id, cfg, conv_id='owner-chat-' + task_id)


@pytest.mark.parametrize('extra', FORMER_EXCEPTIONS,
                         ids=lambda extra: json.dumps(extra, sort_keys=True))
def test_owner_chat_exposes_mcp_only_through_search_whatever_the_settings(
        bridge, workspace, extra):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    task = _owner_chat(workspace, 'owner-' + str(abs(hash(json.dumps(extra, sort_keys=True)))),
                       extra)

    tools, available = assemble_round_tools(task['config'], task, _mcfg(str(workspace)))
    wire = _wire(task, tools)

    assert available
    _assert_search_only(task, tools, wire, _context_text(task, wire, project_path=str(workspace)))


def test_initial_surface_drops_an_mcp_candidate_without_recorded_visibility(bridge, workspace):
    """Admission and the recorded surface never carry an MCP schema either."""
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as assembly
    task = _owner_chat(workspace, 'owner-candidate')
    task['config']['_toolSearchMode'] = 'off'
    tools, available = assembly._apply_assembled_tool_policy(
        {**task['config'], '_executableToolCatalog': [_LEAKED_CANDIDATE]},
        task, [_LEAKED_CANDIDATE], True)
    assert available and tools == []
    assert _mcp(_names(task['_tool_schema'])) == []
    assert _mcp(task['_toolInitialVisibleNames']) == []
    assert _mcp(_names(task['_executable_tool_catalog'])) == ['mcp__docs__tool_0']


def test_a_second_turn_still_lists_no_mcp_tool(bridge, workspace):
    """The retired tail listing named tools absent from a frozen first wire."""
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    task = _owner_chat(workspace, 'owner-second-turn')
    for _ in range(2):
        tools, _ = assemble_round_tools(task['config'], task, _mcfg(str(workspace)))
        wire = _wire(task, tools)
        _assert_search_only(task, tools, wire,
                            _context_text(task, wire, project_path=str(workspace)))


@pytest.fixture
def workspace(tmp_path_factory):
    """A project root whose path cannot echo the test name into context."""
    return tmp_path_factory.mktemp('workspace')


@pytest.fixture(autouse=True)
def coordination(workspace, monkeypatch):
    """Bind the Global Router home and one Project Coordinator conversation.

    Every other project path is an ordinary (non-coordinator) conversation.
    """
    from lib.conversations import project_coordination
    from lib.router.home import router_home_location
    monkeypatch.setattr('lib.router.home.router_home_root',
                        lambda: str(workspace / 'router'))
    project = workspace / 'project'
    project.mkdir()
    bindings = {router_home_location(OWNER): 'router', str(project): 'coordinator'}

    def read(path):
        return {'projectPath': path, 'conversationId': bindings.get(path, ''),
                'availability': 'available' if path in bindings else 'unbound',
                'revision': 1}

    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=read))
    return {conversation: path for path, conversation in bindings.items()}


@pytest.mark.parametrize('role', ['router', 'coordinator'])
@pytest.mark.parametrize('route', [('openai', COMPATIBLE, 'model-a'),
                                   ('anthropic', SANKUAI_ANTHROPIC, 'claude-opus-5.5')],
                         ids=['compatible', 'example-corp-claude'])
def test_router_and_coordinator_carry_no_mcp_and_grant_workers_search_only_mcp(
        bridge, coordination, role, route):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    protocol, base_url, model = route
    path = coordination[role]
    cfg = {'projectPath': path, '_turnOwnerUserId': OWNER, 'userId': OWNER,
           'memoryEnabled': False, 'plugins': []}
    task = _task(f'{role}-task-{protocol}', cfg, conv_id=role, content='继续')
    tools, available = assemble_round_tools(cfg, task, {
        **_mcfg(path), 'model': model, 'code_exec_enabled': True})
    wire = _wire(task, tools, model=model, protocol=protocol, base_url=base_url)
    context = _context_text(task, wire, project_path=path)

    assert available
    assert _mcp(_names(tools)) == _mcp(_names(wire['tools'])) == []
    assert 'mcp' not in _wire_text(wire).lower()
    assert 'mcp' not in context.lower(), context
    assert _mcp(task.get('_requiredDirectToolNames') or ()) == []
    # The role itself holds no MCP tool; the Worker grant it issues names them
    # all, and a Worker reaches them through search (the Worker test below).
    assert _mcp(_names(task['_executable_tool_catalog'])) == []
    assert _mcp(task['_coordinatorWorkerPolicy'].tools) == MCP_NAMES


def test_worker_grant_listing_mcp_names_exposes_them_only_through_search(
        bridge, workspace, monkeypatch):
    from lib.project_dispatch import admission
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    coordinator = {'id': 'issuing-coordinator', 'config': {}}
    capture_worker_policy(
        coordinator, {}, [{'function': {'name': name}}
                          for name in ('read_files', 'run_command', *MCP_NAMES)],
        'model-a', worker_model='model-a')
    granted = coordinator['_coordinatorWorkerPolicy'].tools
    assert _mcp(granted) == MCP_NAMES
    profile = {'mode': 'project', 'tools': list(granted)}
    monkeypatch.setattr(admission, 'is_dispatch_task',
                        lambda task: task.get('id') == 'granted-worker')
    # A bound project-mode grant pins no coordinator role.
    monkeypatch.setattr(admission, 'granted_coordinator_scope', lambda task: (True, None))
    monkeypatch.setattr(admission, 'validate_task',
                        lambda task, **_: {'assignmentId': 'grant'})
    monkeypatch.setattr(admission, 'repository_for',
                        lambda binding: SimpleNamespace(read=lambda assignment: {}))
    monkeypatch.setattr(admission, 'profile_for', lambda assignment: profile)
    cfg = {'userId': OWNER, 'projectPath': str(workspace), 'plugins': [],
           'memoryEnabled': False, 'schedulerEnabled': False}
    task = _task('granted-worker', cfg, conv_id='granted-worker-conv')

    tools, available = assemble_round_tools(cfg, task, _mcfg(str(workspace)))
    wire = _wire(task, tools)

    assert available
    from lib.project_dispatch.scope import granted_tools
    assert set(_names(task['_executable_tool_catalog'])) <= granted_tools(profile)
    _assert_search_only(task, tools, wire,
                        _context_text(task, wire, project_path=str(workspace)))


def test_swarm_child_with_an_exact_mcp_allowlist_reaches_it_only_through_search(
        bridge, workspace):
    from lib.tasks_pkg.child_invocation import delegated_tool_discovery_policy
    from lib.tasks_pkg.invocation import InvocationPolicy
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    parent = _owner_chat(workspace, 'swarm-parent')
    assemble_round_tools(parent['config'], parent, _mcfg(str(workspace)))
    selected = {'read_files', *MCP_NAMES}
    tools = [copy.deepcopy(tool) for tool in parent['_executable_tool_catalog']
             if _names([tool])[0] in selected]
    # A spawn ``tools`` allowlist names the tools exactly.
    policy = delegated_tool_discovery_policy(parent, tools, _names(tools))
    for field in ('_requiredDirectToolNames', '_frontendSelectedToolNames',
                  '_toolInitialVisibleNames'):
        assert _mcp(policy.get(field) or ()) == [], field
    child = _task('swarm-child', {'userId': OWNER, 'projectPath': str(workspace),
                                  'plugins': [], 'memoryEnabled': False},
                  conv_id=parent['convId'])
    child.update(_parentTaskId=parent['id'], _invocationTools=tools,
                 _invocationToolDiscoveryPolicy=policy,
                 _invocationToolContracts=copy.deepcopy(parent['_toolContractDocumentsByName']),
                 _invocationPolicy=InvocationPolicy.child(parent['id']))

    child_tools, available = assemble_round_tools(child['config'], child, _mcfg(str(workspace)))
    wire = _wire(child, child_tools)

    assert available
    _assert_search_only(child, child_tools, wire,
                        _context_text(child, wire, project_path=str(workspace)))


def _projection_body(*, mcp_count=1, builtins=3, **overrides):
    def tool(name):
        return {'type': 'function', 'function': {
            'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': {}}}}
    visible = [tool(f'builtin_{index}') for index in range(builtins)]
    mcp_tools = [tool(f'mcp__docs__tool_{index}') for index in range(mcp_count)]
    body = {
        'model': 'model-a', 'messages': [{'role': 'user', 'content': 'hello'}],
        'tools': list(visible), '_tool_wire_catalog': list(visible),
        '_executable_tool_catalog': visible + mcp_tools,
        '_tool_discovery_policy_by_name': {
            **{_names([t])[0]: 'eager' for t in visible},
            **{_names([t])[0]: 'searchable' for t in mcp_tools}},
        '_tool_search_catalog_size': len(visible) + len(mcp_tools),
        '_tool_searchable_count': len(mcp_tools), '_tool_search_mode': 'local',
    }
    body.update(overrides)
    return body


_LEAKED_CANDIDATE = {'type': 'function', 'function': {
    'name': 'mcp__docs__tool_0', 'description': 'mcp__docs__tool_0',
    'parameters': {'type': 'object', 'properties': {}}}}


@pytest.mark.parametrize('promotion', [
    {'_frontend_selected_tool_names': ['mcp__docs__tool_0']},
    {'_required_direct_tool_names': ['mcp__docs__tool_0']},
    {'tool_choice': {'type': 'function', 'function': {'name': 'mcp__docs__tool_0'}}},
    {'_tool_search_fail_open': True},
    {'_tool_wire_catalog': [_LEAKED_CANDIDATE]},
    {'_tool_search_mode': 'off', '_tool_wire_catalog': [_LEAKED_CANDIDATE]},
], ids=['pin', 'required-direct', 'named-choice', 'fail-open', 'round-candidate',
        'search-off'])
def test_final_projection_never_promotes_an_mcp_schema(promotion):
    from lib.llm import tool_wire
    body = _projection_body(**promotion)
    tool_wire.project_tool_wire(body, api_protocol='openai', base_url=COMPATIBLE)
    # An empty surface leaves the request without a tools array at all.
    assert _mcp(_names(body.get('tools'))) == []
    # A choice naming a search-only tool would dangle; the model chooses.
    assert 'tool_choice' not in body


@pytest.mark.parametrize('builtins', [0, 1, 3, 20])
def test_any_catalog_holding_an_mcp_tool_keeps_the_discovery_gateway(builtins):
    """A small direct surface still needs search_tools to reach its MCP tools."""
    from lib.llm import tool_wire
    body = _projection_body(builtins=builtins)
    tool_wire.project_tool_wire(body, api_protocol='openai', base_url=COMPATIBLE)
    names = _names(body['tools'])
    assert _mcp(names) == []
    assert {'search_tools', 'execute_tools'} <= set(names)


def test_default_tool_descriptions_carry_no_mcp_text():
    from lib.swarm.tools import SPAWN_AGENTS_TOOL
    from lib.tools.gateway import gateway_tool_schemas
    for schema in [*gateway_tool_schemas(include_program_action=True), SPAWN_AGENTS_TOOL]:
        assert 'mcp' not in json.dumps(schema, ensure_ascii=False).lower(), _names([schema])


def test_no_conversation_or_grant_setting_names_mcp_exposure():
    """Exposure has one owner; settings and grants carry no MCP switch."""
    from pathlib import Path
    from lib.agent_loop.agent_options import TofuOptions
    from lib.conv_config import resolve_conv_config
    root = Path(__file__).resolve().parents[2]
    settings = json.loads((root / 'contracts/conversation_settings_v1.json').read_text())
    dispatch = json.loads((root / 'contracts/project_dispatch_v1.schema.json').read_text())
    grant = dispatch['$defs']['workerPolicy']['properties']['config']['properties']
    resolved = resolve_conv_config({'model': 'model-a', 'mcpToolExposure': 'inline'}, {},
                                   {'serverModel': 'model-a'}, is_active=True)
    assert [key for key in json.dumps(settings).split('"') if 'mcp' in key.lower()] == []
    assert [key for key in grant if 'mcp' in key.lower()] == []
    assert [key for key in resolved if 'mcp' in key.lower()] == []
    # A headless caller may still withhold MCP (``mcp: false``); nothing selects exposure.
    assert [key for key in TofuOptions.openapi_schema()['properties']
            if 'mcp' in key.lower()] == ['mcpEnabled']


# ── Worker grants name MCP servers, not only the tools connected at delegation ──

HOPE_NAMES = [name for name in MCP_NAMES if name.startswith('mcp__hope__')]


def _owner_mcp_config(monkeypatch, rows):
    import lib.mcp.config
    monkeypatch.setattr(lib.mcp.config, 'load_mcp_config', lambda *, user_id: rows)


def _coordinator_policy(bridge, coordination, *, connected, cfg_extra=None):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    bridge.connected = connected
    path = coordination['coordinator']
    cfg = {'projectPath': path, '_turnOwnerUserId': OWNER, 'userId': OWNER,
           'memoryEnabled': False, 'plugins': [], **(cfg_extra or {})}
    task = _task('coordinator-' + uuid_hex(), cfg, conv_id='coordinator', content='继续')
    assemble_round_tools(cfg, task, {**_mcfg(path), 'code_exec_enabled': True})
    return task['_coordinatorWorkerPolicy']


def uuid_hex():
    import uuid
    return uuid.uuid4().hex[:8]


def _granted_worker(monkeypatch, workspace, profile):
    from lib.project_dispatch import admission
    from lib.project_dispatch.scope import validate_profile
    profile = validate_profile({'profile': profile})
    monkeypatch.setattr(admission, 'is_dispatch_task',
                        lambda task: task.get('id', '').startswith('granted-worker'))
    monkeypatch.setattr(admission, 'granted_coordinator_scope', lambda task: (True, None))
    monkeypatch.setattr(admission, 'validate_task',
                        lambda task, **_: {'assignmentId': 'grant'})
    monkeypatch.setattr(admission, 'repository_for',
                        lambda binding: SimpleNamespace(read=lambda assignment: {}))
    monkeypatch.setattr(admission, 'profile_for', lambda assignment: profile)
    from lib.project_dispatch import continuation
    monkeypatch.setattr(continuation, 'current_continuation', lambda task, **_: None)
    cfg = {'userId': OWNER, 'projectPath': str(workspace), 'plugins': [],
           'memoryEnabled': False, 'schedulerEnabled': False}
    return _task('granted-worker-' + uuid_hex(), cfg, conv_id='granted-worker-conv')


def test_coordinator_started_before_mcp_connected_grants_its_workers_the_enabled_servers(
        bridge, coordination, workspace, monkeypatch):
    """Found 2026-11-17 after a restart: a Coordinator whose Turn was assembled
    before MCP auto-connect finished issued a Worker grant of tool names
    without one MCP tool, so its Workers never reached the Hope connector the
    owner had enabled. The grant now names the servers the Coordinator is
    entitled to; a disabled server never joins it."""
    from lib.project_dispatch.delegation import worker_profile
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools, refresh_root_mcp_authority)
    _owner_mcp_config(monkeypatch, {'hope': {'enabled': True}, 'docs': {'enabled': False}})
    policy = _coordinator_policy(bridge, coordination, connected=False)
    assert _mcp(policy.tools) == []
    profile = worker_profile(policy)

    # The Worker starts while MCP is still connecting, then the servers connect:
    # a later round of the same Turn gains the enabled server, never the disabled one.
    worker = _granted_worker(monkeypatch, workspace, profile)
    bridge.connected = False
    assemble_round_tools(worker['config'], worker, _mcfg(str(workspace)))
    assert _mcp(_names(worker['_executable_tool_catalog'])) == []
    bridge.connected = True
    refresh_root_mcp_authority(worker)
    assert _mcp(_names(worker['_executable_tool_catalog'])) == HOPE_NAMES

    # A Worker Turn that starts after the servers connected holds them directly.
    later = _granted_worker(monkeypatch, workspace, profile)
    tools, available = assemble_round_tools(later['config'], later, _mcfg(str(workspace)))
    assert available
    assert _mcp(_names(later['_executable_tool_catalog'])) == HOPE_NAMES
    assert _mcp(_names(tools)) == []


def test_worker_grant_names_connected_and_enabled_servers_only(
        bridge, coordination, monkeypatch):
    from lib.project_dispatch.delegation import worker_profile
    _owner_mcp_config(monkeypatch, {'hope': {'enabled': True}, 'docs': {}})
    connected = worker_profile(_coordinator_policy(bridge, coordination, connected=True))
    assert connected['mcpServers'] == ['docs', 'hope']
    _owner_mcp_config(monkeypatch, {'hope': {'enabled': False}, 'docs': {'enabled': False},
                                    'late': {'enabled': True}})
    disabled = worker_profile(_coordinator_policy(bridge, coordination, connected=False))
    assert disabled['mcpServers'] == ['late']
    # The Coordinator's own conversation withholding MCP grants no server.
    withheld = worker_profile(_coordinator_policy(
        bridge, coordination, connected=False, cfg_extra={'mcpEnabled': False}))
    assert 'mcpServers' not in withheld


def test_granted_mcp_servers_admit_only_their_own_namespaced_tools():
    from lib.project_dispatch.scope import granted_tools, validate_profile
    profile = validate_profile({'profile': {
        'mode': 'project', 'tools': ['read_files'], 'readPaths': ['.'], 'writePaths': ['.'],
        'mcpServers': ['hope']}})
    granted = granted_tools(profile)
    assert 'read_files' in granted and 'mcp__hope__submit_job' in granted
    for refused in ('mcp__docs__read_page', 'mcp__hope', 'mcp__hope__', 'mcp__hopeful__x',
                    'run_command', 'hope'):
        assert refused not in granted, refused
    # Only a project Worker's grant names servers.
    for mode, tools in (('files', ['read_files']), ('coordinator', ['report_to_user'])):
        with pytest.raises(Exception):
            validate_profile({'profile': {'mode': mode, 'tools': tools, 'readPaths': ['.'],
                                          'writePaths': [], 'maxToolCalls': 1,
                                          'mcpServers': ['hope']}})


def test_a_coordinator_continuation_issues_its_original_mcp_servers(monkeypatch):
    """Workers delegated by an automatic continuation keep the servers the
    original request recorded; a grant recorded before servers had none."""
    from lib.project_dispatch import continuation
    from lib.project_dispatch.delegation import capture_worker_policy, worker_profile
    for recorded, expected in ((['hope'], ['hope']), (None, None)):
        controls = {'version': 1, 'model': 'model-a', 'workerModel': 'model-a',
                    'workerTools': ['read_files'], 'tools': ['report_to_user']}
        if recorded is not None:
            controls['workerMcpServers'] = recorded
        monkeypatch.setattr(continuation, 'current_continuation', lambda task, **_: {
            'request': {'delegation': {'policy': {'config': {}, 'continuation': controls}}}})
        task = {'id': 'continuation', 'config': {}}
        capture_worker_policy(task, {}, [{'function': {'name': 'mcp__docs__read_page'}}], 'model-a')
        assert worker_profile(task['_coordinatorWorkerPolicy']).get('mcpServers') == expected
