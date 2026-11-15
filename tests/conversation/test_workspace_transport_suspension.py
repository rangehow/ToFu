"""Hidden workspace streams release browser sockets without cancelling work."""
from pathlib import Path
import json
import subprocess
import pytest

from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def run_node(tmp_path, source, harness):
    require_node_modules('vite')
    built = tmp_path / 'owner.cjs'
    result = subprocess.run([
        str(ROOT / 'scripts/vite_test_bundle.mjs'), str(ROOT / source),
        '--bundle', '--format=cjs', '--platform=node', f'--outfile={built}',
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(['node', '-e', harness.replace('BUILT', json.dumps(str(built)))],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout


PRELUDE = r'''
const assert = require('node:assert/strict');
global.window = globalThis;
const streams = [];
class Source extends EventTarget {
  constructor(request) { super(); this.request = request; this.closed = false; streams.push(this); }
  close() { this.closed = true; }
}
const snapshot = id => ({
  ok:true, contract:'tofu.conversation-sync.snapshot/v1', conversationId:id,
  conversationRevision:1, syncSeq:7, cursor:'cursor-7', serverBootId:'boot-test',
  heartbeatIntervalMs:15000, settings:{}, turns:[], attempts:[], queueItems:[],
});
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
'''


def test_coordinator_suspension_fences_late_hydrate_and_wake(tmp_path):
    run_node(tmp_path, 'frontend/src/core/conversation-sync.ts', PRELUDE + r'''
const { ConversationSyncCoordinator } = require(BUILT);
(async () => {
  let reads = 0, release;
  let gate = Promise.resolve();
  const coordinator = new ConversationSyncCoordinator({
    conversationId:'one',
    api:{snapshot:async id => { reads++; await gate; return snapshot(id); }},
    onSnapshot(){}, onAttemptEvent(){return true;}, onTurnDelta(){return true;},
    eventSourceFactory:request => new Source(request),
  });
  await coordinator.resume();
  assert.equal(streams.length, 1);
  coordinator.setSuspended(true);
  assert.equal(streams[0].closed, true);
  await coordinator.resume(); await coordinator.wake(); coordinator.invalidate();
  await tick();
  assert.equal(streams.length, 1);
  assert.equal(reads, 1);
  coordinator.setSuspended(false);
  assert.equal(streams.length, 2);
  assert.equal(streams[1].request.conversationId, 'one');
  assert.equal(streams[1].request.after, 'cursor-7');
  assert.equal(reads, 1, 'resume must use replay cursor without redundant snapshot');
  gate = new Promise(resolve => {release = resolve;});
  const hydration = coordinator.hydrate(true);
  coordinator.setSuspended(true);
  release(); await hydration;
  assert.equal(streams.length, 2, 'late snapshot cannot reopen hidden transport');
  coordinator.pause(); coordinator.setSuspended(false);
  assert.equal(streams.length, 2, 'pausing demand while hidden must be retained');
  coordinator.setSuspended(true); await coordinator.wake();
  coordinator.setSuspended(false);
  assert.equal(streams.length, 3);
  coordinator.close(); coordinator.setSuspended(true); coordinator.setSuspended(false);
  assert(streams.every(stream => stream.closed));
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


def test_runtime_suspends_all_existing_and_future_conversations(tmp_path):
    run_node(tmp_path, 'frontend/src/core/turn-runtime.ts', PRELUDE + r'''
const { createConversationTurnRuntime } = require(BUILT);
(async () => {
  let reads = 0;
  const runtime = createConversationTurnRuntime({
    api:{snapshot:async id => {reads++; return snapshot(id);}},
    eventSourceFactory:request => new Source(request), isActive:() => true,
  });
  for(let index=0; index<6; index++) await runtime.hydrateConversation({id:`conv-${index}`});
  assert.equal(streams.length, 6);
  runtime.setTransportSuspended(true);
  assert(streams.every(stream => stream.closed));
  await runtime.hydrateConversation({id:'late'});
  runtime.invalidateConversation('conv-1');
  runtime.reconcileConversationActivity('conv-1', 'late');
  await tick();
  assert.equal(streams.length, 6, 'new coordinators inherit suspended lifecycle');
  runtime.setTransportSuspended(false);
  assert.equal(streams.length, 13);
  assert.equal(reads, 7);
  runtime.setTransportSuspended(false);
  assert.equal(streams.length, 13, 'repeated activation is idempotent');
  for(const id of ['late', ...Array.from({length:6},(_,i)=>`conv-${i}`)]) runtime.disposeConversation(id);
  assert(streams.every(stream => stream.closed));
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


def test_world_keeps_the_chat_column_in_flow_and_reannounces_for_its_dock(tmp_path):
    """The office docks the classic column by a body attribute (01-workspace-dock.css): page-host keeps the
    column in flow for the world, routes #/world, and announces the world again whenever the office
    re-shows it after a dock change, so the shell recomputes the column's transcript transport."""
    run_node(tmp_path, 'frontend/src/application-shell/page-host.ts', r'''
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<body><div class="chat-wrapper"></div><div class="input-area"></div></body>', {url:'http://localhost/#/chat'});
global.window = dom.window; global.document = dom.window.document;
const {installWorkspaceNavigation, showWorkspace} = require(BUILT);
const seen=[], loaded=[];
const dispose = installWorkspaceNavigation(async page => { loaded.push(page); showWorkspace(page); }, () => {}, page => seen.push(page));
const column = () => [document.querySelector('.chat-wrapper').style.display, document.querySelector('.input-area').style.display];
window.history.replaceState(null,'','#/world');
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(loaded, ['world'], '#/world is a workspace route');
  assert.equal(document.body.dataset.workspace, 'world');
  assert.deepEqual(column(), ['', ''], 'the world keeps the column in flow; its dock shows or hides it');
  const before = seen.length;
  document.body.dataset.conversationDock = 'chat';
  showWorkspace('world', false);
  assert.equal(seen.length, before + 1, 'a dock change re-announces the world');
  assert.equal(seen.at(-1), 'world');
  assert.equal(window.location.hash, '#/world', 'a re-announcement does not touch the address');
  showWorkspace('paper', false);
  assert.deepEqual(column(), ['none', 'none']);
  showWorkspace('chat', false);
  assert.deepEqual(column(), ['', '']);
  dispose();
  dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


def test_workspace_navigation_releases_before_lazy_load_and_restores_failure(tmp_path):
    run_node(tmp_path, 'frontend/src/application-shell/page-host.ts', r'''
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<body></body>', {url:'http://localhost/#/chat'});
global.window = dom.window; global.document = dom.window.document;
const {installWorkspaceNavigation, showWorkspace} = require(BUILT);
const events=[];
const dispose = installWorkspaceNavigation(async page => {
  events.push(`load:${page}`); throw new Error('offline');
}, () => events.push('error'), page => events.push(`visible:${page}`));
window.history.replaceState(null,'','#/paper');
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
(async()=>{
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(events.slice(-4), ['visible:paper','load:paper','visible:chat','error']);
  showWorkspace('paper');
  assert.equal(events.at(-1),'visible:paper');
  showWorkspace('chat');
  assert.equal(events.at(-1),'visible:chat');
  dispose();
  const count=events.length;
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  assert.equal(events.length,count);
  dom.window.close();
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
