"""The Rust MCP catalog owners keep the Python oracle's catalog behaviour.

``packages/tofu-mcp`` ports Tool Search (``lib/mcp/tool_search.py`` →
``src/tool_search.rs``), the retired progressive wrappers
(``progressive.py`` → ``src/progressive.rs``), connector guidance
(``connector_guidance.py`` → ``src/connector_guidance.rs``), the registry's
edition filter (``registry.py`` → ``src/registry.rs``) and the bridge's
catalog projections (``client/_bridge.py`` → ``src/bridge/catalog.rs``).

Every case runs the same inputs through both: pure functions directly, the
selection state as identical operation scenarios, and bridge projections from
the same loopback stdio MCP server connected once through each client. The
task/tool-gateway layers that consume the MCP catalog (search-only exposure,
declarations, assembly, the routes summary) run unchanged over a
``RustMCPBridge``: the oracle's own test bodies are called with the Rust client
in place of their fake bridge.

State the oracle keeps elsewhere reaches Rust through binding calls
(``lib/mcp/rust_host.py`` ``DEFAULT_CALLBACKS``): the index capacity, the
durable conversation wire, the registry ``CATALOG`` and owner config rows.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.mcp_protocol.rust_parity_support import (
    ROOT, mcp_host_binary as mcp_host_binary, python_bridge, rust_host as rust_host,
    scene as scene,
)
from tests.goals.test_goal_invocation_integration import runtime as invocation_runtime

__all__ = ['invocation_runtime']

pytestmark = pytest.mark.unit

COVERS: dict[str, list[str]] = {
    'test_mcp_tool_search.py': [
        'test_index_is_content_addressed_like_python',
        'test_valid_fingerprint_hint_bypasses_snapshot_hashing',
        'test_index_cache_is_lru_bounded_by_the_binding_capacity',
        'test_index_capacity_comes_from_the_resource_policy',
        'test_phrase_boost_branches_rank_like_python',
        'test_selection_scenarios_match_python',
        'test_selection_state_expiry_removes_only_the_expired_prefix',
        'test_selection_state_uses_the_host_monotonic_clock',
        'test_selection_state_retains_no_query_content',
        'test_recent_mcp_history_matches_python',
        'test_scope_ids_and_declaration_validation_match_python',
        'test_registry_assembly_uses_the_rust_generation_projection',
        'test_bridge_projection_matches_python_and_refreshes_on_catalog_change',
        'test_bridge_disconnect_invalidates_the_projection',
        'test_pure_catalog_functions_match_python',
        'cargo:src/tool_search.rs::prune_reads_only_the_expired_prefix',
        'cargo:src/tool_search.rs::index_cache_is_lru_bounded_and_content_addressed',
        'cargo:src/tool_search.rs::dependency_precedes_owner_and_wire_freezes',
        'cargo:src/tool_search.rs::cpython_text_semantics',
    ],
    'test_mcp_progressive.py': [
        'test_progressive_search_matches_python',
        'test_progressive_wrappers_enforce_the_safety_annotation_like_python',
        'test_progressive_budget_page_keeps_full_candidates_canonical',
        'test_compact_schema_matches_python',
        'cargo:src/progressive.rs::search_ranks_and_labels_the_wrapper',
        'cargo:src/progressive.rs::budget_page_keeps_the_full_selection_canonical',
    ],
    'test_mcp_connector_guidance.py': [
        'test_connector_search_availability_matches_python',
        'test_no_evidence_and_no_config_read_when_usable_unowned_or_generic',
        'test_unreadable_configuration_makes_no_claim',
        'test_document_links_map_to_their_connector',
        'test_document_link_guidance_follows_install_state',
        'test_fetch_reader_over_rust_guidance',
        'test_tool_search_gateway_relays_rust_setup_guidance',
        'cargo:src/connector_guidance.rs::urls_split_like_urllib',
    ],
    'test_mcp_catalog_generation.py': [
        'test_catalog_generation_advances_exactly_when_the_visible_catalog_changes',
        'test_assembled_catalog_equals_the_python_bridge_catalog',
        'test_unchanged_mcp_round_transfers_no_catalog',
    ],
    'test_mcp_tool_summary.py': [
        'test_enabled_summary_matches_python',
        'test_oracle_tool_summary_cases_on_rust',
    ],
    'test_mcp_wire_tool_order.py': ['test_oracle_wire_tool_order_cases_on_rust'],
    'test_mcp_search_only.py': [
        'test_oracle_owner_chat_search_only_on_rust',
        'test_oracle_second_turn_and_initial_surface_on_rust',
        'test_oracle_router_and_coordinator_on_rust',
        'test_oracle_worker_grant_and_swarm_child_on_rust',
        'test_oracle_coordinator_and_worker_server_grants_on_rust',
    ],
    'test_mcp_search_only_headless.py': ['test_oracle_headless_search_only_on_rust'],
    'test_mcp_declaration_lifecycle.py': [
        'test_oracle_declaration_lifecycle_on_rust',
        'test_oracle_catalog_outage_on_rust',
        'test_oracle_disclosed_loss_on_rust',
    ],
    'test_mcp_transient_assembly.py': [
        'test_oracle_transient_assembly_on_rust',
        'test_oracle_headless_root_delivery_on_rust',
        'test_scope_ids_and_declaration_validation_match_python',
    ],
    'test_mcp_catalog_internal_only.py': [
        'test_opensource_build_hides_internal_entries_like_python',
        'test_registry_hot_reload_reaches_rust',
        'cargo:src/registry.rs::the_edition_follows_the_oracle_spellings',
    ],
    'test_mcp_owner_isolation.py': [
        'test_owner_bridges_are_isolated_and_owner_ids_validated',
        'test_registry_retains_every_owner_and_ignores_the_retired_capacity_setting',
        'test_owner_config_rows_are_read_per_owner',
        'test_mcp_build_fails_closed_without_owner_on_rust',
        'test_resolver_uses_only_the_task_owners_rust_bridge',
        'test_a_busy_owner_never_blocks_another_owner',
    ],
    'test_brand_mascot_single.py': ['test_is_opensource_build_matches_python'],
    'test_audio_chat_has_text_companion.py': ['test_is_opensource_build_matches_python'],
}

OWNER = 7

# A loopback stdio MCP peer serving the tools in a JSON file (re-read on every
# tools/list, so a reconnect sees an edit). ``tools/call`` echoes its
# arguments; an ``id`` of ``sleep:<seconds>`` sleeps first.
LOOP_SERVER = textwrap.dedent(r'''
    import json, sys, time
    path = sys.argv[1]
    def send(request_id, *, result=None, error=None):
        message = {'jsonrpc': '2.0', 'id': request_id}
        message['error' if error is not None else 'result'] = error if error is not None else result
        sys.stdout.write(json.dumps(message, separators=(',', ':')) + '\n')
        sys.stdout.flush()
    for line in sys.stdin:
        request = json.loads(line)
        method, request_id = request.get('method', ''), request.get('id')
        if request_id is None:
            continue
        if method == 'initialize':
            send(request_id, result={'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                                     'serverInfo': {'name': 'loop', 'version': '1.0'}})
        elif method == 'tools/list':
            with open(path, encoding='utf-8') as handle:
                document = json.load(handle)
            send(request_id, result=document)
        elif method == 'tools/call':
            arguments = request['params'].get('arguments') or {}
            if str(arguments.get('id', '')).startswith('sleep:'):
                time.sleep(float(arguments['id'].split(':', 1)[1]))
            send(request_id, result={'isError': False, 'content': [
                {'type': 'text', 'text': 'called ' + request['params']['name'] + ' ' + json.dumps(arguments, sort_keys=True)}]})
        elif method == 'ping':
            send(request_id, result={})
        else:
            send(request_id, error={'code': -32601, 'message': 'Method not found'})
''')


class Servers:
    """Loopback server configurations whose tool lists live in files."""

    def __init__(self, root: Path):
        self.root = root
        self.script = root / 'loop_server.py'
        self.script.write_text(LOOP_SERVER, encoding='utf-8')

    def config(self, name: str, tools: list[dict], *, version: str = '', tag: str = '') -> dict:
        path = self.root / f'{name}{tag}.tools.json'
        self.write(path, tools, version)
        return {'transport': 'stdio', 'command': sys.executable,
                'args': [str(self.script), str(path)], 'enabled': True}

    @staticmethod
    def write(path: Path, tools: list[dict], version: str = '') -> None:
        document = {'tools': tools}
        if version:
            document['_meta'] = {'catalogVersion': version}
        path.write_text(json.dumps(document), encoding='utf-8')


@pytest.fixture
def servers(tmp_path) -> Servers:
    return Servers(tmp_path)


def _tool(name, description='', *, read_only=None, meta=None, schema=None):
    tool = {'name': name, 'description': description or name,
            'inputSchema': schema or {'type': 'object', 'properties': {}}}
    if read_only is not None:
        tool['annotations'] = {'readOnlyHint': read_only}
    if meta:
        tool['_meta'] = meta
    return tool


def _tools_from_rows(rows) -> dict[str, list[dict]]:
    """The tools a server must list for a bridge to project these snapshot rows."""
    from lib.mcp.types import parse_namespaced_name
    by_server: dict[str, list[dict]] = {}
    for row in rows:
        function = row['openai_def']['function']
        server, short = parse_namespaced_name(function['name'])
        description = str(function.get('description') or '').removeprefix(f'[MCP:{server}] ')
        by_server.setdefault(server, []).append(_tool(
            short, description, meta=row.get('meta') or None,
            schema=function.get('parameters') or None,
            read_only=row.get('read_only_hint')))
    return by_server


class OracleFacingBridge:
    """A ``RustMCPBridge`` whose ``connected`` an oracle test may override."""

    def __new__(cls, host, owner):
        from lib.mcp.rust_host import RustMCPBridge

        class _Bridge(RustMCPBridge):
            _connected_override = None

            @property
            def connected(self):
                if self._connected_override is not None:
                    return self._connected_override
                return RustMCPBridge.connected.fget(self)

            @connected.setter
            def connected(self, value):
                self._connected_override = value

            @property
            def _tool_index(self):
                return {name: info for name, info, _ in self.tool_index_rows()}

            def get_frozen_openai_tool_defs(self):
                # An oracle that breaks this instance's discovery read
                # (``live.get_openai_tool_defs = fail``) breaks the frozen read too.
                override = self.__dict__.get('get_openai_tool_defs')
                if override is not None:
                    return -1, tuple(override())
                return RustMCPBridge.get_frozen_openai_tool_defs(self)

        return _Bridge(host, owner)


class RustFactory:
    """Stands in for an oracle's fake-bridge builder with the Rust client."""

    def __init__(self, host, servers: Servers, monkeypatch, owner=OWNER):
        self.host, self.servers, self.monkeypatch, self.owner = host, servers, monkeypatch, owner
        self.count = 0

    def connect(self, by_server: dict[str, list[dict]], *, connected=None, disabled=None):
        """A fresh Rust bridge holding exactly these servers' catalogs."""
        import lib.mcp
        bridge = OracleFacingBridge(self.host, self.owner)
        bridge.disconnect_all()
        self.count += 1
        for server, tools in by_server.items():
            bridge.connect_server(server, self.servers.config(server, tools, tag=f'-{self.count}'))
            if disabled and disabled.get(server):
                bridge.set_disabled_tools(server, disabled[server])
        if connected is not None:
            bridge.connected = connected
        self.monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: bridge)
        return bridge

    def rows(self, monkeypatch, snapshot=None, *, connected=True):
        """The declaration-lifecycle ``bridge(monkeypatch, snapshot, connected=)`` helper."""
        from tests.mcp_protocol.test_mcp_tool_search import _large_snapshot
        rows = _large_snapshot() if snapshot is None else snapshot
        by_server = _tools_from_rows(rows)
        if not by_server and connected:
            by_server = {'empty': []}
        return self.connect(by_server, connected=None if connected else False)


@pytest.fixture
def factory(rust_host, servers, monkeypatch):
    built = RustFactory(rust_host, servers, monkeypatch)
    yield built
    try:
        OracleFacingBridge(rust_host, OWNER).disconnect_all()
    except Exception:  # the host may already be closed by the test
        pass


@pytest.fixture(autouse=True)
def _clear_python_selection_state():
    from lib.mcp import tool_search
    tool_search.clear_tool_search_caches()
    yield
    tool_search.clear_tool_search_caches()


def _names(definitions):
    return [tool['function']['name'] for tool in definitions]


def _error_text(exc) -> str:
    return re.sub(r'^\w+Error: ', '', str(exc))


# ══ tool_search: pure functions ══════════════════════════════════════════════

def _snapshots():
    from tests.mcp_protocol.test_mcp_tool_search import (
        _confirmation_snapshot, _large_snapshot, _row)
    odd = [
        _row('s', 'unicode', description='Straße ﬁle ΣΑΣ K', meta={
            'aliases': 'single-alias', 'intents': ['', ' ', 0, 'Grüße'], 'risk': ' READ ',
            'bundle': 3, 'requires': 'unicode', 'profiles': [1.5, True]}),
        {'openai_def': {'function': None}, 'namespaced_name': 'mcp__x__y', 'description': 'row text'},
        {'openai_def': {'function': {'name': 'not-namespaced'}}},
        {'server_name': 'only', 'namespaced_name': 'mcp__only__name', 'openai_def': {},
         'meta': {'schemaHash': 'sha256:given', 'catalogVersion': 'c9'}},
    ]
    return {'large': _large_snapshot(), 'confirmation': _confirmation_snapshot(), 'odd': odd}


@pytest.mark.parametrize('name', ['large', 'confirmation', 'odd'])
def test_pure_catalog_functions_match_python(rust_host, name):
    from lib.mcp import tool_search
    snapshot = _snapshots()[name]
    assert rust_host.call('tool_search.fingerprint', {'snapshot': snapshot}) == \
        tool_search.catalog_snapshot_fingerprint(snapshot)
    assert rust_host.call('tool_search.search_text', {'snapshot': snapshot}) == \
        tool_search.catalog_search_text_by_name(snapshot)
    for row in snapshot:
        schema = (row.get('openai_def') or {}).get('function') if isinstance(row.get('openai_def'), dict) else row
        assert rust_host.call('tool_search.canonical_schema_hash', {'schema': schema}) == \
            tool_search.canonical_schema_hash(schema)
    for text in ('请编辑学城文档 Xuecheng', 'hope_search.v2:/x--y', 'İstanbul ſtraße Kelvin', '++', ''):
        counts, digest = tool_search._query_profile(text)
        rust = rust_host.call('tool_search.terms', {'text': text})
        assert rust['terms'] == list(tool_search._iter_terms(text))
        assert dict(rust['counts']) == dict(counts) and rust['digest'] == digest.hex()


def _describe_python(index):
    return {
        'fingerprint': index.fingerprint, 'names': [tool.name for tool in index.tools],
        'fallback_names': list(index.fallback_names),
        'max_query_boost_chars': index.max_query_boost_chars,
        'term_postings': {term: [list(row) for row in rows]
                          for term, rows in sorted(index.term_postings.items())},
    }


@pytest.mark.parametrize('name', ['large', 'confirmation', 'odd'])
def test_index_is_content_addressed_like_python(rust_host, name):
    from lib.mcp import tool_search
    snapshot = _snapshots()[name]
    first = rust_host.call('tool_search.build_index', {'snapshot': snapshot})
    second = rust_host.call('tool_search.build_index', {'snapshot': list(reversed(snapshot))})
    assert first['id'] == second['id'] and first['fingerprint'] == second['fingerprint']
    python = tool_search.build_catalog_index(snapshot)
    assert {key: value for key, value in first.items() if key != 'id'} == _describe_python(python)
    assert all('_meta' not in row.get('openai_def', {}) for row in snapshot if isinstance(row.get('openai_def'), dict))
    server = next((tool.server_id for tool in python.tools), 'xuecheng')
    rust_host.call('tool_search.invalidate_server', {'server_id': server})
    rebuilt = rust_host.call('tool_search.build_index', {'snapshot': snapshot})
    assert rebuilt['fingerprint'] == first['fingerprint']
    assert (rebuilt['id'] != first['id']) == bool(python.tools)


def test_valid_fingerprint_hint_bypasses_snapshot_hashing(rust_host):
    from lib.mcp import tool_search
    snapshot = _snapshots()['large']
    for hint in ('A' * 64, ' ' + 'b' * 64 + ' ', 'z' * 64, 'a' * 63, ''):
        python = tool_search.build_catalog_index(snapshot, fingerprint_hint=hint).fingerprint
        rust = rust_host.call('tool_search.build_index', {'snapshot': snapshot, 'fingerprint_hint': hint})
        assert rust['fingerprint'] == python


def test_index_cache_is_lru_bounded_by_the_binding_capacity(rust_host, monkeypatch):
    from lib.mcp import tool_search
    monkeypatch.setattr(tool_search, '_catalog_index_capacity', lambda: 2)

    def versioned(version):
        rows = _snapshots()['large']
        for row in rows:
            row['catalog_version'] = version
        return rows

    def build(version):
        return rust_host.call('tool_search.build_index', {'snapshot': versioned(version)})['id']

    first, second = build('v1'), build('v2')
    assert build('v1') == first
    build('v3')
    stats = rust_host.call('tool_search.stats')
    assert stats['indexes'] == 2 and stats['indexCapacity'] == 2 and stats['indexEvictions'] == 1
    assert build('v2') != second
    python_first = tool_search.build_catalog_index(versioned('v1'))
    tool_search.build_catalog_index(versioned('v2'))
    assert tool_search.build_catalog_index(versioned('v1')) is python_first
    tool_search.build_catalog_index(versioned('v3'))
    assert tool_search.catalog_cache_stats()['indexes'] == stats['indexes']


@pytest.mark.parametrize('term_capacity', [None, 512, 4096])
def test_index_capacity_comes_from_the_resource_policy(rust_host, monkeypatch, term_capacity):
    from lib.mcp import tool_search
    from lib.tools import resource_policy
    if term_capacity is not None:
        monkeypatch.setattr(resource_policy, 'tool_search_term_cache_capacity', lambda: term_capacity)
    assert rust_host.call('tool_search.stats')['indexCapacity'] == \
        tool_search.catalog_cache_stats()['indexCapacity']


# ══ tool_search: selection scenarios ═════════════════════════════════════════

def _scenarios():
    from tests.mcp_protocol.test_mcp_tool_search import (
        _confirmation_snapshot, _large_snapshot, _row)
    from lib.mcp.tool_search import mcp_selection_scope_id as scope_id
    large = _large_snapshot()
    reduced = [row for row in large if row['server_id'] != 'xuecheng']
    grown = large + [_row('late', 'late_tool', description='late arrival')]
    dependency = [
        _row('xuecheng', 'prepare_doc_edit', description='load editable source',
             meta={'bundle': 'edit_document', 'risk': 'read'}),
        _row('xuecheng', 'update_doc', description='update document',
             meta={'bundle': 'edit_document', 'requires': ['prepare_doc_edit'],
                   'intents': ['编辑学城文档'], 'risk': 'write'}),
        *(_row('hope', f'unrelated_{i}', description=f'unrelated tool {i}') for i in range(8)),
    ]
    compiler = [_row('compiler', 'compile', description='build source', meta={
        'intents': ['compile C++ source'], 'risk': 'write'}),
        *(_row('safe', f'read_{i}', meta={'risk': 'read'}) for i in range(8))]
    risks = [
        _row('llm', 'safe_none', meta={'risk': 'none'}), _row('hope', 'safe_read', meta={'risk': 'read'}),
        _row('other', 'unspecified'), _row('llm', 'auth', meta={'risk': 'auth'}),
        _row('llm', 'mutating', meta={'risk': 'mutating'}), _row('hope', 'write', meta={'risk': 'write'}),
        _row('llm', 'destructive', meta={'risk': 'destructive'}),
        _row('hope', 'second_read', meta={'risk': 'readonly'}), _row('other', 'second_unspecified'),
    ]
    confirmation = _confirmation_snapshot()
    conf = scope_id(task_id='confirmation-first', conv_id='conv-confirmation', owner_user_id=7)
    other_owner = scope_id(task_id='x', conv_id='conv-confirmation', owner_user_id=8)
    late = scope_id(task_id='late-connect-task', conv_id='late-connect-conv', owner_user_id=7)
    names_scope = scope_id(task_id='wire-names-task', conv_id='wire-names-conv', owner_user_id=7)
    unknown = scope_id(task_id='never', conv_id='wire-names-never', owner_user_id=7)
    secret = 'private-longform-source-sentence '
    footprint = [scope_id(task_id='', conv_id=f'footprint-{i}', owner_user_id=7) for i in range(5)]

    def sel(snapshot, task_id, query, limit=4, **extra):
        return ('select', snapshot, {'task_id': task_id, 'query': query, 'limit': limit, **extra})

    return {
        'stable_dependencies': [sel(large, 'mcp-edit-task', '请编辑学城文档'),
                                sel(large, 'mcp-edit-task', '请编辑学城文档')],
        'snapshot_order': [sel(large, 'forward-order', '请编辑学城文档'), ('clear',),
                           sel(list(reversed(large)), 'reverse-order', '请编辑学城文档')],
        'dependency_order': [sel(dependency, 'dep-order', '编辑学城文档')],
        'intent_change': [sel(large, 'sticky', 'hope search'), ('record', 'sticky', 'mcp__hope__search', ''),
                          sel(large, 'sticky', '编辑学城文档')],
        'catalog_change': [sel(large, 'freeze', 'hope search'), sel(reduced, 'freeze', 'hope search'),
                           sel(grown, 'freeze', 'late arrival')],
        'empty_wire': [('freeze', late, []), ('names', late),
                       sel(large, 'late-connect-task', 'hope search', selection_scope_id=late)],
        'wire_names': [('names', names_scope),
                       sel(large, 'wire-names-task', 'hope search', selection_scope_id=names_scope),
                       ('names', names_scope), ('names', unknown), ('wire', names_scope)],
        'mark_used': [sel(large, 'order', 'hope utility'), ('record', 'order', 'mcp__hope__tool_7', ''),
                      sel(large, 'order', 'hope utility')],
        'confirmation': [sel(confirmation, 'confirmation-first', 'read xuecheng document', 8,
                             selection_scope_id=conf),
                         ('record', 'confirmation-first', 'mcp__xuecheng__read_doc', conf),
                         sel(confirmation, 'confirmation-second', 'Agreed', 8, selection_scope_id=conf),
                         sel(confirmation, 'x', 'Agreed', 8, selection_scope_id=other_owner)],
        'used_names_lead': [sel(large, 'used', 'hope', 2, used_names=['mcp__hope__login',
                                                                      'mcp__hope__tool_3', 'mcp__gone__x'])],
        'ambiguous_four': [sel(large, 'fallback', '完全不匹配的意图', 8), sel(large, 'fallback', '完全不匹配的意图', 8)],
        'risk_vocabulary': [sel(risks, 'risks', 'no matching intent', 8)],
        'empty_query_fallback': [sel(large, 'precomputed', '', 4)],
        'long_query': [sel(large, 'overlong', 'z' * 64, 4)],
        'punctuation_intent': [sel(compiler, 'punct', '++', 4)],
        'identical_query': [sel(large, 'digest', 'long repeated coding context ' * 400),
                            sel(large, 'digest', 'long repeated coding context ' * 400)],
        'punctuation_variant': [sel(large, 'variant', 'hope search'), sel(large, 'variant', 'hope, search')],
        'small_catalog': [sel(large[:3], 'small', 'anything', 8), ('record_many', 'small-{}', 3, 'mcp__docs__read'),
                          ('used', 'small-0'), ('stats',)],
        'limit_spellings': [sel(large, 'limit-zero', 'hope', 0), sel(large, 'limit-negative', 'hope', -3),
                            sel(large, 'limit-string', 'hope', '3'), sel(large, 'limit-float', 'hope', 2.9)],
        'no_scope': [sel(large, '', 'hope search'), sel(large, '', 'edit document')],
        'state_churn': [sel(large, 'oldest-live-wire', 'hope search'),
                        ('record_many', 'unrelated-wire-{}', 4097, 'mcp__docs__read'),
                        sel(large, 'oldest-live-wire', 'edit document'), ('stats',)],
        'history': [('record_many_names', 'bounded-tools', 4097), ('record', 'bounded-tools', 'mcp__docs__tool_0', ''),
                    ('used', 'bounded-tools')],
        'reclaim': [sel(large, 'pressure', 'hope search'), ('record_many_names', 'pressure', 129),
                    ('reclaim',), ('stats',), sel(large, 'pressure', 'edit document'), ('used', 'pressure')],
        'invalid_record': [('record', 'bad', 'not-namespaced', ''), ('record', '', 'mcp__a__b', ''), ('stats',)],
        'footprint': [*(sel(large, '', 'hope', selection_scope_id=scope) for scope in footprint), ('stats',)],
        'release': [('freeze', 'task:headless-dispose-first', []), ('freeze', 'task:headless-dispose-second', []),
                    ('release', {'id': 'headless-dispose-first', '_userId': 73, '_transientRuntime': True}),
                    ('release', {'id': 'headless-dispose-first', '_userId': 73, '_transientRuntime': True}),
                    ('wire', 'task:headless-dispose-first'), ('wire', 'task:headless-dispose-second'),
                    ('release', {'id': 'headless-dispose-second', '_userId': 73, '_transientRuntime': False}),
                    ('release', {'id': 'headless-dispose-second', '_userId': True, '_transientRuntime': True}),
                    ('release', {'id': 'headless-dispose-second', '_userId': 73, '_transientRuntime': True})],
        'freeze_validation': [('freeze', 'task:empty', []),
                              ('freeze', 'task:nonempty', [{'type': 'function', 'function': {'name': 'mcp__s__read'}}]),
                              ('freeze', 'task:nonempty', [{'type': 'function', 'function': {'name': 'mcp__s__other'}}]),
                              ('freeze', 'task:malformed', [{'type': 'function', 'function': {'name': 'changed'}}]),
                              ('freeze', 'task:duplicate', [{'type': 'function', 'function': {'name': 'mcp__s__a'}}] * 2),
                              ('freeze', 'task:count', [{'type': 'function', 'function': {'name': f'mcp__s__{i}'}}
                                                        for i in range(129)]),
                              ('freeze', 'task:bytes', [{'type': 'function', 'function': {
                                  'name': 'mcp__s__big', 'description': 'é' * 140_000}}]),
                              ('freeze', '', [{'type': 'function', 'function': {'name': 'mcp__s__x'}}]),
                              ('wire', 'conversation:invalid'), ('wire', 'conversation:x7:c'),
                              ('wire', 'conversation: +7:c'), ('wire', '')],
        'secret_query': [sel(large, 'digest-task', secret * 200)],
    }


def _run_python(steps):
    from lib.mcp import tool_search as ts
    out = []
    for step in steps:
        try:
            kind = step[0]
            if kind == 'select':
                out.append(ts.select_active_mcp_tools(step[1], **step[2]))
            elif kind == 'record':
                out.append(ts.record_mcp_tool_used(step[1], step[2], selection_scope_id=step[3]))
            elif kind == 'record_many':
                for index in range(step[2]):
                    ts.record_mcp_tool_used(step[1].format(index), step[3])
                out.append(None)
            elif kind == 'record_many_names':
                for index in range(step[2]):
                    ts.record_mcp_tool_used(step[1], f'mcp__docs__tool_{index}')
                out.append(None)
            elif kind == 'freeze':
                out.append(ts.freeze_wire_definitions(step[1], copy.deepcopy(step[2])))
            elif kind == 'names':
                names, frozen = ts.frozen_wire_tool_names(step[1])
                out.append([list(names), frozen])
            elif kind == 'wire':
                out.append(ts.frozen_wire_definitions(step[1]))
            elif kind == 'used':
                out.append(ts._SELECTION_STATE[step[1]]['used'])
            elif kind == 'stats':
                stats = ts.catalog_cache_stats()
                out.append({key: stats[key] for key in ('indexes', 'tasks')})
            elif kind == 'clear':
                out.append(ts.clear_tool_search_caches())
            elif kind == 'reclaim':
                out.append(ts.reclaim_tool_search_indexes())
            elif kind == 'release':
                out.append(ts.release_transient_task_selection(step[1]))
        except Exception as exc:
            out.append(('error', _error_text(exc)))
    return out


def _run_rust(host, steps):
    from lib.mcp.rust_host import RustHostError
    out = []
    for step in steps:
        try:
            kind = step[0]
            if kind == 'select':
                out.append(host.call('tool_search.select', {'snapshot': step[1], **step[2]}))
            elif kind == 'record':
                out.append(host.call('tool_search.record_used', {
                    'task_id': step[1], 'name': step[2], 'selection_scope_id': step[3]}))
            elif kind == 'record_many':
                for index in range(step[2]):
                    host.call('tool_search.record_used', {'task_id': step[1].format(index), 'name': step[3]})
                out.append(None)
            elif kind == 'record_many_names':
                for index in range(step[2]):
                    host.call('tool_search.record_used', {'task_id': step[1], 'name': f'mcp__docs__tool_{index}'})
                out.append(None)
            elif kind == 'freeze':
                out.append(host.call('tool_search.freeze', {'selection_scope_id': step[1], 'definitions': step[2]}))
            elif kind == 'names':
                out.append(host.call('tool_search.frozen_names', {'selection_scope_id': step[1]}))
            elif kind == 'wire':
                out.append(host.call('tool_search.frozen_wire', {'selection_scope_id': step[1]}))
            elif kind == 'used':
                state = next(s for s in host.call('tool_search.states') if s['key'] == step[1])
                out.append(state['used'])
            elif kind == 'stats':
                stats = host.call('tool_search.stats')
                out.append({key: stats[key] for key in ('indexes', 'tasks')})
            elif kind == 'clear':
                out.append(host.call('tool_search.clear'))
            elif kind == 'reclaim':
                out.append(host.call('tool_search.reclaim'))
            elif kind == 'release':
                out.append(host.call('tool_search.release_transient', {'task': step[1]}))
        except RustHostError as exc:
            out.append(('error', _error_text(exc)))
    return out


@pytest.mark.parametrize('scenario', sorted(_scenarios()))
def test_selection_scenarios_match_python(rust_host, declaration_authority, scenario):
    steps = _scenarios()[scenario]
    python = _run_python(steps)
    durable_python = copy.deepcopy(declaration_authority.settings)
    declaration_authority.settings.clear()
    rust = _run_rust(rust_host, steps)
    assert json.loads(json.dumps(rust)) == json.loads(json.dumps(python))
    assert declaration_authority.settings == durable_python
    if scenario == 'confirmation':
        assert all(name.startswith('mcp__xuecheng__') for name in _names(rust[0]))
        assert _names(rust[2]) == _names(rust[0])
        assert any(name.startswith('mcp__12306-train__') for name in _names(rust[3]))
    if scenario == 'punctuation_intent':
        assert _names(rust[0]) == ['mcp__compiler__compile']
    if scenario == 'stable_dependencies':
        names = _names(rust[0])
        assert names.index('mcp__xuecheng__prepare_doc_edit') < names.index('mcp__xuecheng__update_doc')
    if scenario == 'empty_wire':
        assert rust == [[], [[], True], []]


@pytest.mark.parametrize('query', ['request', 'hope', 'Hope', 'xuecheng', '学城', 'Xuecheng',
                                   'update_doc', 'mcp__hope__search', 'search', '编辑学城文档',
                                   'utility 3', 'read', '++', 'tool', '  hope  '])
def test_phrase_boost_branches_rank_like_python(rust_host, query):
    """Sparse (posting candidates) and dense (whole catalog) boost scans, via selection."""
    from lib.mcp import tool_search
    snapshot = _snapshots()['large']
    task = f'boost-{query}'
    python = tool_search.select_active_mcp_tools(snapshot, task_id=task, query=query, limit=4)
    rust = rust_host.call('tool_search.select', {'snapshot': snapshot, 'task_id': task,
                                                 'query': query, 'limit': 4})
    assert rust == python


def test_selection_state_expiry_removes_only_the_expired_prefix(rust_host):
    from lib.mcp import tool_search
    now, ttl = 100_000.0, tool_search._STATE_TTL_SECONDS
    for key, touched in (('expired-oldest', now - ttl - 2), ('expired-newer', now - ttl - 1),
                         ('live-boundary', now - ttl), ('live-newest', now - 1)):
        rust_host.call('tool_search.record_used', {'task_id': key, 'name': 'mcp__docs__read', 'now': touched})
        tool_search._SELECTION_STATE[key] = {'used': ['mcp__docs__read'], 'touched': touched}
    before = tool_search.catalog_cache_stats()['stateExpirations']
    rust_host.call('tool_search.prune', {'now': now})
    tool_search._prune_states(now)
    assert [state['key'] for state in rust_host.call('tool_search.states')] == \
        list(tool_search._SELECTION_STATE) == ['live-boundary', 'live-newest']
    assert rust_host.call('tool_search.stats')['stateExpirations'] == \
        tool_search.catalog_cache_stats()['stateExpirations'] - before == 2


def test_selection_state_uses_the_host_monotonic_clock(rust_host):
    snapshot = _snapshots()['large']
    rust_host.call('tool_search.record_used', {'task_id': 'record-clock', 'name': 'mcp__docs__read',
                                               'now': 10_000.0})
    rust_host.call('tool_search.select', {'snapshot': snapshot, 'task_id': 'select-clock',
                                          'query': 'search', 'limit': 4, 'now': 20_000.0})
    states = {state['key']: state for state in rust_host.call('tool_search.states')}
    assert states['record-clock']['touched'] == 10_000.0
    assert states['select-clock']['touched'] == 20_000.0
    low = rust_host.call('tool_search.monotonic')
    rust_host.call('tool_search.record_used', {'task_id': 'live-clock', 'name': 'mcp__docs__read'})
    high = rust_host.call('tool_search.monotonic')
    touched = next(s['touched'] for s in rust_host.call('tool_search.states') if s['key'] == 'live-clock')
    assert low <= touched <= high


def test_selection_state_retains_no_query_content(rust_host):
    secret = 'private-longform-source-sentence'
    rust_host.call('tool_search.select', {'snapshot': _snapshots()['large'], 'task_id': 'digest-task',
                                          'query': (secret + ' ') * 200, 'limit': 4})
    states = rust_host.call('tool_search.states')
    assert [state['key'] for state in states] == ['digest-task']
    assert secret not in json.dumps(states)


@pytest.mark.parametrize('limit', [8, 1, 0, 20])
def test_recent_mcp_history_matches_python(rust_host, limit):
    from lib.mcp import tool_search
    histories = [
        [{'role': 'user', 'content': 'Read the Xuecheng document'},
         {'role': 'assistant', 'content': 'Authorization is required.', 'toolRounds': [
             {'toolName': 'mcp__xuecheng__read_doc'}, {'toolName': 'mcp__xuecheng__login'},
             {'toolName': 'mcp__xuecheng__read_doc'}]},
         {'role': 'user', 'content': 'Agreed'}],
        [{'role': 'user', 'content': 'old'},
         {'role': 'assistant', 'toolRounds': [{'toolName': 'mcp__old__x'}]},
         {'role': 'user', 'content': 'a'},
         {'role': 'assistant', 'tool_calls': [{'function': {'name': 'mcp__a__one'}},
                                               {'function': {'name': 'read_files'}}, 'junk'],
          'apiRounds': [{'toolCalls': [{'tool_name': 'mcp__a__two'}, {'name': 'mcp__a__three'}]}, 3],
          'content': [{'type': 'tool_use', 'name': 'mcp__b__four'}, {'type': 'text', 'name': 'mcp__b__x'},
                      {'type': 'function_call', 'name': 'mcp__b__five'}]},
         'not a message',
         {'role': 'tool', 'toolRounds': [{'toolName': 'mcp__c__six'}, {'toolName': 'mcp__c__seven'},
                                         {'toolName': 'mcp__c__eight'}, {'toolName': 'mcp__c__nine'}]},
         {'role': 'user', 'content': 'b'}],
        [{'role': 'user', 'content': 'only one'}],
        None,
    ]
    for messages in histories:
        assert rust_host.call('tool_search.recent_names', {'messages': messages, 'limit': limit}) == \
            tool_search.recent_conversation_mcp_tool_names(messages, limit=limit)


def test_scope_ids_and_declaration_validation_match_python(rust_host, monkeypatch):
    """Storage-free selectors never read the declaration store (transient assembly)."""
    from lib.mcp import tool_search
    from lib.mcp.rust_host import RustHostError
    cases = [dict(task_id='headless-unique', conv_id='named-conversation', owner_user_id=73, storage_free=True),
             dict(task_id='root', conv_id='named-conversation', owner_user_id=73),
             dict(task_id='', conv_id='  spaced  ', owner_user_id=0),
             dict(task_id='', conv_id='', owner_user_id=None),
             dict(task_id='t', conv_id='c', owner_user_id='9', storage_free=False)]
    for case in cases:
        rust_case = {('owner' if key == 'owner_user_id' else key): value for key, value in case.items()}
        assert rust_host.call('tool_search.scope_id', rust_case) == tool_search.mcp_selection_scope_id(**case)
    with pytest.raises(ValueError, match='explicit'):
        tool_search.mcp_selection_scope_id(task_id='t', storage_free=1)
    with pytest.raises(RustHostError, match='explicit'):
        rust_host.call('tool_search.scope_id', {'task_id': 't', 'storage_free': 1})
    reads = []
    rust_host.callbacks['mcp.declarations_get'] = lambda params: reads.append(params)
    scope = tool_search.mcp_selection_scope_id(task_id='headless-unique', conv_id='named', owner_user_id=73,
                                               storage_free=True)
    assert rust_host.call('tool_search.frozen_wire', {'selection_scope_id': scope}) is None
    assert rust_host.call('tool_search.freeze', {'selection_scope_id': 'task:transient', 'definitions': []}) == []
    assert reads == []
    rust_host.call('tool_search.frozen_wire', {'selection_scope_id': 'conversation:73:named'})
    assert reads == [{'user_id': 73, 'conversation_id': 'named'}]


# ══ catalog projections over a real server ═══════════════════════════════════

def _catalog_tools(version_tag=''):
    return [
        _tool('read_doc', 'Read a Xuecheng document' + version_tag, read_only=True,
              meta={'aliases': ['学城'], 'bundle': 'docs', 'intents': ['读文档']}),
        _tool('update_doc', 'Update a document', read_only=False,
              meta={'requires': ['read_doc'], 'risk': 'write'},
              schema={'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string'}}}),
        _tool('list_spaces', 'List spaces'),
    ]


class Pair:
    """The same loopback servers connected through the Python and the Rust bridge."""

    def __init__(self, host, servers, monkeypatch, owner=OWNER):
        from lib.mcp.rust_host import RustMCPBridge
        self.servers, self.host = servers, host
        self.py = python_bridge(monkeypatch, owner)
        self.rs = RustMCPBridge(host, owner)

    def connect(self, name, tools, *, version=''):
        self.py.connect_server(name, self.servers.config(name, tools, version=version, tag='-py'))
        self.rs.connect_server(name, self.servers.config(name, tools, version=version, tag='-rs'))

    def close(self):
        self.py.disconnect_all()
        self.rs.disconnect_all()


@pytest.fixture
def pair(rust_host, servers, monkeypatch):
    built = Pair(rust_host, servers, monkeypatch)
    yield built
    built.close()


def _assert_same_catalog(pair):
    py_fp, py_rows = pair.py.get_tool_catalog_projection()
    rs_fp, rs_rows = pair.rs.get_tool_catalog_projection()
    assert json.loads(json.dumps(list(rs_rows))) == json.loads(json.dumps(list(py_rows)))
    assert rs_fp == py_fp
    assert pair.rs.get_tool_catalog_snapshot() == json.loads(json.dumps(pair.py.get_tool_catalog_snapshot()))
    assert dict(pair.rs.get_tool_catalog_search_text_projection()) == \
        dict(pair.py.get_tool_catalog_search_text_projection())
    assert pair.rs.get_openai_tool_defs() == json.loads(json.dumps(pair.py.get_openai_tool_defs()))
    assert list(pair.rs.get_frozen_openai_tool_defs()[1]) == \
        json.loads(json.dumps(list(pair.py.get_frozen_openai_tool_defs()[1])))
    assert pair.rs.get_tool_safety() == pair.py.get_tool_safety()
    assert pair.rs.get_enabled_tool_summary() == pair.py.get_enabled_tool_summary()
    assert (pair.rs.server_count, pair.rs.tool_count, pair.rs.connected) == \
        (pair.py.server_count, pair.py.tool_count, pair.py.connected)
    for name in ('mcp__docs__read_doc', 'mcp__docs__missing', 'read_doc'):
        assert pair.rs.is_mcp_tool(name) == pair.py.is_mcp_tool(name)


def test_bridge_projection_matches_python_and_refreshes_on_catalog_change(pair):
    from lib.mcp import tool_search
    pair.connect('docs', _catalog_tools(), version='catalog-1')
    pair.connect('mail', [_tool('send', 'Send mail')])
    _assert_same_catalog(pair)
    fingerprint, projection = pair.rs.get_tool_catalog_projection()
    again_fingerprint, again = pair.rs.get_tool_catalog_projection()
    assert again is projection and again_fingerprint == fingerprint
    search_text = pair.rs.get_tool_catalog_search_text_projection()
    assert pair.rs.get_tool_catalog_search_text_projection() is search_text
    with pytest.raises(TypeError):
        search_text['mcp__docs__read_doc'] = 'mutated'
    snapshot = pair.rs.get_tool_catalog_snapshot()
    assert dict(search_text) == tool_search.catalog_search_text_by_name(snapshot)
    assert {row['catalog_version'] for row in snapshot if row['server_name'] == 'docs'} == {'catalog-1'}
    assert all(row['schema_hash'].startswith('sha256:') and 'meta' not in row['openai_def'] for row in snapshot)
    snapshot[0]['tool_name'] = 'mutated-public-copy'
    assert pair.rs.get_tool_catalog_projection()[1][0]['tool_name'] != 'mutated-public-copy'

    # A changed catalog (a refresh after tools/list_changed) rebuilds every projection.
    Servers.write(pair.servers.root / 'docs-py.tools.json', _catalog_tools(' v2'), 'catalog-2')
    Servers.write(pair.servers.root / 'docs-rs.tools.json', _catalog_tools(' v2'), 'catalog-2')
    pair.rs._host.call('reconnect_server', pair.rs._params(name='docs'))
    pair.py._disconnect_one('docs')
    pair.py.connect_server('docs', pair.servers.config('docs', _catalog_tools(' v2'), version='catalog-2', tag='-py'))
    refreshed_fingerprint, refreshed = pair.rs.get_tool_catalog_projection()
    assert refreshed_fingerprint != fingerprint and refreshed is not projection
    assert pair.rs.get_tool_catalog_search_text_projection() is not search_text
    assert {row['catalog_version'] for row in refreshed if row['server_name'] == 'docs'} == {'catalog-2'}
    _assert_same_catalog(pair)
    for name in ('docs', 'mail'):
        pair.rs.set_disabled_tools(name, ['read_doc', 'send'])
        pair.py.set_disabled_tools(name, ['read_doc', 'send'])
    _assert_same_catalog(pair)


def test_bridge_disconnect_invalidates_the_projection(pair):
    pair.connect('docs', _catalog_tools())
    fingerprint, projection = pair.rs.get_tool_catalog_projection()
    pair.rs._host.call('disconnect_server', pair.rs._params(name='docs'))
    pair.py._disconnect_one('docs')
    empty_fingerprint, empty = pair.rs.get_tool_catalog_projection()
    assert projection and not empty and empty_fingerprint != fingerprint
    assert empty_fingerprint == pair.py.get_tool_catalog_projection()[0]


def test_catalog_generation_advances_exactly_when_the_visible_catalog_changes(pair):
    pair.connect('docs', [_tool('read'), _tool('search')])
    pair.connect('mail', [_tool('send')])
    generation, frozen = pair.rs.get_frozen_openai_tool_defs()
    assert pair.rs.get_frozen_openai_tool_defs() == (generation, frozen)
    assert pair.rs.get_frozen_openai_tool_defs()[1] is frozen
    assert pair.rs.tool_catalog_generation() == generation
    # Waking a parked server that rediscovers the same catalog (another order)
    # is not a change: the handle keeps its catalog fingerprint.
    Servers.write(pair.servers.root / 'docs-rs.tools.json', [_tool('search'), _tool('read')])
    pair.rs._host.call('park', pair.rs._params(name='docs'))
    pair.rs._host.call('reconnect_server', pair.rs._params(name='docs'))
    assert pair.rs.get_frozen_openai_tool_defs()[0] == generation
    # A full reconnect drops the server's rows first, like the oracle's.
    pair.rs._host.call('reconnect_server', pair.rs._params(name='docs'))
    generation = pair.rs.get_frozen_openai_tool_defs()[0]
    # A changed tool list is.
    Servers.write(pair.servers.root / 'docs-rs.tools.json', [_tool('read'), _tool('search', 'v2')])
    pair.rs._host.call('reconnect_server', pair.rs._params(name='docs'))
    changed, frozen_changed = pair.rs.get_frozen_openai_tool_defs()
    assert changed > generation
    assert [tool['function']['description'] for tool in frozen_changed
            if tool['function']['name'] == 'mcp__docs__search'] == ['[MCP:docs] v2']
    # A grant change is; repeating the same grant is not.
    pair.rs.set_disabled_tools('docs', ['read'])
    granted, frozen_granted = pair.rs.get_frozen_openai_tool_defs()
    assert granted > changed and 'mcp__docs__read' not in set(_names(frozen_granted))
    pair.rs.set_disabled_tools('docs', ['read'])
    assert pair.rs.get_frozen_openai_tool_defs()[0] == granted
    # A disconnect that removes tools is.
    pair.rs._host.call('disconnect_server', pair.rs._params(name='mail'))
    disconnected, frozen_disconnected = pair.rs.get_frozen_openai_tool_defs()
    assert disconnected > granted and set(_names(frozen_disconnected)) == {'mcp__docs__search'}


def test_assembled_catalog_equals_the_python_bridge_catalog(pair, monkeypatch):
    import lib.mcp
    from lib.tools.registry import _build
    pair.connect('docs', _catalog_tools())
    pair.connect('mail', [_tool('send', 'Send mail', read_only=True)])
    published = []
    for bridge in (pair.py, pair.rs):
        monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner, bridge=bridge: bridge)
        ctx = SimpleNamespace(cfg={}, tid='t', task_id='t', conv_id='', owner_user_id=OWNER, messages=[])
        assert _build._build_mcp(ctx) == []
        published.append(json.loads(json.dumps({key: dict(value) if key == '_mcpToolSearchTextByName' else value
                                                for key, value in ctx.cfg.items()})))
    assert published[1] == published[0]
    assert published[1]['_mcpAllowedToolCatalog'] == pair.rs.get_openai_tool_defs()


def test_registry_assembly_uses_the_rust_generation_projection(pair, monkeypatch):
    import lib.mcp
    from lib.tools.registry import _build
    pair.connect('docs', _catalog_tools())
    monkeypatch.setattr(pair.rs, 'get_tool_catalog_snapshot',
                        lambda: pytest.fail('the cached projection must bypass public snapshot copies'))
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: pair.rs)
    cfg = {'mcpEnabled': True}
    ctx = SimpleNamespace(cfg=cfg, tid='projection', task_id='projection', conv_id='c', owner_user_id=OWNER,
                          messages=[{'role': 'user', 'content': '编辑学城文档'}])
    assert _build._build_mcp(ctx) == []
    assert cfg['_mcpToolSearchTextByName'] is pair.rs.get_tool_catalog_search_text_projection()
    assert _names(cfg['_mcpAllowedToolCatalog']) == ['mcp__docs__list_spaces', 'mcp__docs__read_doc',
                                                      'mcp__docs__update_doc']
    assert cfg['_mcpToolReadOnlyByName']['mcp__docs__read_doc'] is True


def test_unchanged_mcp_round_transfers_no_catalog(pair, monkeypatch):
    """Rust counterpart of 'no catalog deep copy': an unchanged round moves no definitions."""
    import lib.mcp
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools, refresh_root_mcp_authority)
    from tests.mcp_protocol.test_mcp_catalog_generation import _mcp_catalog, _root_task
    pair.connect('docs', [_tool('read'), _tool('search')])
    pair.connect('mail', [_tool('send')])
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: pair.rs)
    transfers = []
    call = pair.rs._host.call

    def counting(method, params=None, timeout=None):
        answer = call(method, params, timeout)
        if method == 'catalog.frozen_defs' and 'defs' in answer:
            transfers.append(answer['generation'])
        return answer

    monkeypatch.setattr(pair.rs._host, 'call', counting)
    task, cfg, mcfg = _root_task()
    assemble_round_tools(cfg, task, mcfg)
    baseline = json.dumps(_mcp_catalog(task))
    assert set(_names(_mcp_catalog(task))) == {'mcp__docs__read', 'mcp__docs__search', 'mcp__mail__send'}
    transfers.clear()
    for _ in range(3):
        refresh_root_mcp_authority(task)
        assert json.dumps(_mcp_catalog(task)) == baseline
    assert transfers == []
    pair.rs.set_disabled_tools('docs', ['search'])
    refresh_root_mcp_authority(task)
    assert len(transfers) == 1
    assert set(_names(_mcp_catalog(task))) == {'mcp__docs__read', 'mcp__mail__send'}
    pair.rs.set_disabled_tools('docs', [])
    refresh_root_mcp_authority(task)
    refresh_root_mcp_authority(task)
    assert len(transfers) == 2 and json.dumps(_mcp_catalog(task)) == baseline


# ══ tool summary / wire order (oracle bodies over the Rust bridge) ═══════════

def test_enabled_summary_matches_python(pair):
    pair.connect('zeta', [_tool('two'), _tool('one')])
    pair.connect('alpha', [_tool('disabled'), _tool('enabled')])
    pair.connect('empty', [])
    for bridge in (pair.py, pair.rs):
        bridge.set_disabled_tools('alpha', ['disabled', 'no-longer-discovered'])
    assert pair.rs.get_enabled_tool_summary() == pair.py.get_enabled_tool_summary() == {
        'servers': [{'name': 'alpha', 'count': 1}, {'name': 'zeta', 'count': 2}], 'total': 3}


def test_oracle_tool_summary_cases_on_rust(factory, monkeypatch):
    """The oracle's bodies with its fake bridge built as a Rust bridge.

    A tool row whose server is not live (the oracle's ``ghost``) cannot exist
    in the Rust index: disconnecting a server removes its rows.
    """
    from tests.mcp_protocol import test_mcp_tool_summary as oracle
    from lib.mcp.rust_host import RustMCPBridge, RustMCPHost

    def build(*, servers, tools, disabled=None):
        by_server = {name: [] for name in servers}
        for server, name in tools:
            if server in by_server:
                by_server[server].append(_tool(name, 'description-' + 'x' * 256, schema={
                    'type': 'object', 'description': 'y' * 512, 'properties': {}}))
        return factory.connect(by_server, disabled=disabled)

    monkeypatch.setattr(oracle, '_bridge', build)
    oracle.test_enabled_summary_is_sorted_exact_and_omits_non_live_or_zero_servers()
    oracle.test_240_tool_schema_inventory_collapses_below_one_percent_on_the_wire()
    from routes.api_v1.mcp import _enabled_tool_summary
    closed = RustMCPHost(factory.host._proc.args[0], env=dict(os.environ))
    closed.close()
    assert _enabled_tool_summary(RustMCPBridge(closed, OWNER)) == {'servers': [], 'total': 0}


def test_oracle_wire_tool_order_cases_on_rust(factory, monkeypatch):
    from tests.mcp_protocol import test_mcp_wire_tool_order as oracle
    monkeypatch.setattr(oracle, '_bridge_with', lambda order: factory.connect(
        {'srv': [_tool(name, 'd') for name in order]}))
    oracle.test_authority_catalog_is_sorted(monkeypatch)
    oracle.test_reconnect_reorder_does_not_change_the_authority(monkeypatch)
    oracle.test_the_fixture_orders_really_differ()
    oracle.test_assembly_consumes_the_sorted_helper(monkeypatch)
    oracle.test_per_server_handle_order_is_not_what_ships(monkeypatch)


# ══ search-only exposure (oracle bodies over the Rust bridge) ════════════════

@pytest.fixture
def search_only_bridge(factory):
    from lib.tasks_pkg.context_composer import _providers
    from lib.tools.routing import _reset_sticky_exposure_for_tests
    from tests.mcp_protocol.test_mcp_search_only import CATALOG
    bridge = factory.connect({server: [_tool(tool, description, read_only=True, schema={
        'type': 'object', 'properties': {'id': {'type': 'string'}}}) for tool, description in tools]
        for server, tools in CATALOG.items()})
    _providers._reset_tail_transitions_for_tests()
    _reset_sticky_exposure_for_tests()
    yield bridge
    _reset_sticky_exposure_for_tests()


@pytest.fixture
def workspace(tmp_path_factory):
    return tmp_path_factory.mktemp('workspace')


@pytest.fixture
def coordination(workspace, monkeypatch):
    """The search-only oracle's Global Router / Coordinator bindings."""
    from lib.conversations import project_coordination
    from lib.router.home import router_home_location
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(workspace / 'router'))
    project = workspace / 'project'
    project.mkdir()
    bindings = {router_home_location(OWNER): 'router', str(project): 'coordinator'}

    def read(path):
        return {'projectPath': path, 'conversationId': bindings.get(path, ''),
                'availability': 'available' if path in bindings else 'unbound', 'revision': 1}

    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=read))
    return {conversation: path for path, conversation in bindings.items()}


def test_oracle_owner_chat_search_only_on_rust(search_only_bridge, workspace, coordination):
    from tests.mcp_protocol import test_mcp_search_only as oracle
    for extra in oracle.FORMER_EXCEPTIONS:
        oracle.test_owner_chat_exposes_mcp_only_through_search_whatever_the_settings(
            search_only_bridge, workspace, extra)


def test_oracle_second_turn_and_initial_surface_on_rust(search_only_bridge, workspace, coordination):
    from tests.mcp_protocol import test_mcp_search_only as oracle
    oracle.test_a_second_turn_still_lists_no_mcp_tool(search_only_bridge, workspace)
    oracle.test_initial_surface_drops_an_mcp_candidate_without_recorded_visibility(
        search_only_bridge, workspace)


def test_oracle_router_and_coordinator_on_rust(search_only_bridge, coordination):
    from tests.mcp_protocol import test_mcp_search_only as oracle
    for role in ('router', 'coordinator'):
        for route in (('openai', oracle.COMPATIBLE, 'model-a'),
                      ('anthropic', oracle.SANKUAI_ANTHROPIC, 'claude-opus-5.5')):
            oracle.test_router_and_coordinator_carry_no_mcp_and_grant_workers_search_only_mcp(
                search_only_bridge, coordination, role, route)


def test_oracle_worker_grant_and_swarm_child_on_rust(search_only_bridge, workspace, coordination, monkeypatch):
    from tests.mcp_protocol import test_mcp_search_only as oracle
    oracle.test_worker_grant_listing_mcp_names_exposes_them_only_through_search(
        search_only_bridge, workspace, monkeypatch)
    oracle.test_swarm_child_with_an_exact_mcp_allowlist_reaches_it_only_through_search(
        search_only_bridge, workspace)


def test_oracle_coordinator_and_worker_server_grants_on_rust(search_only_bridge, coordination, workspace,
                                                           monkeypatch):
    from tests.mcp_protocol import test_mcp_search_only as oracle
    oracle.test_coordinator_started_before_mcp_connected_grants_its_workers_the_enabled_servers(
        search_only_bridge, coordination, workspace, monkeypatch)
    search_only_bridge.connected = None
    oracle.test_worker_grant_names_connected_and_enabled_servers_only(
        search_only_bridge, coordination, monkeypatch)


def test_oracle_headless_search_only_on_rust(rust_host, servers, monkeypatch, tmp_path_factory):
    """The headless runtime's owner bridge is the Rust client (``client._bridges``)."""
    import lib.mcp.client as client
    from lib.mcp.rust_host import RustMCPBridge
    from tests.mcp_protocol import test_mcp_search_only_headless as oracle
    script = servers.root / 'weather_server.py'
    script.write_text(oracle.LOOPBACK_MCP_SERVER, encoding='utf-8')
    bridge = RustMCPBridge(rust_host, oracle.OWNER)
    assert bridge.connect_server(oracle.SERVER, {'transport': 'stdio', 'command': sys.executable,
                                                 'args': [str(script)], 'enabled': True}) == ['weather_forecast']
    called = []
    original = bridge.call_tool

    def call_tool(name, arguments, **options):
        called.append(name)
        return original(name, arguments, **options)

    bridge.call_tool = call_tool
    monkeypatch.setitem(client._bridges, oracle.OWNER, bridge)
    try:
        for config in ({'tools': '*'}, {'tools': '*', 'responses': {'promptProfile': 'lean'}}):
            oracle.test_headless_request_is_mcp_clean_and_reaches_mcp_through_search(
                bridge, monkeypatch, tmp_path_factory, config)
    finally:
        bridge.disconnect_all()
    assert called == [oracle.TOOL, oracle.TOOL]


# ══ declarations / transient assembly (oracle bodies over the Rust bridge) ═══

@pytest.fixture
def lifecycle_oracle(factory, monkeypatch):
    from tests.mcp_protocol import test_mcp_declaration_lifecycle as lifecycle
    from tests.mcp_protocol import test_mcp_transient_assembly as transient
    monkeypatch.setattr(lifecycle, 'bridge', factory.rows)
    monkeypatch.setattr(transient, 'bridge', factory.rows)
    # The oracles compare the published authority with the snapshot's row
    # order; a real bridge (either client) publishes it sorted by name.
    monkeypatch.setattr(lifecycle, '_large_snapshot', _sorted_large_snapshot)
    monkeypatch.setattr(transient, '_large_snapshot', _sorted_large_snapshot)
    return lifecycle


def _sorted_large_snapshot():
    from tests.mcp_protocol.test_mcp_tool_search import _large_snapshot
    return sorted(_large_snapshot(), key=lambda row: row['namespaced_name'])


def test_oracle_declaration_lifecycle_on_rust(lifecycle_oracle, monkeypatch, declaration_authority):
    oracle = lifecycle_oracle
    for connected in (False, True):
        oracle.test_disconnect_or_empty_catalog_clears_authority_and_declares_nothing(monkeypatch, connected)
    oracle.test_explicit_empty_survives_clear_and_late_connection(monkeypatch)
    oracle.test_assembly_never_consults_selection_or_durable_declarations(monkeypatch, declaration_authority)
    oracle.test_root_request_refresh_recovers_startup_mcp_without_widening(monkeypatch)
    oracle.test_root_refresh_admits_an_enabled_server_that_connects_after_startup(monkeypatch)
    oracle.test_dispatched_worker_refresh_regains_granted_mcp_tools_only(monkeypatch)


@pytest.mark.parametrize('outage', ['get_openai_tool_defs', 'get_tool_catalog_projection'])
def test_oracle_catalog_outage_on_rust(lifecycle_oracle, monkeypatch, declaration_authority, outage):
    def inject(live):
        def fail():
            raise OSError('catalog unavailable')
        monkeypatch.setattr(live, outage, fail, raising=False)

    lifecycle_oracle.test_catalog_outage_keeps_baseline_without_mcp_grants(
        monkeypatch, inject, declaration_authority)
    lifecycle_oracle.test_catalog_exception_drops_grants_without_raising(monkeypatch, inject)


@pytest.mark.parametrize('loss', ['disconnect', 'revoke', 'outage'])
def test_oracle_disclosed_loss_on_rust(lifecycle_oracle, monkeypatch, loss):
    lifecycle_oracle.test_disclosed_mcp_loss_is_unavailable_not_hallucinated_and_recovers(monkeypatch, loss)


@pytest.fixture
def transient_assembly(lifecycle_oracle, monkeypatch):
    from lib.mcp import tool_search
    from lib.storage.mcp_declarations import MCPDeclarationStore
    tool_search.clear_tool_search_caches()
    lifecycle_oracle.bridge(monkeypatch, _sorted_large_snapshot()[:2])
    accesses = []

    class MissingConversation:
        def query(self, operation, payload):
            accesses.append((operation, payload))
            return None

    monkeypatch.setattr(MCPDeclarationStore, '_storage', lambda self: MissingConversation())
    cfg = {'userId': 7, 'model': 'test', 'memoryEnabled': False, 'schedulerEnabled': False,
           'preferencesEnabled': False, 'plugins': []}
    task = {'id': 'transient-mcp-test', 'convId': 'client-supplied-job-label', '_userId': 7,
            'messages': [{'role': 'user', 'content': 'hope search'}], 'config': cfg}
    resolved = {'project_path': '', 'project_enabled': False, 'search_mode': 'off',
                'search_enabled': False, 'fetch_enabled': False, 'code_exec_enabled': False,
                'browser_enabled': False, 'desktop_enabled': False, 'image_gen_enabled': False}
    return cfg, task, resolved, accesses


def test_oracle_transient_assembly_on_rust(transient_assembly, monkeypatch):
    from tests.mcp_protocol import test_mcp_transient_assembly as oracle
    for flag in ('_inline_messages', '_transientRuntime', None):
        cfg, task, resolved, accesses = transient_assembly
        oracle.test_roots_publish_mcp_authority_without_reading_a_conversation(
            (cfg, dict(task), resolved, accesses), flag)
    cfg, task, resolved, accesses = transient_assembly
    oracle.test_storage_free_direct_assembly_ignores_a_supplied_conversation_label(
        (dict(cfg), dict(task), resolved, accesses))
    oracle.test_each_headless_request_assembles_its_own_live_authority(
        (dict(cfg), dict(task), resolved, accesses), monkeypatch)


def test_oracle_headless_root_delivery_on_rust(transient_assembly, invocation_runtime, monkeypatch):
    from tests.mcp_protocol import test_mcp_transient_assembly as oracle
    oracle.test_headless_root_reaches_the_provider_and_terminal_delivery_with_mcp_enabled(
        transient_assembly, invocation_runtime, monkeypatch)


# ══ progressive wrappers ═════════════════════════════════════════════════════

def _progressive_python(defs, safety, query, server='', limit=5):
    from lib.mcp.progressive import search_mcp_catalog

    class Fake:
        def get_openai_tool_defs(self):
            return defs

        def get_tool_safety(self):
            return safety

    out = search_mcp_catalog(Fake(), query, server=server, limit=limit)
    canonical = getattr(out, 'canonical', None)
    return {'text': str(out), 'canonical': canonical.text() if canonical is not None else None}


def _progressive_defs():
    from tests.mcp_protocol.test_mcp_progressive import _tool as oracle_tool
    rows = [oracle_tool('mcp__github__search_issues', description='Search GitHub issues', read_only=True),
            oracle_tool('mcp__github__create_issue', description='Create a GitHub issue'),
            oracle_tool('mcp__docs__read_page', description='Lire la page ÉTÉ', read_only=True),
            oracle_tool('mcp__Docs__edit-page.v2', description='')]
    defs = [row[0] for row in rows]
    defs[2]['function']['parameters'] = {
        'type': 'object', 'description': 'd' * 300, 'title': 'dropped', 'required': ['url'],
        'properties': {'url': {'type': 'string', 'format': 'uri', 'examples': ['x']},
                       'opts': {'type': 'object', 'properties': {'deep': {'items': {'type': 'integer'}}}},
                       'choice': {'oneOf': [{'type': 'string'}, 3, {'enum': [1, 2]}]}}}
    defs.append({'type': 'function', 'function': {'name': 'not-namespaced'}})
    defs.append({'type': 'function'})
    safety = {row[0]['function']['name']: row[1] for row in rows}
    return defs, safety


@pytest.mark.parametrize(('query', 'server', 'limit'), [
    ('search issues', '', 5), ('github', '', 1), ('GITHUB', 'GitHub', 5), ('page', 'docs', 'x'),
    ('  ', '', 5), ('été', '', 9), ('edit-page.v2', '', None), ('mcp__docs__read_page', '', '2'),
    ('issue', '', 0), ('nothing matches', '', 5)])
def test_progressive_search_matches_python(rust_host, query, server, limit):
    defs, safety = _progressive_defs()
    python = _progressive_python(defs, safety, query, server, limit)
    rust = rust_host.call('progressive.search', {'defs': defs, 'safety': safety, 'query': query,
                                                 'server': server, 'limit': limit})
    assert rust == python


def test_progressive_budget_page_keeps_full_candidates_canonical(rust_host):
    """Twelve long matches exceed the oracle's 24,000-character page."""
    from tests.mcp_protocol.test_mcp_progressive import _tool as oracle_tool
    defs = [oracle_tool(f'mcp__srv__match_{i}', description='match capability guidance. ' * 160)[0]
            for i in range(12)]
    safety = {definition['function']['name']: True for definition in defs}
    python = _progressive_python(defs, safety, 'match', limit=12)
    rust = rust_host.call('progressive.search', {'defs': defs, 'safety': safety, 'query': 'match', 'limit': 12})
    assert rust == python
    page, full = json.loads(rust['text']), json.loads(rust['canonical'])
    assert page['truncated_for_budget'] is True and len(full['matches']) > len(page['matches'])
    small = rust_host.call('progressive.search', {'defs': defs[:1], 'safety': safety, 'query': 'match'})
    assert small['canonical'] is None and small == _progressive_python(defs[:1], safety, 'match')


def test_compact_schema_matches_python(rust_host):
    from lib.mcp.progressive import _compact_mcp_schema
    nested = {'type': 'object'}
    for _ in range(12):
        nested = {'type': 'object', 'properties': {'inner': nested}, 'anyOf': [nested, 3] * 1 + [nested] * (20 if _ == 11 else 1)}
    for schema in (None, 'text', {}, {'description': 'x' * 181, 'items': {'type': 'string'}}, nested,
                   _progressive_defs()[0][2]['function']['parameters']):
        assert rust_host.call('progressive.compact_schema', {'schema': schema}) == _compact_mcp_schema(schema)


def test_progressive_wrappers_enforce_the_safety_annotation_like_python(pair):
    from lib.mcp.progressive import MCP_READ_TOOL_NAME, MCP_WRITE_TOOL_NAME, call_progressive_mcp
    pair.connect('docs', [_tool('read_page', read_only=True), _tool('edit_page', read_only=False)])
    pair.py.set_disabled_tools('docs', [])
    cases = [(MCP_WRITE_TOOL_NAME, 'mcp__docs__read_page', {}),
             (MCP_READ_TOOL_NAME, 'mcp__docs__edit_page', {}),
             (MCP_READ_TOOL_NAME, 'not-a-name', {}),
             (MCP_READ_TOOL_NAME, 'mcp__docs__read_page', ['not', 'an', 'object']),
             (MCP_READ_TOOL_NAME, 'mcp__docs__missing', {}),
             (MCP_READ_TOOL_NAME, ' mcp__docs__read_page ', {'id': 1}),
             (MCP_WRITE_TOOL_NAME, 'mcp__docs__edit_page', {'id': 1})]
    for wrapper, name, arguments in cases:
        python = call_progressive_mcp(pair.py, wrapper, name, arguments)
        answer = pair.rs._host.call('progressive.call', pair.rs._params(
            wrapper=wrapper, name=name, arguments=arguments))
        if 'refused' in answer:
            assert answer['refused'] == python
        else:
            from lib.mcp.bridge_relay import _decode_result
            assert str(_decode_result(answer['result'])) == str(python)
            assert 'called' in str(python)


# ══ connector guidance / registry ════════════════════════════════════════════

TENCENT_TOKEN_PAGE = 'https://docs.qq.com/open/auth/mcp.html'
SHEET_URL = 'https://docs.qq.com/sheet/DUmFpbnZtYnZsQ0xW'


@pytest.fixture
def owner_config(monkeypatch, rust_host):
    """Owner config rows for both clients; ``reads`` records each owner read."""
    from lib.mcp import connector_guidance as guidance
    configs, reads = {}, []

    def load(*, user_id):
        reads.append(user_id)
        if configs.get('broken'):
            raise OSError('disk')
        return configs.get(user_id, {})

    monkeypatch.setattr(guidance, 'load_mcp_config', load)
    monkeypatch.setattr('lib.mcp.config.load_mcp_config', load)
    return configs, reads


def _availability_both(host, query='', namespace='', usable=(), owner=7):
    from lib.mcp import connector_guidance as guidance
    python = guidance.connector_search_availability(owner, query, namespace, usable)
    rust = host.call('connector_guidance.search_availability', {
        'owner': owner, 'query': query, 'namespace': namespace, 'usable': list(usable)})
    assert rust == python
    return rust


def test_connector_search_availability_matches_python(rust_host, owner_config):
    configs, _ = owner_config
    [row] = _availability_both(rust_host, '腾讯文档 表格')
    assert row['reason_code'] == 'mcp_connector_not_installed' and TENCENT_TOKEN_PAGE in row['recovery_hint']
    configs[7] = {'kdocs': {'enabled': False}}
    [row] = _availability_both(rust_host, 'read sheet', namespace='KDocs')
    assert row['reason_code'] == 'mcp_connector_disabled'
    configs[7] = {'kdocs': {'enabled': True}, 'tencent-docs': 'not a row'}
    [row] = _availability_both(rust_host, '金山文档')
    assert row['reason_code'] == 'mcp_connector_unavailable' and 'https://www.kdocs.cn/latest' in row['recovery_hint']
    for query in ('TENCENT DOCS and kdocs', 'tencent docs spreadsheet', '  腾讯文档  '):
        _availability_both(rust_host, query, owner='7')
        _availability_both(rust_host, query, owner=7.9, usable=[' Tencent-Docs '])


def test_no_evidence_and_no_config_read_when_usable_unowned_or_generic(rust_host, owner_config):
    _, reads = owner_config
    assert _availability_both(rust_host, '腾讯文档', usable=['tencent-docs']) == []
    for owner in (0, None, -3, 'x', True):
        if owner is True:
            continue
        assert _availability_both(rust_host, '腾讯文档', owner=owner) == []
    assert _availability_both(rust_host, 'online spreadsheet read range') == []
    assert reads == [], 'no configuration read without a named connector'


def test_unreadable_configuration_makes_no_claim(rust_host, owner_config):
    configs, _ = owner_config
    configs['broken'] = True
    assert _availability_both(rust_host, '腾讯文档') == []
    assert rust_host.call('connector_guidance.document_link_guidance', {'owner': 7, 'url': SHEET_URL}) is None


@pytest.mark.parametrize('url', [
    SHEET_URL, 'https://DOCS.QQ.COM/doc/DSGxxxx?tab=1', 'https://www.kdocs.cn/l/cmAqaKBmDXAN',
    'https://kdocs.cn/l/cmAqaKBmDXAN', 'https://docs.qq.com/open/document/mcp/', 'https://365.kdocs.cn/l/abc',
    'ftp://docs.qq.com/sheet/x', 'not a url', '  https://user:pw@docs.qq.com:443/sheet/x#frag  ',
    'HTTPS://docs.qq.com/sheet/x', 'https://[::1]/sheet', 'https://[bad/sheet', 'http://docs.qq.com\t/sheet/x',
    '//docs.qq.com/sheet/x', ''])
def test_document_links_map_to_their_connector(rust_host, url):
    from lib.mcp import connector_guidance as guidance
    python = guidance.connector_for_document_url(url)
    rust = rust_host.call('connector_guidance.for_document_url', {'url': url})
    assert (rust['id'] if rust else None) == (python['id'] if python else None)


def test_document_link_guidance_follows_install_state(rust_host, owner_config):
    from lib.mcp import connector_guidance as guidance
    configs, _ = owner_config

    def both(owner, url):
        python = guidance.document_link_guidance(owner, url)
        assert rust_host.call('connector_guidance.document_link_guidance', {'owner': owner, 'url': url}) == python
        return python

    assert TENCENT_TOKEN_PAGE in both('7', SHEET_URL)
    configs[7] = {'tencent-docs': {'enabled': True}}
    assert "namespace 'tencent-docs'" in both('7', SHEET_URL)
    configs[7] = {'tencent-docs': {'enabled': False}}
    assert 'disabled' in both(7, SHEET_URL)
    assert both('', SHEET_URL) is None and both('7', 'https://example.com/') is None
    both(7, 'https://www.kdocs.cn/l/cmAqaKBmDXAN')


def test_fetch_reader_over_rust_guidance(rust_host, owner_config, monkeypatch):
    """The fetch reader (``lib.search.search_bridge``) answers from the Rust guidance."""
    from lib import auth_sources
    from lib.mcp import connector_guidance as guidance
    from lib.search import search_bridge
    monkeypatch.setattr(guidance, 'connector_for_document_url', lambda url: rust_host.call(
        'connector_guidance.for_document_url', {'url': url}))
    monkeypatch.setattr(guidance, 'document_link_guidance', lambda owner, url: rust_host.call(
        'connector_guidance.document_link_guidance', {'owner': owner, 'url': url}))
    reader = search_bridge._ChatuiConnectorDocReader()
    monkeypatch.setattr(search_bridge, '_bind_browser_identity', lambda: ('7', '', ''))
    monkeypatch.setattr(auth_sources, 'match_source', lambda url: None)
    assert reader.matches(SHEET_URL)
    assert not reader.matches('https://docs.qq.com/open/document/mcp/')
    assert TENCENT_TOKEN_PAGE in reader.read(SHEET_URL)
    monkeypatch.setattr(auth_sources, 'match_source', lambda url: {'domain': 'docs.qq.com'})
    assert reader.read(SHEET_URL) is None


def test_tool_search_gateway_relays_rust_setup_guidance(rust_host, owner_config, monkeypatch):
    from lib.mcp import connector_guidance as guidance
    from lib.tasks_pkg.tool_search_projection import tool_search_display_results
    from tests.mcp_protocol.test_mcp_connector_guidance import _search

    def run():
        task = {'id': 'connector-task', '_userId': 7, '_executable_tool_catalog': [],
                '_executableToolNamespaceByName': {}}
        out = [_search(monkeypatch, task, 'tencent docs spreadsheet'),
               _search(monkeypatch, task, 'tencent docs spreadsheet')]
        task['_executableToolNamespaceByName'] = {'mcp__tencent-docs__get_sheet': 'tencent-docs'}
        out.append(_search(monkeypatch, task, 'tencent docs sheet'))
        return out

    python = run()
    monkeypatch.setattr(guidance, 'connector_search_availability', lambda owner, query, namespace, usable: (
        rust_host.call('connector_guidance.search_availability', {
            'owner': owner, 'query': query, 'namespace': namespace, 'usable': list(usable)})))
    rust = run()
    assert rust == python
    [evidence] = rust[0]['capability_availability']
    assert evidence['reason_code'] == 'mcp_connector_not_installed' and TENCENT_TOKEN_PAGE in rust[0]['notice']
    assert tool_search_display_results(rust[0])[-1]['reasonCode'] == 'mcp_connector_not_installed'
    assert rust[1]['repeated_query'] == 2 and 'capability_availability' not in rust[2]


def _edition_host(binary, value):
    from lib.mcp.rust_host import RustMCPHost
    env = {key: item for key, item in os.environ.items() if key != 'TOFU_OPENSOURCE_BUILD'}
    if value is not None:
        env['TOFU_OPENSOURCE_BUILD'] = value
    return RustMCPHost(binary, env=env)


def _python_edition(value):
    """``registry.is_opensource_build`` in a fresh interpreter (its import-time read)."""
    env = {key: item for key, item in os.environ.items() if key != 'TOFU_OPENSOURCE_BUILD'}
    if value is not None:
        env['TOFU_OPENSOURCE_BUILD'] = value
    env['PYTHONPATH'] = str(ROOT)
    out = subprocess.run([sys.executable, '-c', 'import json; import lib.mcp.registry as r; '
                          'print(json.dumps([r.is_opensource_build(), [e["id"] for e in r.get_catalog()]]))'],
                         env=env, cwd=str(ROOT), capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('value', [None, '', '1', 'true', ' YES ', 'On', '0', 'no', 'opensource'])
def test_is_opensource_build_matches_python(mcp_host_binary, value):
    """The only MCP part brand/audio tests use: ``lib.mcp.registry.is_opensource_build``."""
    edition, ids = _python_edition(value)
    host = _edition_host(mcp_host_binary, value)
    try:
        assert host.call('registry.is_opensource_build', timeout=30) is edition
        assert [entry['id'] for entry in host.call('registry.catalog', timeout=30)] == ids
    finally:
        host.close()


@pytest.mark.parametrize('value', [None, '1'])
def test_opensource_build_hides_internal_entries_like_python(mcp_host_binary, monkeypatch, value):
    import lib.mcp.registry as reg
    entries = [{'id': 'hope', 'name': 'Hope', 'command': 'hope-mcp', 'internal_only': True},
               {'id': 'github', 'name': 'GitHub', 'command': 'gh'},
               {'id': 'llm', 'name': 'LLM', 'command': 'llm', 'internal_only': 1},
               {'id': 'github', 'name': 'GitHub 2', 'command': 'gh2'}]
    monkeypatch.setattr(reg, 'CATALOG', entries)
    monkeypatch.setattr(reg, '_CATALOG_INDEX', {e['id']: e for e in entries})
    monkeypatch.setattr(reg, '_catalog_mtime', float('inf'))
    monkeypatch.setattr(reg, '_OPENSOURCE_BUILD', value == '1')
    host = _edition_host(mcp_host_binary, value)
    try:
        assert host.call('registry.catalog', timeout=30) == reg.get_catalog()
        for sid in ('hope', 'llm', 'github', 'missing'):
            assert host.call('registry.catalog_entry', {'server_id': sid}, timeout=30) == reg.get_catalog_entry(sid)
    finally:
        host.close()
    real = _python_edition('1')[1]
    assert not {'hope', 'llm', 'xuecheng'} & set(real)


def test_registry_hot_reload_reaches_rust(rust_host, tmp_path, monkeypatch):
    import lib.mcp.registry as reg
    monkeypatch.setattr(reg, '_OPENSOURCE_BUILD', False)
    monkeypatch.setattr(reg, 'CATALOG', [{'id': 'seed', 'name': 'Seed', 'command': 'seed-mcp', 'args': []}])
    monkeypatch.setattr(reg, '_CATALOG_INDEX', {'seed': reg.CATALOG[0]})
    path = tmp_path / 'reg_copy.py'
    path.write_text("CATALOG = [{'id': 'seed', 'name': 'S', 'command': 's', 'args': []},"
                    " {'id': 'fresh-card', 'name': 'F', 'command': 'f', 'args': []}]\n", encoding='utf-8')
    monkeypatch.setattr(reg, '_registry_path', lambda: str(path))
    monkeypatch.setattr(reg, '_catalog_mtime', os.path.getmtime(path) - 100)
    assert {e['id'] for e in rust_host.call('registry.catalog')} == {'seed', 'fresh-card'}
    assert rust_host.call('registry.catalog_entry', {'server_id': 'fresh-card'})['name'] == 'F'
    path.write_text('CATALOG = [ this is not valid python {{{\n', encoding='utf-8')
    monkeypatch.setattr(reg, '_catalog_mtime', os.path.getmtime(path) - 100)
    assert {e['id'] for e in rust_host.call('registry.catalog')} == {'seed', 'fresh-card'}
    sentinel = [{'id': 'sentinel', 'name': 'S', 'command': 's-mcp', 'args': []}]
    monkeypatch.setattr(reg, 'CATALOG', sentinel)
    monkeypatch.setattr(reg, '_catalog_mtime', os.path.getmtime(path) + 100)
    assert [e['id'] for e in rust_host.call('registry.catalog')] == ['sentinel']


# ══ owner isolation ══════════════════════════════════════════════════════════

def test_owner_bridges_are_isolated_and_owner_ids_validated(rust_host, servers):
    from lib.mcp.rust_host import RustHostError, RustMCPBridge
    one, two = RustMCPBridge(rust_host, 1), RustMCPBridge(rust_host, 2)
    try:
        one.connect_server('same', servers.config('same', [_tool('submit')], tag='-1'))
        two.connect_server('same', servers.config('same', [_tool('other')], tag='-2'))
        assert _names(one.get_openai_tool_defs()) == ['mcp__same__submit']
        assert _names(two.get_openai_tool_defs()) == ['mcp__same__other']
        assert RustMCPBridge(rust_host, 1).is_mcp_tool('mcp__same__submit')
        assert not two.is_mcp_tool('mcp__same__submit')
        for owner in (0, -1, True, 'x'):
            with pytest.raises(RustHostError, match='positive integer'):
                rust_host.call('catalog.counts', {'owner_user_id': owner})
    finally:
        one.disconnect_all()
        two.disconnect_all()


@pytest.mark.parametrize('raw', ['0', 'bad', '999999', '1'])
def test_registry_retains_every_owner_and_ignores_the_retired_capacity_setting(mcp_host_binary, servers, raw):
    from lib.mcp.rust_host import RustMCPBridge, RustMCPHost
    host = RustMCPHost(mcp_host_binary, env={**os.environ, 'TOFU_MCP_OWNER_BRIDGE_CAPACITY': raw})
    try:
        first = RustMCPBridge(host, 1)
        first.connect_server('kept', servers.config('kept', [_tool('read')], tag=f'-{raw}'))
        for owner in range(2, 129):
            assert RustMCPBridge(host, owner).server_count == 0
        assert first.server_count == 1 and first.is_mcp_tool('mcp__kept__read')
        first.disconnect_all()
    finally:
        host.close()


def test_owner_config_rows_are_read_per_owner(rust_host, monkeypatch):
    from lib.mcp import config as mcp_config
    from lib.mcp import connector_guidance as guidance
    rows = {1: {'tencent-docs': {'enabled': True}}, 2: {'tencent-docs': {'enabled': False}}}
    reads = []

    def load(*, user_id):
        reads.append(user_id)
        return rows.get(user_id, {})

    monkeypatch.setattr(mcp_config, 'load_mcp_config', load)
    monkeypatch.setattr(guidance, 'load_mcp_config', load)
    codes = {}
    for owner in (1, 2, 3):
        [row] = _availability_both(rust_host, '腾讯文档', owner=owner)
        codes[owner] = row['reason_code']
    assert codes == {1: 'mcp_connector_unavailable', 2: 'mcp_connector_disabled',
                     3: 'mcp_connector_not_installed'}
    assert reads == [1, 1, 2, 2, 3, 3]


def test_mcp_build_fails_closed_without_owner_on_rust(rust_host, monkeypatch):
    import lib.mcp
    from lib.mcp.rust_host import RustMCPBridge
    from lib.tools.registry._build import _build_mcp
    from lib.tools.registry._spec import ToolContext
    asked = []
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: asked.append(owner) or RustMCPBridge(rust_host, 1))
    ctx = ToolContext(cfg={'mcpEnabled': True}, task_id='t', project_path='', project_enabled=False,
                      search_mode='off', search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                      browser_enabled=False, desktop_enabled=False, owner_user_id=0)
    assert _build_mcp(ctx) == [] and asked == []


def test_resolver_uses_only_the_task_owners_rust_bridge(rust_host, servers, monkeypatch):
    import lib.mcp.client as client
    from lib.mcp.rust_host import RustMCPBridge
    from lib.tasks_pkg.executor._resolver import resolver_for
    one, two = RustMCPBridge(rust_host, 1), RustMCPBridge(rust_host, 2)
    one.connect_server('hop', servers.config('hop', [_tool('submit')], tag='-1'))
    two.connect_server('other', servers.config('other', [_tool('tool')], tag='-2'))
    monkeypatch.setitem(client._bridges, 1, one)
    monkeypatch.setitem(client._bridges, 2, two)
    try:
        registry = type('R', (), {'lookup': staticmethod(lambda n, r=None: None)})()
        resolver = resolver_for({'_userId': 1}, registry=registry)
        assert resolver.lookup('mcp__hop__submit') is not None
        assert resolver.lookup('mcp__other__tool') is None
        assert resolver_for({'_userId': 0}, registry=registry).lookup('mcp__hop__submit') is None
    finally:
        one.disconnect_all()
        two.disconnect_all()


def test_a_busy_owner_never_blocks_another_owner(rust_host, servers):
    from lib.mcp.rust_host import RustMCPBridge
    seven, eight = RustMCPBridge(rust_host, 7), RustMCPBridge(rust_host, 8)
    seven.connect_server('slow', servers.config('slow', [_tool('wait', schema={
        'type': 'object', 'properties': {'id': {'type': 'string'}}})], tag='-7'))
    eight.connect_server('fast', servers.config('fast', [_tool('read')], tag='-8'))
    try:
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault(
            'slow', seven.call_tool('mcp__slow__wait', {'id': 'sleep:3'})))
        started = time.monotonic()
        worker.start()
        time.sleep(0.3)
        assert 'called read' in str(eight.call_tool('mcp__fast__read', {}))
        assert eight.get_enabled_tool_summary() == {'servers': [{'name': 'fast', 'count': 1}], 'total': 1}
        assert time.monotonic() - started < 3
        worker.join(30)
        assert 'called wait' in str(result['slow'])
    finally:
        seven.disconnect_all()
        eight.disconnect_all()


def test_generated_unicode_tables_are_current():
    subprocess.run([sys.executable, str(ROOT / 'packages' / 'tofu-mcp' / 'scripts' / 'gen_pyunicode.py'),
                    '--check'], check=True)
