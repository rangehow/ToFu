"""Project-root adapter contract for the typed conversation-first surface.

The retired CollabBar markup is not the contract. The current presence adapter
must still resolve an armed New Chat project through the same projectState
fallback used by the composer, then pass that root to the typed surface.
"""
from __future__ import annotations

import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import ROOT, runtime_section_path

pytestmark = pytest.mark.unit


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const root = process.argv[2];
const {JSDOM} = require(path.join(root, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!doctype html><body><div id="presenceStrip"></div><div id="convList"></div></body>', {url: 'http://localhost'});
global.window = dom.window;
global.document = dom.window.document;
global.runtimeScope = {};
global.conversations = [];
global.activeConvId = null;
global.projectState = {active: true, path: '/proj/B', extraRoots: []};
global.getActiveConv = () => conversations.find(row => row.id === activeConvId) || null;
global._getConvProjectPath = row => row?.projectPath || '';
global.getActiveFolderId = () => null;
global.Api = {project: {}};
global.t = key => key;
global._projectConversationNavigation = {checkpoint: () => () => true};
const scopes = [];
global.createProjectCoordinationSurface = ports => {
  scopes.push(ports);
  return {refresh() {}, destroy() {}, coordinatorId() { return ''; }};
};
global.createSameTaskReadJoin = () => (_key, _signal, read) => read({});
const emptyEntry = () => ({
  refresh() {}, destroy() {}, home() { return ''; }, location() { return ''; },
  conversationId() { return ''; }, ensure() {},
});
global.createGlobalRouterEntry = emptyEntry;
global.createRouterWatchdogEntry = emptyEntry;
global.createRouterOwnerDelegation = emptyEntry;
const groupPorts = [];
global.createCoordinatorConversationGroups = ports => {
  groupPorts.push(ports);
  return {refresh() {}, destroy() {}};
};
// Sidebar status inputs: a row's activity flags and its hydrated Turns. The
// composed section binds runtimeScope to the window, so the read port lives there.
global._convStatusFlags = row => row.flags || {};
window.ConversationTurnRead = {ordered: row => row.turns || []};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const out = [];
function check(name, condition) { out.push((condition ? 'PASS ' : 'FAIL ') + name); }
conversations = [{id: 'saved', projectPath: '/proj/A'}];
activeConvId = 'saved';
check('active_conversation_project', scopes[0].scope().path === '/proj/A');
activeConvId = null;
check('armed_new_chat_project', scopes[0].scope()?.path === '/proj/B');
check('new_chat_has_no_conversation_identity', scopes[0].scope()?.currentConversationId === '');
projectState = {active: false, path: '', extraRoots: []};
check('cleared_project_has_no_surface_scope', scopes[0].scope() === null);
// The Coordinator's Worker rows read their status from the group's status port.
const status = groupPorts[0]?.status;
check('roster_status_port_bound', typeof status === 'function');
check('roster_status_running', status?.({flags: {streaming: true}}) === 'running');
check('roster_status_done', status?.({flags: {}, turns: [{turnId: 't'}]}) === 'done');
check('roster_status_unopened_shell', status?.({flags: {}, turns: []}) === null);
console.log(out.join('\n'));
dom.window.close();
"""


def _run(tmp_path, presence_src: str | None = None) -> str:
    require_node_modules('jsdom')
    proc = subprocess.run(
        ['node', '-e', _HARNESS,
         presence_src or runtime_section_path('presence.js'), str(ROOT)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def test_project_surface_uses_armed_new_chat_project(tmp_path):
    output = _run(tmp_path)
    for name in ('active_conversation_project', 'armed_new_chat_project',
                 'new_chat_has_no_conversation_identity', 'cleared_project_has_no_surface_scope'):
        assert f'PASS {name}' in output, output


def test_coordinator_roster_reads_the_shared_status_owner(tmp_path):
    """Worker rows under their Coordinator label 运行中/已完成 through the status port."""
    output = _run(tmp_path)
    for name in ('roster_status_port_bound', 'roster_status_running',
                 'roster_status_done', 'roster_status_unopened_shell'):
        assert f'PASS {name}' in output, output


def test_projectstate_fallback_is_load_bearing(tmp_path):
    presence_js = runtime_section_path('presence.js')
    src = (tmp_path / 'presence.js')
    original = open(presence_js, encoding='utf-8').read()
    needle = "if (!path && typeof projectState !== 'undefined' && projectState?.active) {"
    assert original.count(needle) == 1, 'projectState fallback drifted'
    src.write_text(original.replace(needle, 'if (!path && false) {', 1),
                   encoding='utf-8')
    output = _run(tmp_path, str(src))
    assert 'FAIL armed_new_chat_project' in output, output
    assert 'PASS active_conversation_project' in output, output
    assert 'PASS cleared_project_has_no_surface_scope' in output, output
    assert open(presence_js, encoding='utf-8').read() == original
