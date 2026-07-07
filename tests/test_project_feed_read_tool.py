"""tests/test_project_feed_read_tool.py — Pillar #6 gap #1: agent-readable feed.

Closes the audited perception hole: the cross-conversation ACTIVITY FEED was
written everywhere (task lifecycle / board / peer notes) but an AGENT could
never read it mid-turn — only the HTTP/UI route surfaced it, and
``system_context.py`` injects only the board + charter blocks, never the feed.
A conversation could therefore see who is live NOW (``project_peer_status``)
and the epic lanes (``project_board_read``) but not the narrative of what
siblings had been DOING.

The fix is an on-demand ``project_feed_read`` agent tool (NOT always-on
injection — the feed holds up to ``_PROJECT_EVENTS_KEEP`` events and changes
every turn a sibling acts, so injecting it would bloat context AND bust the
append-only prompt-cache prefix; board+charter are small stable summaries suited
to injection, the chronological pulse is pulled).

Also covers the operator-initiated hard-abort route (gap #2):
``POST /api/v1/project/brain/peer-abort`` — the human counterpart to
``project_intervene(hard_abort=True)`` where the authenticated operator IS the
approval token.

Style: pure-pytest (no Quart shim) for the tool logic + formatter; the two
route tests use ``flask_client``. Every load-bearing seam has a byte-reverting
NEGATIVE CONTROL.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_PEER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_peer.py')
_CONV_SRC = os.path.join(ROOT, 'lib', 'tools', 'conversation.py')


def _patch_restore(path, old, new, run):
    """Byte-revert a guard, run the neutered assertion, restore byte-identical."""
    with open(path, encoding='utf-8') as f:
        original = f.read()
    assert old in original, f'anchor not found in {path}'
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original.replace(old, new, 1))
        run()
    finally:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original)
    with open(path, encoding='utf-8') as f:
        assert f.read() == original, 'source not restored byte-identical'


def _events():
    return [
        {'seq': 3, 'kind': 'completed', 'conv_id': 'cB', 'title': 'Docs pass',
         'summary': 'finished the glossary'},
        {'seq': 2, 'kind': 'claimed', 'conv_id': 'cA', 'title': 'Parser work',
         'summary': 'claimed «Refactor the parser»'},
        {'seq': 1, 'kind': 'note', 'conv_id': 'cA', 'title': 'Parser work',
         'summary': 'intervention → conv cB: overlap'},
    ]


# ════════════════════════════════════════════════════════════════════
#  Pure formatter: _fmt_feed
# ════════════════════════════════════════════════════════════════════

def test_fmt_feed_empty():
    from lib.conversations.project_peer import _fmt_feed
    out = _fmt_feed([])
    assert 'No recent cross-conversation activity' in out


def test_fmt_feed_renders_rows_and_marks_self():
    from lib.conversations.project_peer import _fmt_feed
    out = _fmt_feed(_events(), current_conv_id='cA')
    # header + one line per event
    assert '3 event(s)' in out
    assert '[completed]' in out and '[claimed]' in out and '[note]' in out
    assert 'Docs pass' in out and 'finished the glossary' in out
    # the caller's own conversation is marked
    assert '(this conversation)' in out
    # a foreign conv is NOT marked as self
    lines = [ln for ln in out.splitlines() if 'Docs pass' in ln]
    assert lines and '(this conversation)' not in lines[0]


# ════════════════════════════════════════════════════════════════════
#  execute_peer_tool('project_feed_read', ...) — DB-free (read stubbed)
# ════════════════════════════════════════════════════════════════════

def test_feed_read_refuses_outside_project():
    from lib.conversations.project_peer import execute_peer_tool
    out = execute_peer_tool('project_feed_read', {}, current_conv_id='cA',
                            project_path='')
    assert 'only available in project mode' in out


def test_feed_read_reads_and_formats(monkeypatch):
    """The tool calls read_project_feed for the given project and formats the
    result — DB-free via a stubbed read."""
    import lib.conversations.project_feed as pf
    captured = {}

    def _fake_read(project_path, since_seq=0, limit=100):
        captured['path'] = project_path
        captured['limit'] = limit
        return {'events': _events(), 'maxSeq': 3}

    monkeypatch.setattr(pf, 'read_project_feed', _fake_read)
    from lib.conversations.project_peer import execute_peer_tool
    out = execute_peer_tool('project_feed_read', {'limit': 5},
                            current_conv_id='cB', project_path='/proj/x')
    assert captured['path'] == '/proj/x'
    assert captured['limit'] == 5
    assert '[completed]' in out and 'Docs pass (this conversation)' in out


def test_feed_read_clamps_limit(monkeypatch):
    """limit is clamped to [1, 60] and defaults to 25 (bounds the payload)."""
    import lib.conversations.project_feed as pf
    seen = []
    monkeypatch.setattr(pf, 'read_project_feed',
                        lambda p, since_seq=0, limit=100: seen.append(limit) or {'events': [], 'maxSeq': 0})
    from lib.conversations.project_peer import execute_peer_tool
    # NOTE: limit=0 is falsy → `int(0 or 25)` defaults to 25 (0 is meaningless);
    # the floor-to-1 clamp only bites on a negative limit.
    for arg in [{}, {'limit': -5}, {'limit': 999}, {'limit': 'x'}, {'limit': 10}]:
        execute_peer_tool('project_feed_read', arg, current_conv_id='c',
                          project_path='/p')
    assert seen == [25, 1, 60, 25, 10], seen


# ════════════════════════════════════════════════════════════════════
#  AGENT REACHABILITY — schema + name-set + registry routing + dispatch.
#  (Not just the execute_peer_tool branch: the tool must be invocable
#   end-to-end, or it's a phantom tool the model can never call.)
# ════════════════════════════════════════════════════════════════════

def test_feed_read_in_schema_and_name_set():
    from lib.tools import PEER_TOOLS, PEER_TOOL_NAMES
    names = [t['function']['name'] for t in PEER_TOOLS]
    assert 'project_feed_read' in names, \
        'project_feed_read MUST be in the tool schema (else phantom-tool trap)'
    assert 'project_feed_read' in PEER_TOOL_NAMES, \
        'project_feed_read MUST be in the gate/name set (else dispatch never routes)'
    spec = [t for t in PEER_TOOLS
            if t['function']['name'] == 'project_feed_read'][0]
    props = spec['function']['parameters']['properties']
    assert 'limit' in props


def test_feed_read_registered_in_conv_ref_provides():
    """The conv_ref ToolSpec's provides/idempotent sets must include the tool
    so the assembler advertises it and marks it read-only (idempotent)."""
    from lib.tools.registry import all_specs
    spec = next(s for s in all_specs() if s.key == 'conv_ref')
    assert 'project_feed_read' in spec.provides
    assert 'project_feed_read' in spec.idempotent_tools


def test_registry_routes_feed_read_to_peer_handler():
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers.misc import _handle_peer_tool
    handler = tool_registry.lookup('project_feed_read', {})
    assert handler is _handle_peer_tool, \
        'project_feed_read must route to _handle_peer_tool via PEER_TOOL_NAMES'


def test_feed_read_reachable_end_to_end_via_agent_dispatch(monkeypatch):
    """END-TO-END: an agent tool call for project_feed_read must actually read
    the feed and return the formatted slice — driven through _execute_tool_one,
    the same path a live LLM turn uses. DB-free (read stubbed)."""
    import threading

    import lib.conversations.project_feed as pf
    monkeypatch.setattr(pf, 'read_project_feed',
                        lambda p, since_seq=0, limit=100: {'events': _events(), 'maxSeq': 3})
    # Keep the round finalize + event append side-effect-free.
    monkeypatch.setattr('lib.tasks_pkg.handlers.misc.append_event',
                        lambda t, ev: None)

    from lib.tasks_pkg.executor import _execute_tool_one
    task = {
        'id': 'tfeed01', 'convId': 'cA', 'toolRounds': [], 'messages': [],
        'events': [], 'events_lock': threading.Lock(),
    }
    round_entry = {'roundNum': 1, 'query': 'project_feed_read',
                   'results': None, 'status': 'searching',
                   'toolName': 'project_feed_read'}
    task['toolRounds'].append(round_entry)
    tc_id, content, _is_search = _execute_tool_one(
        task, {'id': 'tc1', 'function': {'name': 'project_feed_read'}},
        'project_feed_read', 'tc1', {'limit': 10}, 1, round_entry,
        {'model': 'x'}, '/proj/x', True, None)
    assert 'Recent project activity' in content
    assert '[completed]' in content and 'Docs pass' in content


# ── NC-SCHEMA: byte-remove project_feed_read from the schema registration →
#    the model-facing tool list no longer advertises it, so it becomes a
#    phantom the agent can never call. Schema-level + NON-POLLUTING: reloads
#    ONLY lib.tools.conversation + lib.tools (call-time schema source). It does
#    NOT reload lib.tasks_pkg.executor — the dispatch registry captures the
#    NAME-SET frozenset at import time, so reloading it here would recreate the
#    tool_registry singleton and strip every other handler for the rest of the
#    session (the routing side is proven separately by
#    test_registry_routes_feed_read_to_peer_handler). ──
def test_NC_feed_read_schema_registration_is_load_bearing():
    import importlib

    def run():
        import lib.tools.conversation as conv
        importlib.reload(conv)
        import lib.tools as _tools
        importlib.reload(_tools)
        names = [t['function']['name'] for t in _tools.PEER_TOOLS]
        assert 'project_feed_read' not in names, \
            'NC-SCHEMA: with PEER_FEED_TOOL removed the model-facing schema ' \
            'must NOT advertise project_feed_read (proving the registration ' \
            'is what makes the tool visible to the agent)'
        assert 'project_feed_read' not in _tools.PEER_TOOL_NAMES

    _patch_restore(
        _CONV_SRC,
        "PEER_TOOLS = [PEER_STATUS_TOOL, PEER_FEED_TOOL, PEER_MESSAGE_TOOL,\n"
        "              PEER_INTERVENE_TOOL]\n"
        "PEER_TOOL_NAMES = {'project_peer_status', 'project_feed_read',\n"
        "                   'project_message', 'project_intervene'}",
        "PEER_TOOLS = [PEER_STATUS_TOOL, PEER_MESSAGE_TOOL,\n"
        "              PEER_INTERVENE_TOOL]  # NC-SCHEMA (feed_read unregistered)\n"
        "PEER_TOOL_NAMES = {'project_peer_status',\n"
        "                   'project_message', 'project_intervene'}",
        run,
    )
    # Restore the facade from the byte-identical source (executor untouched).
    import lib.tools as _tools
    import lib.tools.conversation as conv
    importlib.reload(conv)
    importlib.reload(_tools)
    # Positive proof the restore worked: the tool is advertised again.
    assert 'project_feed_read' in _tools.PEER_TOOL_NAMES


# ── NC-DISPATCH: no-op the feed branch in execute_peer_tool → the tool no
#    longer reads the feed (falls through to the unknown-tool tail). ──
def test_NC_feed_branch_noop_breaks_read(monkeypatch):
    import importlib

    import lib.conversations.project_feed as pf
    monkeypatch.setattr(pf, 'read_project_feed',
                        lambda p, since_seq=0, limit=100: {'events': _events(), 'maxSeq': 3})

    def run():
        import lib.conversations.project_peer as pp
        importlib.reload(pp)
        out = pp.execute_peer_tool('project_feed_read', {'limit': 5},
                                   current_conv_id='cA', project_path='/p')
        assert 'Recent project activity' not in out, \
            'NC-DISPATCH: with the feed branch disabled the tool must NOT ' \
            'return the formatted feed (proving the branch is load-bearing)'
        assert 'Unknown peer tool' in out

    _patch_restore(
        _PEER_SRC,
        "        if fn_name == 'project_feed_read':",
        "        if fn_name == '__nc_disabled_feed_read__':",
        run,
    )
    import lib.conversations.project_peer as pp
    importlib.reload(pp)


# ════════════════════════════════════════════════════════════════════
#  Operator-initiated hard-abort route (gap #2)
# ════════════════════════════════════════════════════════════════════

def test_peer_abort_route_stops_target(flask_client, monkeypatch):
    """POST /brain/peer-abort aborts the target's running task via
    intervene_peer with the operator stamped as approved_by. abort +
    feed-emit are stubbed so no live task/DB is needed."""
    aborted = []
    monkeypatch.setattr('lib.tasks_pkg.manager.abort_running_tasks_for_conv',
                        lambda c, **k: aborted.append(c) or 2)
    monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                        lambda *a, **k: None)
    # Identity target-id resolution: synthetic cTARGET has no seeded
    # conversations row. Without this the REAL resolver returns unknown_target
    # once any sibling suite has run init_db() (creating the empty conversations
    # table) — a cross-file ordering fragility, not a product bug.
    monkeypatch.setattr('lib.conversations.project_peer._resolve_target_conv_id',
                        lambda t: ((t or '').strip(), ''))
    audits = []
    monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                        lambda ev, **k: audits.append((ev, k)))

    resp = flask_client.post('/api/v1/project/brain/peer-abort', json={
        'path': '/proj/x', 'convId': 'cOP', 'toConvId': 'cTARGET'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    data = body.get('data', body)
    assert data.get('mode') == 'hard_abort' and data.get('aborted') == 2
    assert aborted == ['cTARGET'], 'the operator abort must target the peer'
    # The intervention is audit-logged with a non-blank operator identity.
    intervention = [k for ev, k in audits if ev == 'intervention']
    assert intervention and (intervention[0].get('approved_by') or '').strip(), \
        'the operator abort must be audit-logged with a non-blank approver'


def test_peer_abort_route_requires_convid(flask_client):
    """Refused (400) without the acting conversation — mirrors the board
    mutations; never invents a proxy conv."""
    resp = flask_client.post('/api/v1/project/brain/peer-abort', json={
        'path': '/proj/x', 'toConvId': 'cTARGET'})
    assert resp.status_code == 400
    assert 'convId' in resp.get_data(as_text=True)


def test_peer_abort_route_requires_target(flask_client):
    resp = flask_client.post('/api/v1/project/brain/peer-abort', json={
        'path': '/proj/x', 'convId': 'cOP'})
    assert resp.status_code == 400
    assert 'toConvId' in resp.get_data(as_text=True)


if __name__ == '__main__':
    import sys
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db(globals())
    sys.exit(pytest.main([__file__, '-v']))
