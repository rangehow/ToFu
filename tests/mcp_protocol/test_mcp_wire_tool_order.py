"""tests/mcp_protocol/test_mcp_wire_tool_order.py — the MCP catalog a task searches and
executes is order-stable, pinned at the ASSEMBLY PATH rather than at the helper.

WHY THIS EXISTS ALONGSIDE test_cache_schema_stability.py
---------------------------------------------------------
``TestMCPDeterministicOrdering`` already pins that ``get_openai_tool_defs()``
sorts. That is necessary but not sufficient: it drives the helper directly, so
it stays GREEN if someone repoints the actual consumer somewhere else.

There are two orderings in the bridge and only one of them is sorted:

  * ``MCPBridge.get_openai_tool_defs()`` — sorted by namespaced name.
  * ``handle.tools = response.tools`` (``_bridge.py``) — the server's own
    order, stored verbatim per server.

MCP tools are search-only (``lib/tools/mcp_exposure.py``): ``_build_mcp``
contributes no wire schema and publishes the sorted helper's catalog as the
task's MCP authority (``_mcpAllowedToolCatalog``), which the executable
catalog, Tool Search ranking ties and exact-name lookups all read. A reconnect
that reorders a server's ``tools/list`` must not reorder that authority.

WHAT THIS PINS
--------------
Drive the real ``_build_mcp`` with two bridges holding the same tools in
DIFFERENT server-returned orders (what a reconnect / re-discovery produces)
and assert the published authority is identical and sorted, and that no
schema reaches the wire.

WHY THE NON-EMPTY ASSERTION IS LOAD-BEARING
--------------------------------------------
``_build_mcp`` publishes nothing on any failure — disabled via config, bridge
not connected, or any exception (it logs and continues). An empty catalog is
trivially "identical" and trivially "sorted", so without an explicit
non-empty check every assertion below passes vacuously: a bridge missing
``_started`` reports ``connected == False`` and publishes nothing.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

pytestmark = pytest.mark.unit


def _bridge_with(server_order):
    """A connected MCPBridge whose tool index was populated in ``server_order``.

    Insertion order is what a server's ``tools/list`` response dictates, and it
    is exactly what changes across a reconnect.
    """
    from lib.mcp.client._bridge import MCPBridge
    from lib.mcp.types import make_namespaced_name

    b = MCPBridge.__new__(MCPBridge)
    b._lock = threading.RLock()
    b._tool_index = {}
    b._servers = {'srv': type('H', (), {'session': object(), 'tools': []})()}
    # ``connected`` is a read-only property: ``_started and server_count > 0``.
    # Without this the bridge reads as disconnected and nothing is published.
    b._started = True

    for name in server_order:
        ns = make_namespaced_name('srv', name)
        b._tool_index[ns] = {
            'server_name': 'srv',
            'tool_name': name,
            'namespaced_name': ns,
            'description': 'd',
            'input_schema': {'type': 'object', 'properties': {}},
            'openai_def': {'type': 'function',
                           'function': {'name': ns, 'description': 'd',
                                        'parameters': {}}},
            'read_only_hint': False,
        }
    return b


def _assembled(monkeypatch, bridge):
    import lib.mcp as mcpmod
    from lib.tools.registry import _build

    assert bridge.connected, (
        'fixture bridge is not connected — _build_mcp would publish nothing and '
        'every assertion in this module would pass vacuously')
    monkeypatch.setattr(mcpmod, 'get_bridge', lambda owner: bridge)
    ctx = type('C', (), {'cfg': {'mcpEnabled': True}, 'tid': 't-order', 'owner_user_id': 7})()
    wire = _build._build_mcp(ctx)
    return wire, [d['function']['name'] for d in ctx.cfg['_mcpAllowedToolCatalog']]


def _authority_names(monkeypatch, server_order):
    """Names of the MCP authority ``_build_mcp`` publishes for the task."""
    wire, names = _assembled(monkeypatch, _bridge_with(server_order))
    assert wire == [], 'an MCP schema reached the wire; MCP is search-only'
    assert names, ('_build_mcp published an EMPTY catalog — the test would be '
                   'vacuous; check the bridge fixture, not the assertion')
    return names


#: Deliberately not alphabetical, so "sorted" and "server order" differ.
_ORDER_A = ['zebra_tool', 'alpha_tool', 'middle_tool', 'beta_tool']
#: The same tools a server might return after a reconnect, reshuffled.
_ORDER_B = ['beta_tool', 'middle_tool', 'zebra_tool', 'alpha_tool']


def test_authority_catalog_is_sorted(monkeypatch):
    """The catalog a task searches and executes is sorted by namespaced name."""
    names = _authority_names(monkeypatch, _ORDER_A)
    assert names == sorted(names), (
        f'MCP authority catalog is not sorted: {names}. Search ties and the '
        f'executable catalog would then follow server reconnect order.')


def test_reconnect_reorder_does_not_change_the_authority(monkeypatch):
    """Two different server orders must produce an identical catalog."""
    a = _authority_names(monkeypatch, _ORDER_A)
    b = _authority_names(monkeypatch, _ORDER_B)
    assert a == b, (
        f'server reorder changed the MCP authority catalog:\n  {a}\n  {b}')


def test_the_fixture_orders_really_differ():
    """Guard-of-the-guard: the two fixtures must not be the same order.

    If someone "tidied" these lists into the same (or already-sorted) order,
    the reorder test above would compare a sequence with itself and could
    never fail.
    """
    assert _ORDER_A != _ORDER_B
    assert _ORDER_A != sorted(_ORDER_A), (
        '_ORDER_A is already alphabetical — sorted and server order would be '
        'indistinguishable and the sort assertion would be vacuous')


def test_assembly_consumes_the_sorted_helper(monkeypatch):
    """The authority must equal the SORTED helper's output, tool for tool.

    Pins the routing, not just the shape: if the consumer were repointed at
    the per-server ``handle.tools`` order, this diverges even though the
    helper-level guards in test_cache_schema_stability.py stay green.
    """
    bridge = _bridge_with(_ORDER_A)
    wire, names = _assembled(monkeypatch, bridge)
    helper = [d['function']['name'] for d in bridge.get_openai_tool_defs()]
    assert wire == [] and names, 'empty catalog — vacuous'
    assert names == helper, (
        f'MCP authority does not come from get_openai_tool_defs():\n'
        f'  authority {names}\n  helper    {helper}')


def test_per_server_handle_order_is_not_what_ships(monkeypatch):
    """Complement: the raw server order must NOT be what the task holds.

    Without this, an implementation that happened to sort in a second place
    would satisfy the tests above while re-introducing a second source of
    truth for ordering.
    """
    names = _authority_names(monkeypatch, _ORDER_A)
    from lib.mcp.types import make_namespaced_name
    raw = [make_namespaced_name('srv', n) for n in _ORDER_A]
    assert names != raw, (
        'the MCP authority is in the server-returned order — ordering is no '
        'longer normalised on our side')
