/* Real retained sidebar -> loadConversation -> composer adapters, with UI/network ports only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const {test} = require('node:test');
const base = 'frontend/src/runtime/sections/';
/* Real retained source for named function declarations and top-level
 * constants, so the harness runs the shipped wiring instead of a copy. */
function declarations(path, names) {
  const text = fs.readFileSync(base + path, 'utf8');
  const ast = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const found = new Map();
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && names.includes(node.name?.text)) {
      found.set(node.name.text, node.getText(ast));
    }
    if (ts.isVariableStatement(node) && node.parent === ast) {
      for (const declaration of node.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name) && names.includes(declaration.name.text)) {
          found.set(declaration.name.text, node.getText(ast));
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.deepEqual([...found.keys()].sort(), [...names].sort(), `${path} declarations`);
  return names.map(name => found.get(name)).join('\n');
}
function typed(name, directory = 'frontend/src/conversation/application') {
  const module = {exports: {}};
  const source = fs.readFileSync(`${directory}/${name}.ts`, 'utf8');
  new Function('module', 'exports', ts.transpileModule(source, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  }}).outputText)(module, module.exports);
  return module.exports;
}
function harness({cold = false, text = '', attachment = false, staleCleanup = false,
  outgoing = null, busy = false, turns = 1, catalog = null} = {}) {
  const input = {value: text};
  const timers = [], writes = [], reconciliations = [];
  const settings = {searchMode: 'multi', fetchEnabled: false, codeExecEnabled: false,
    browserEnabled: false, desktopEnabled: false, memoryEnabled: false,
    schedulerEnabled: false, autopilotEnabled: false, activeFlow: '',
    imageGenEnabled: false, imageGenMode: false, humanGuidanceEnabled: false,
    autoTranslate: false};
  const ctx = {console, Promise, setTimeout: (fn, delay) => timers.push({fn, delay}), Date,
    ...settings, _igSelectedModel: '', _igSelectedProviderId: '', _igSelectedCount: 1,
    _igSelectedAspect: '1:1', _igSelectedResolution: '1K',
    convIsBusy: () => busy,
    persistConversationSettings: conv => writes.push({...conv}),
    reconcileConversationCatalogMetadata: id => reconciliations.push(id),
    config: {model: 'gpt-6-astra'}, activeConvId: null,
    conversations: catalog || [{id: 'apk', title: 'APK', model: 'kimi-k3', projectPath: '/your-username/apk',
      _turnSnapshotRequired: cold}],
    projectState: {active: true, path: '/your-username/tofu'},
    pendingImages: attachment ? [{}] : [], pendingPdfTexts: [], pendingVideos: [],
    _pendingLogClean: staleCleanup ? {originalText: 'old pasted logs'} : null,
    document: {getElementById: id => id === 'userInput' ? input : {textContent: ''},
      querySelectorAll: () => [], querySelector: () => null},
    sessionStorage: {setItem() {}}, runtimeScope: {requestAuthoritativeConversationRender() {},
      ConversationTurnStore: {wakeConversation: async () => {}},
      ConversationTurnRead: {ordered: () => Array.from({length: turns}, () => ({}))}},
    getActiveConv: () => ctx.conversations.find(c => c.id === ctx.activeConvId) || null, _purgeEmptyConvs() {}, _swapActiveConvItem: () => true,
    _conversationDisplayTitle: x => x, t: x => x,
    _resumePendingTranslations() {}, renderPendingQueueUI() {}, _refreshServerQueue() {},
    updateSendButton() {}, debugLog() {}, _updateProjectUI() {},

    renderComposerModelNotice() {}, holdComposerModelNotice() {},

    composerTranslationStatus: {refresh() {}},
    normalizeConversationInteractionModes: () => ({agentMode: 'standard', activeFlow: ''}),
    convAutoTranslate: () => false, _scheduleReflow() {},
    _applyModelUI: model => {ctx.config.model = model;},
    Api: {project: {status: async () => null}},
    ...typed('conversation-model'), ...typed('conversation-project'),
    ...typed('project-conversation-navigation', 'frontend/src/features'),
  };
  if (outgoing) {
    ctx.conversations.push({id: 'outgoing', title: 'Outgoing', updatedAt: 123,
      model: 'saved-model', projectPath: '/saved-project', ...settings, ...outgoing});
    ctx.activeConvId = 'outgoing';
  }
  for (const name of ['SearchMode', 'FetchEnabled', 'CodeExec', 'Browser', 'Desktop', 'Memory',
    'AgentMode', 'Flow', 'ImageGenTool', 'ImageGen', 'HumanGuidance', 'AutoTranslate']) ctx[`_apply${name}UI`] = () => {};
  ctx._updateAutoApplyUI = () => {};
  let release;
  ctx.hydrateConversationRuntime = async () => {
    await new Promise(resolve => {release = resolve;});
    ctx.conversations[0]._turnSnapshotRequired = false;
  };
  vm.createContext(ctx);
  vm.runInContext([
    declarations('project_state.js', ['_hasProjectComposerDraft', '_conversationProjectController',
      '_restoreConvProject', '_enrichConvProjectContext', 'loadProjectStatus',
      '_saveConvProjectPath', 'clearProject']),
    declarations('main.js', ['_conversationModelController', 'restoreConversationSettingsToComposer']),
    declarations('main/main_conv_lifecycle.js', ['_projectConversationNavigation', 'loadConversation']),
  ].join('\n'), ctx);
  const listeners = new Map();
  typed('sidebar', 'frontend/src/application-shell/interactions').installSidebarInteractions(
    {add() {}, listen: (_host, type, listener) => listeners.set(type, listener)},
    {performance: {now: () => 0}}, {}, {activate: id => ctx.loadConversation(id)},
  );
  return {ctx, input, timers, writes, reconciliations,
    flushTimers() { for (const timer of timers.splice(0)) timer.fn(); }, open() {
    listeners.get('pointerdown')({pointerType: 'mouse', button: 0, target: {
      nodeType: 1,
      closest: selector => selector === '.conv-item' ? {dataset: {convId: 'apk'}} : null,
    }});
  }, release: () => release?.()};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
for (const cold of [false, true]) {
  test(`empty composer restores both saved selections despite stale cleanup (cold=${cold})`, async () => {
    const h = harness({cold, staleCleanup: true});
    h.open(); h.release(); await settle();
    assert.equal(h.ctx.config.model, 'kimi-k3');
    assert.equal(h.ctx.projectState.path, '/your-username/apk');
  });
  for (const draft of [{text: 'unsent'}, {attachment: true}]) {
    // Model restore is draft-gated; saved roots are projected with no draft
    // gate (docs/FRONTEND_ARCHITECTURE.md "Conversation workspace projection").
    test(`real sidebar keeps the draft model while roots follow the conversation ${JSON.stringify(draft)} (cold=${cold})`, async () => {
      const h = harness({cold, ...draft});
      h.open(); h.release(); await settle();
      assert.equal(h.ctx.config.model, 'gpt-6-astra');
      assert.equal(h.ctx.projectState.path, '/your-username/apk');
    });
  }
}

test('warm sidebar selection projects saved roots and requests enrichment once', async () => {
  const h = harness();
  let projections = 0, enrichments = 0;
  h.ctx._updateProjectUI = () => { projections += 1; };
  h.ctx.Api.project.status = async () => { enrichments += 1; return null; };
  h.open(); await settle();
  assert.equal(h.ctx.projectState.path, '/your-username/apk');
  assert.equal(projections, 1);
  assert.equal(enrichments, 1);
});

test('cold sidebar selection reprojects authoritative hydrated roots', async () => {
  const h = harness({cold: true});
  const roots = [];
  h.ctx._updateProjectUI = () => roots.push(h.ctx.projectState.path);
  h.open();
  h.ctx.conversations[0].projectPath = '/authoritative-root';
  h.release(); await settle();
  assert.deepEqual(roots, ['/your-username/apk', '/authoritative-root']);
  assert.equal(h.ctx.projectState.path, '/authoritative-root');
});

/* The same conversation can be visited again (A -> B -> A), so its ID is not
 * a visit token: a late enrichment response from the first visit must not
 * overwrite the current visit's newer branch context. */
test('late first-visit enrichment cannot overwrite the current visit of the same conversation', async () => {
  const pending = [];
  const h = harness({catalog: [
    {id: 'alpha', title: 'Alpha', projectPath: '/p/alpha', projectPaths: ['/p/alpha'],
      _turnSnapshotRequired: false},
    {id: 'beta', title: 'Beta', projectPath: '/p/beta', projectPaths: ['/p/beta'],
      _turnSnapshotRequired: false},
  ]});
  const applied = [];
  h.ctx.runtimeScope.applyProjectGitState = data => applied.push(data.gitBranch);
  h.ctx.Api.project.status = id => new Promise(resolve => pending.push({id, resolve}));
  h.ctx.loadConversation('alpha');
  h.ctx.loadConversation('beta');
  h.ctx.loadConversation('alpha');
  assert.deepEqual(pending.map(request => request.id), ['alpha', 'beta', 'alpha']);
  // The current A visit answers first and is a successful enrichment.
  pending[2].resolve({gitBranch: 'alpha-current'});
  await settle(); await settle();
  assert.deepEqual(applied, ['alpha-current']);
  assert.equal(h.ctx.activeConvId, 'alpha');
  // The first A visit and the intervening B visit answer late.
  pending[0].resolve({gitBranch: 'alpha-stale'});
  pending[1].resolve({gitBranch: 'beta-stale'});
  await settle(); await settle();
  assert.deepEqual(applied, ['alpha-current']);
});

for (const cold of [false, true]) {
  test(`sidebar selection uses snapshot/stream queue without another full read (cold=${cold})`, async () => {
    const h = harness({cold});
    let duplicateReads = 0;
    h.ctx._refreshServerQueue = () => { duplicateReads += 1; };
    h.open(); h.release(); await settle();
    assert.equal(duplicateReads, 0);
  });
}

for (const cold of [false, true]) {
  test(`unchanged outgoing settings skip all deferred persistence work (cold=${cold})`, async () => {
    const h = harness({cold, outgoing: {}, text: 'unsent'});
    const outgoing = h.ctx.getActiveConv();
    const before = {...outgoing};
    h.open(); h.release(); await settle();
    assert.deepEqual(outgoing, before);
    assert.equal(h.timers.length, 0);
    assert.deepEqual(h.writes, []);
    assert.deepEqual(h.reconciliations, []);
    assert.equal(h.ctx.activeConvId, 'apk');
    assert.equal(h.ctx.config.model, 'gpt-6-astra');
    assert.equal(h.ctx.projectState.path, '/your-username/apk');
  });
  test(`changed outgoing tools persist only after switch without activity bump (cold=${cold})`, async () => {
    const h = harness({cold, outgoing: {fetchEnabled: true}, attachment: true});
    const outgoing = h.ctx.getActiveConv();
    h.open(); h.release(); await settle();
    assert.equal(outgoing.fetchEnabled, false);
    assert.equal(outgoing.model, 'saved-model');
    assert.equal(outgoing.projectPath, '/saved-project');
    assert.equal(outgoing.updatedAt, 123);
    assert.equal(h.ctx.activeConvId, 'apk');
    assert.equal(h.timers.length, 1);
    assert.equal(h.timers[0].delay, 0);
    assert.deepEqual(h.writes, []);
    h.flushTimers();
    assert.deepEqual(h.reconciliations, [null]);
    assert.equal(h.writes.length, 1);
    assert.equal(h.writes[0].id, 'outgoing');
    assert.equal(h.writes[0].fetchEnabled, false);
    assert.equal(h.writes[0].updatedAt, 123);
  });
}

for (const cold of [false, true]) {
  /* Clearing the notice on selection collapsed the composer for the whole
   * load and grew it back at first paint, under the freshly pinned tail. */
  test(`selection holds the outgoing model notice for the first commit to replace (cold=${cold})`, async () => {
    const h = harness({cold, outgoing: {}});
    const notices = [];
    h.ctx.holdComposerModelNotice = () => notices.push('hold');
    h.ctx.renderComposerModelNotice = (_node, view) => notices.push(view ? 'render' : 'clear');
    h.open(); h.release(); await settle();
    assert.deepEqual(notices, ['hold']);
  });
}

test('a failed cold load releases the held model notice of its own selection only', async () => {
  for (const reselect of [false, true]) {
    const h = harness({cold: true, outgoing: {}});
    const notices = [];
    h.ctx.holdComposerModelNotice = () => notices.push('hold');
    h.ctx.renderComposerModelNotice = (_node, view) => notices.push(view ? 'render' : 'clear');
    h.ctx.console = {...console, error() {}};
    h.ctx.hydrateConversationRuntime = async () => { throw new Error('offline'); };
    h.open();
    if (reselect) h.ctx.activeConvId = 'outgoing';
    await settle();
    assert.deepEqual(notices, reselect ? ['hold'] : ['hold', 'clear']);
  }
});

test('busy outgoing translation remains frozen and does not create a write', () => {
  const h = harness({outgoing: {autoTranslate: true}, busy: true});
  const outgoing = h.ctx.getActiveConv();
  h.open();
  assert.equal(outgoing.autoTranslate, true);
  assert.equal(h.timers.length, 0);
});

test('idle outgoing translation change is persisted', () => {
  const h = harness({outgoing: {autoTranslate: true}});
  h.open(); h.flushTimers();
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].autoTranslate, false);
});

test('disabled image generation preserves saved image settings without a write', () => {
  const h = harness({outgoing: {imageGenModel: 'saved-image', imageGenCount: 4}});
  const outgoing = h.ctx.getActiveConv();
  h.open();
  assert.equal(outgoing.imageGenModel, 'saved-image');
  assert.equal(outgoing.imageGenCount, 4);
  assert.equal(h.timers.length, 0);
});

test('enabled image generation captures changed selection through deferred persistence', () => {
  const h = harness({outgoing: {imageGenMode: true, imageGenModel: 'saved-image'}});
  h.ctx.imageGenMode = true;
  h.ctx._igSelectedModel = 'new-image';
  h.ctx._igSelectedCount = 3;
  h.open();
  assert.deepEqual(h.writes, []);
  h.flushTimers();
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].imageGenModel, 'new-image');
  assert.equal(h.writes[0].imageGenCount, 3);
});

test('changed empty outgoing shell reconciles metadata but does not upload settings', () => {
  const h = harness({outgoing: {fetchEnabled: true}, turns: 0});
  h.open(); h.flushTimers();
  assert.deepEqual(h.reconciliations, [null]);
  assert.deepEqual(h.writes, []);
});

for (const staleFirst of [true, false]) {
  for (const replacement of ['revisit', 'choice', 'clear']) {
    test(`status refresh rejects earlier view (${replacement}, staleFirst=${staleFirst})`, async () => {
      const h = harness({catalog: [
        {id: 'alpha', title: 'Alpha', projectPath: '/p/alpha'},
        {id: 'beta', title: 'Beta', projectPath: '/p/beta'},
      ]});
      const pending = [], applied = [];
      h.ctx.Api.project.status = id => new Promise(resolve => pending.push({id, resolve}));
      h.ctx.runtimeScope.applyProjectGitState = data => applied.push(data.gitBranch);
      h.ctx.loadConversation('alpha');
      h.ctx.loadProjectStatus();
      const oldStatus = pending[1];
      if (replacement === 'revisit') {
        h.ctx.loadConversation('beta');
        h.ctx.loadConversation('alpha');
      } else if (replacement === 'choice') {
        h.ctx._saveConvProjectPath('/p/chosen', ['/p/extra'], ['/p/extra']);
      } else {
        await h.ctx.clearProject();
      }
      const expectedRoot = replacement === 'choice' ? '/p/chosen'
        : replacement === 'clear' ? '' : '/p/alpha';
      const current = pending.at(-1);
      assert.deepEqual(pending.map(x => x.id), replacement === 'revisit'
        ? ['alpha', 'alpha', 'beta', 'alpha'] : ['alpha', 'alpha', 'alpha']);
      if (staleFirst) {
        oldStatus.resolve({gitBranch: 'stale', path: '/foreign'});
        await settle();
        assert.deepEqual(applied, []);
      }
      current.resolve({gitBranch: 'current', path: '/foreign'});
      await settle();
      assert.deepEqual(applied, ['current']);
      if (!staleFirst) {
        oldStatus.resolve({gitBranch: 'stale', path: '/foreign'});
        await settle();
        assert.deepEqual(applied, ['current']);
      }
      assert.equal(h.ctx.projectState.path, expectedRoot);
      if (replacement === 'choice') {
        assert.equal(h.ctx.projectState.extraRoots[0].path, '/p/extra');
        assert.equal(h.ctx.projectState.extraRoots[0].readOnly, true);
      }
    });
  }
}

for (const staleFirst of [true, false]) {
  test(`cold enrichment belongs to its hydration phase (staleFirst=${staleFirst})`, async () => {
    const h = harness({cold: true});
    const pending = [], applied = [];
    h.ctx.Api.project.status = id => new Promise(resolve => pending.push({id, resolve}));
    h.ctx.runtimeScope.applyProjectGitState = data => applied.push(data.gitBranch);
    h.open();
    h.ctx.loadProjectStatus();
    h.ctx.conversations[0].projectPath = '/hydrated';
    h.ctx.conversations[0].projectPaths = ['/hydrated', '/hydrated-extra'];
    h.ctx.conversations[0].readOnlyPaths = ['/hydrated-extra'];
    h.release(); await settle();
    assert.deepEqual(pending.map(x => x.id), ['apk', 'apk', 'apk']);
    const stale = () => {
      pending[0].resolve({gitBranch: 'cold-stale'});
      pending[1].resolve({gitBranch: 'status-stale'});
    };
    if (staleFirst) { stale(); await settle(); assert.deepEqual(applied, []); }
    pending[2].resolve({gitBranch: 'hydrated-current', path: '/foreign'});
    await settle();
    assert.deepEqual(applied, ['hydrated-current']);
    if (!staleFirst) { stale(); await settle(); }
    assert.deepEqual(applied, ['hydrated-current']);
    assert.equal(h.ctx.projectState.path, '/hydrated');
    assert.equal(h.ctx.projectState.extraRoots[0].path, '/hydrated-extra');
    assert.equal(h.ctx.projectState.extraRoots[0].readOnly, true);
    h.ctx.loadProjectStatus();
    pending[3].resolve({gitBranch: 'refreshed-current'});
    await settle();
    assert.deepEqual(applied, ['hydrated-current', 'refreshed-current']);
    assert.equal(pending.length, 4);
  });
}

test('rejected old hydration cannot supersede the current enrichment', async () => {
  const h = harness({cold: true});
  const pending = [], applied = [];
  h.ctx.Api.project.status = id => new Promise(resolve => pending.push({id, resolve}));
  h.ctx.runtimeScope.applyProjectGitState = data => applied.push(data.gitBranch);
  h.open();
  h.ctx._saveConvProjectPath('/chosen');
  h.release(); await settle();
  assert.equal(pending.length, 2);
  assert.equal(h.ctx.projectState.path, '/chosen');
  pending[1].resolve({gitBranch: 'chosen-current'});
  pending[0].resolve({gitBranch: 'cold-stale'});
  await settle();
  assert.deepEqual(applied, ['chosen-current']);
});
