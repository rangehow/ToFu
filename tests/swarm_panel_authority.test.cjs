/* Run: node --test tests/swarm_panel_authority.test.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const root = require('node:path').resolve(__dirname, '..');
function runtime() {
  const context = vm.createContext({ console, Date, Set, Map,
    escapeHtml: String, t: key => key, renderMarkdown: String,
    _toolIcon: () => '', _toolDisplayName: String,
    _swEnsureTicker: () => {}, _swDemandReconciliation: () => {},
    SWARM_RECONCILIATION_POLICY: { intervalMs: 20000, fastMs: 1000 },

    createSwarmReconciliationScheduler: () => ({}),
  });
  for (const name of ['streaming_swarm_panel', 'swarm_live_projection']) {
    vm.runInContext(fs.readFileSync(`${root}/frontend/src/runtime/sections/ui/${name}.js`, 'utf8'), context);
  }
  vm.runInContext('_swEnsureTicker = () => {}; _swDemandReconciliation = () => {}; _swScheduleUnconfirmedProbe = () => {};', context);
  return context;
}
const agent = (id, status) => ({ id, role: 'review', status, preview: status === 'completed' ? 'final answer' : '' });
const terminal = () => ({ toolName: 'spawn_agents', _swarm: true, roundNum: 1, toolCallId: 'launch-1', taskId: 'exec-1', _swarmActive: true,
  _swarmAgents: [agent('a', 'pending'), agent('b', 'running')],
  _swarmSnapshot: { settled: true, swarm_id: 'exec-1', agents: [agent('a', 'completed'), agent('b', 'completed')] } });
test('settled roster wins over stale live cards, equally after reload; rendering is read-only', () => {
  const c = runtime(), round = terminal(), before = JSON.stringify(round);
  const html = c._buildSwarmPanelHTML(round, [round]);
  assert.match(html, /2\/2 agents finished/);
  assert.match(html, /> Complete</);
  assert.match(html, /final answer/);
  assert.doesNotMatch(html, /sw-a-running|sw-a-pending|data-sw-start=/);
  assert.equal(JSON.stringify(round), before);
  delete round._swarmAgents;
  assert.equal(c._buildSwarmPanelHTML(round, [round]), html);
});
test('aborted, stalled and partially unresolved rosters never claim Complete', () => {
  const c = runtime();
  for (const status of ['aborted', 'stalled', 'unknown']) {
    const round = terminal(); round._swarmSnapshot.agents[1].status = status;
    const html = c._buildSwarmPanelHTML(round, [round]);
    assert.doesNotMatch(html, /> Complete</);
    assert.match(html, status === 'aborted' ? /2\/2 agents finished/ : /1\/2 agents finished/);
  }
});
function overlayModule() {
  const source = fs.readFileSync(`${root}/frontend/src/conversation/application/swarm-presentation-overlay.ts`, 'utf8');
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, Date, Map, Set, structuredClone });
  return exports;
}
const turn = rounds => ({ turnId: 't', actor: 'assistant', status: 'completed', projectionRevision: 1, projection: { toolRounds: rounds } });
test('durable terminal round discards stale presentation and provisional launch binds without duplicate', () => {
  const { swarmPresentationOverlay: overlay } = overlayModule();
  const durable = terminal(), previous = { ...durable, _swarmAgents: [agent('a', 'running')], status: 'searching' };
  delete previous._swarmSnapshot;
  const result = overlay.rebase(turn([durable]), turn([previous]));
  assert.equal(result.projection.toolRounds[0].status, durable.status);
  const provisional = { _swarm: true, toolName: 'spawn_agents', roundNum: 99, taskId: 'exec-2' };
  const rounds = [terminal(), { toolName: 'await_agents', roundNum: 2 }, { toolName: 'get_agent_result', roundNum: 3 },
    { toolName: 'spawn_agents', taskId: 'exec-2', roundNum: 4, toolCallId: 'launch-2' }];
  const rebased = overlay.rebase(turn(rounds), turn([provisional]));
  assert.equal(rebased.projection.toolRounds.length, 4);
  assert.equal(rebased.projection.toolRounds.filter(r => r._swarm).length, 2);
});
test('successive execution uses actual launch; late events cannot resurrect terminal roster', () => {
  const c = runtime(), old = terminal();
  const current = { toolName: 'spawn_agents', roundNum: 4, taskId: 'exec-2', toolCallId: 'launch-2' };
  const projection = { _swarmRoundNum: 1, toolRounds: [old, {toolName: 'await_agents'}, {toolName: 'get_agent_result'}, current] };
  const context = { convId: 'conv', taskId: 'exec-2', assistantProjection: projection };
  c._handleSwarmPhase({ type: 'swarm_phase', phase: 'spawning', swarmId: 'exec-2', agents: [{id:'c'}] }, context);
  assert.equal(projection.toolRounds.length, 4);
  assert.equal(current._swarmAgents[0].id, 'c');
  const before = JSON.stringify(old);
  c._handleSwarmAgent({type: 'swarm_agent_phase', swarmId: 'exec-1', agentId: 'a', phase: 'thinking'}, context);
  assert.equal(JSON.stringify(old), before);
  assert.equal(current._swarmAgents.length, 1);
});

function pushHarness(turns) {
  const c = runtime(), overlays = new Map();
  const state = { turnsById: Object.fromEntries(turns.map(t => [t.turnId, t])), laneOrder: { main: turns.map(t => t.turnId) } };
  const controller = overlayModule().createSwarmPushPresentationController({
    findConversation: id => ({ id }), readTurnState: () => state,
    readOverlay: (_conv, id) => overlays.get(id) || null,
    upsertOverlay: (_conv, value) => overlays.set(value.turnId, value),
    removeOverlay: (_conv, id) => overlays.delete(id),
    hydrateConversation: () => null, attachAutoContinue() {},
    reducePhase: (frame, context) => c._handleSwarmPhase(frame, context),
    reduceAgent: (frame, context) => c._handleSwarmAgent(frame, context),
    debug: (_message, error) => { throw error; }, warn() {},
  });
  return { overlays, controller, send: frame => controller.handleFrame({ type: 'swarm_phase', convId: 'conv', ...frame }) };
}
function freezeDeep(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freezeDeep);
    Object.freeze(value);
  }
  return value;
}
test('late launch phases for a durably settled run never create an overlay on old or current turns', () => {
  for (const phase of ['planning', 'spawning', 'spawn_more']) {
    for (const newer of [false, true]) {
      const old = turn([terminal()]);
      const turns = freezeDeep(newer ? [old, { ...turn([]), turnId: 'current', status: 'running' }] : [old]);
      const before = JSON.stringify(turns), h = pushHarness(turns);
      h.send({ phase, swarmId: 'exec-1', agents: [{ id: 'late' }] });
      assert.equal(h.overlays.size, 0, `${phase}, newer=${newer}`);
      assert.equal(JSON.stringify(turns), before);
    }
  }
});
test('new launch IDs remain valid on completed parents and before tool result settlement', () => {
  for (const phase of ['planning', 'spawning', 'spawn_more']) {
    for (const toolStarted of [false, true]) {
      const rounds = [terminal()];
      if (toolStarted) rounds.push({ toolName: 'spawn_agents', taskId: 'exec-new', toolCallId: 'new-call', roundNum: 2 });
      const durable = freezeDeep(turn(rounds)), before = JSON.stringify(durable), h = pushHarness([durable]);
      h.send({ phase, swarmId: 'exec-new', agents: [{ id: 'new-agent' }] });
      const first = h.overlays.get('t'), firstBefore = JSON.stringify(first);
      assert.equal(first.projection.toolRounds.length, 2);
      assert.equal(first.projection.toolRounds[1]._swarmAgents[0].id, 'new-agent');
      h.send({ phase: 'spawn_more', swarmId: 'exec-new', agents: [{ id: 'followup' }] });
      const updated = h.overlays.get('t');
      assert.equal(updated.projection.toolRounds.length, 2);
      assert.equal(updated.projection.toolRounds[1]._swarmAgents.length, 2);
      assert.equal(JSON.stringify(first), firstBefore, 'previous overlay is immutable');
      assert.equal(JSON.stringify(durable), before, 'durable input is immutable');
    }
  }
});
test('rebase binds repeated call IDs by scoped occurrence without mutating either input', () => {
  const overlay = overlayModule().swarmPresentationOverlay;
  const scopes = [{ attemptId: 'old' }, { attemptId: 'new' }, { taskId: 'task' }, {}];
  const rounds = scopes.flatMap((scope, i) => [0, 1].map(n => ({ ...scope, toolCallId: 'reused', toolName: 'inspect_image', toolContent: `${i}:${n}`, llmRound: 0 })));
  // Segment order differs across scopes: global occurrence counters would bind
  // the new attempt to the old one, while scalar maps repeat its first crop.
  const segments = [1, 0, 2, 3].flatMap(i => [0, 1, 2].map(n => ({ ...scopes[i], type: 'tool_use', id: 'reused', llmRound: 0, expected: n < 2 ? `${i}:${n}` : undefined })));
  segments.push({ type: 'tool_use', id: 'missing', llmRound: 0, attemptId: 'new' });
  const durable = freezeDeep({ ...turn(rounds), projection: { toolRounds: rounds, segments } });
  const previous = freezeDeep(turn([{ _swarm: true, toolName: 'spawn_agents', toolCallId: 'launch-overlay' }]));
  const before = JSON.stringify([durable, previous]);
  const result = overlay.rebase(durable, previous);
  for (const segment of result.projection.segments) {
    assert.equal(segment._round?.toolContent, segment.expected);
    if (segment._round) assert.ok(result.projection.toolRounds.includes(segment._round));
  }
  assert.equal(JSON.stringify([durable, previous]), before);
});
