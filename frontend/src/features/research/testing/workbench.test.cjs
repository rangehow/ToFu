const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
function load(file, ports = {}, cache = new Map()) {
  const full = path.resolve(root, file);
  if (cache.has(full)) return cache.get(full);
  const exports = {};
  cache.set(full, exports);
  const js = ts.transpileModule(fs.readFileSync(full, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function('require', 'exports', js)((name) => {
    if (name in ports) return ports[name];
    return load(path.relative(root, path.resolve(path.dirname(full), name + '.ts')), ports, cache);
  }, exports);
  return exports;
}
test('telemetry retains editor identity, selection, disclosures and workspace', () => {
  const dom = new JSDOM('<div id="host"></div>');
  global.document = dom.window.document;
  global.Element = dom.window.Element;
  const { patchResearchDom } = load('components/stable-dom.ts');
  const host = document.getElementById('host');
  patchResearchDom(host, '<div><details><summary>Evidence</summary>old</details><textarea data-rsw-field="hypothesis">saved</textarea><section id="researchWorkspace">editor</section><span>1</span></div>');
  const editor = host.querySelector('textarea');
  editor.value = 'unsaved'; editor.focus(); editor.setSelectionRange(2, 4);
  host.querySelector('details').open = true;
  const workspace = host.querySelector('section');
  patchResearchDom(host, '<div><details><summary>Evidence</summary>new</details><textarea data-rsw-field="hypothesis">server</textarea><section id="researchWorkspace">loading</section><span>2</span></div>');
  assert.equal(host.querySelector('textarea'), editor);
  assert.equal(editor.value, 'unsaved');
  assert.equal(editor.selectionStart, 2);
  assert.equal(document.activeElement, editor);
  assert.equal(host.querySelector('details').open, true);
  assert.equal(host.querySelector('section'), workspace);
  assert.equal(workspace.textContent, 'editor');
  assert.equal(host.querySelector('span').textContent, '2');
  dom.window.close();
});
test('duplicate submit is gated and failed cancel never invents aborted state', async () => {
  const registry = { _researchModeActive: () => true, _paintResearch: () => {} };
  let resolveStart; let starts = 0; let reads = 0;
  registry.Api = { tasks: {
    start: () => { starts++; return new Promise((resolve) => { resolveStart = resolve; }); },
    get: async () => { reads++; return {ok: true, status: 'running'}; },
    abort: async () => ({ok: false, error: 'permission denied'}),
  }};
  global.window = { setTimeout: () => 1, clearTimeout: () => {} };
  const runtime = load('controllers/runtime.ts', {
    '../../../feature-registry': { featureRegistry: registry },
    '../../../i18n': { _i18nLang: 'en' },
    '../api/reader': { openResearchReader: async () => {} },
  });
  const launch = runtime.startResearchJob('test');
  assert.equal(registry._researchStream.status, 'submitting');
  await runtime.startResearchJob('duplicate');
  assert.equal(starts, 1);
  resolveStart({ok: true, taskId: 'task-1'});
  await launch;
  await runtime.abortResearchJob();
  assert.equal(registry._researchStream.status, 'running');
  assert.equal(registry._researchStream.commandError, 'permission denied');
  assert.equal(registry._researchStream.cancelling, false);
  const stream = registry._researchStream;
  const a = runtime.pollResearchOnce(stream); const b = runtime.pollResearchOnce(stream);
  assert.equal(a, b); await a;
  assert.equal(reads, 2);
  runtime.destroyResearchRuntime();
  assert.equal(registry._researchStream, null);
});

test('terminal push wins over late running snapshots and duplicate events', () => {
  const registry = {};
  const runtime = load('controllers/runtime.ts', {
    '../../../feature-registry': { featureRegistry: registry },
    '../../../i18n': { _i18nLang: 'en' },
  });
  const stream = runtime.newResearchStream('question');
  stream.status = 'running';
  runtime.researchIngestEvent(stream, {seq: 10, type: 'done', status: 'aborted'});
  assert.equal(stream.status, 'aborted');
  runtime.researchApplySnapshot(stream, {status: 'running', cursor: {next: 9}, result: {accepted: [{}]}});
  assert.equal(stream.status, 'aborted');
  assert.equal(stream.accepted, 0);
  assert.equal(runtime.researchIngestEvent(stream, {seq: 10, type: 'tool_start', toolCallId: 'late'}), false);
  assert.equal(stream.toolRounds.length, 0);
});

test('harvest receipt renders an escaped paper card and durable reopen retains exact run', async () => {
  const dom = new JSDOM('<main id="host"></main>');
  global.document = dom.window.document;
  global.window = dom.window;
  const record = { task_id:'saved-run',direction:'same direction',lang:'zh',status:'interrupted',created_at:Date.now(),updated_at:Date.now(),papers:[{arxivId:'2401.00001',title:'<img src=x onerror=alert(1)>',status:'parsed',textLength:123}],result:{survey_md:'this run only'} };
  const registry = {_researchModeActive:()=>true,_paintResearch:()=>{},Api:{research:{record:async()=>({record})}}};
  const ports = {'../../../feature-registry':{featureRegistry:registry},'../../../i18n':{_i18nLang:'zh'}};
  const runtime = load('controllers/runtime.ts', ports);
  const stream = runtime.newResearchStream('live');
  runtime.researchApplyEvent(stream,{type:'paper_done',result:record.papers[0]});
  runtime.researchApplyEvent(stream,{type:'paper_done',result:record.papers[0]});
  assert.equal(stream.papers.length,1);
  const cards = load('components/live-library.ts',ports);
  const host = document.getElementById('host');
  host.innerHTML=cards.paperCards(stream);
  assert.equal(host.querySelectorAll('.rs-paper-card').length,1);
  assert.equal(host.querySelector('img'),null);
  assert.ok(host.textContent.includes('<img src=x'));
  assert.ok(await runtime.openResearchRecord('saved-run'));
  assert.equal(registry._researchStream.status,'interrupted');
  assert.equal(registry._researchStream.papers[0].arxivId,'2401.00001');
  assert.equal(registry._researchStream.surveyMd,'this run only');
  runtime.destroyResearchRuntime(); dom.window.close();
});

// ── Landing stage (showResearchLanding) ──────────────────────────────
// The foundry landing is the entry point to auto-research: hero, composer,
// five-step journey, template gallery and one-click resumable records. These
// pins drive the REAL view.ts through jsdom so a markup/behaviour regression
// fails here, not in a user session.
function landingPorts(registry, runtime) {
  return {
    '../../../feature-registry': { featureRegistry: registry },
    '../../../i18n': { _i18nLang: 'en' },
    '../../../html-safety': { escapeHtml: (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;') },
    './live-library': { paperCards: () => '', bindPaperCards: () => {} },
    './stable-dom': { patchResearchDom: () => {} },
    '../../paper/research-cycle': {
      researchNumber: (v) => { const n = Number(v); return Number.isFinite(n) ? n : null; },
      researchText: (v) => (v == null ? '' : String(v)),
    },
    '../controllers/runtime': runtime,
    '../controllers/workspace': { loadResearchWorkspace: () => {}, researchWorkspaceHtml: () => '' },
  };
}

function landingDom() {
  const dom = new JSDOM('<div id="researchViewer"></div><div id="researchSidebarCurrent"></div>');
  global.document = dom.window.document;
  global.window = dom.window;
  global.Event = dom.window.Event;
  dom.window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  return dom;
}

test('landing renders hero, journey, template cards and one-click resumable records', async () => {
  const dom = landingDom();
  const opened = [];
  const registry = {
    t: (k) => k,
    _researchStream: null,
    Api: { research: {
      records: async () => ({ ok: true, items: [
        { task_id: 'task-1', direction: 'KV-cache <img src=x> compression', status: 'done', updated_at: 1700000000 },
        { task_id: 'task-2', direction: 'tool-use error recovery', status: 'running', updated_at: 1700001000 },
      ] }),
      list: async () => ({ ok: true, items: [] }),
    } },
  };
  const view = load('components/view.ts', landingPorts(registry, {
    openResearchRecord: async (id) => { opened.push(id); return true; },
    resumeSavedResearch: async () => {},
    researchIsRunning: () => false,
  }));
  view.showResearchLanding();

  // Entry contract (the browser surface test drives the same two hooks).
  const input = document.getElementById('paperResearchInput');
  assert.ok(input, 'direction textarea missing');
  assert.equal(input.getAttribute('maxlength'), '2000');
  assert.ok(document.querySelector('[data-tofu-action*="_submitResearchDirection"]'),
    'start button lost its submit action');

  // Hero + five-step journey in pipeline order.
  assert.equal(document.querySelector('.rs-hero-title').textContent, 'paper.research.heroTitle');
  assert.ok(document.querySelector('.rs-hero-kicker').textContent
    .includes('paper.research.workbenchLabel'));
  const steps = [...document.querySelectorAll('.rs-journey-step')];
  assert.equal(steps.length, 5, 'journey must mirror the five pipeline phases');
  assert.equal(steps[0].querySelector('strong').textContent, 'paper.research.phase.harvest');
  assert.equal(steps[4].querySelector('strong').textContent, 'paper.research.phase.publish');
  assert.equal(steps[0].querySelector('.rs-journey-node').textContent, '01');
  assert.equal(steps[1].querySelector('span:last-child').textContent, 'paper.research.step.survey');

  // Template gallery fills the composer through the real action and the
  // autosize/counter wiring observes the injected value.
  const cards = [...document.querySelectorAll('.rs-template-card')];
  assert.equal(cards.length, 3);
  assert.ok(cards[0].getAttribute('data-tofu-action').includes('_fillResearchTemplate(0)'));
  view.fillResearchTemplate(0);
  assert.equal(input.value, 'paper.research.template1');
  assert.equal(document.getElementById('researchCharCount').textContent,
    'paper.research.template1'.length + ' / 2000');

  // Recent records render asynchronously and resume on click — escaped markup.
  await new Promise((r) => setTimeout(r, 10));
  const recent = document.getElementById('researchLandingRecent');
  assert.equal(recent.hidden, false, 'recent section must surface when records exist');
  const rows = [...recent.querySelectorAll('.rs-recent-item')];
  assert.equal(rows.length, 2);
  assert.ok(rows[0].textContent.includes('KV-cache'));
  assert.equal(recent.querySelector('img'), null, 'direction HTML must stay escaped');
  rows[0].click();
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(opened, ['task-1']);

  // No live stream: the sidebar current card stays empty.
  assert.equal(document.getElementById('researchSidebarCurrent').innerHTML, '');
  dom.window.close();
});

test('landing recent falls back to the durable direction archive', async () => {
  const dom = landingDom();
  const opened = [];
  const restored = [];
  const registry = {
    t: (k) => k,
    _researchStream: null,
    Api: { research: {
      records: async () => ({ ok: true, items: [] }),
      list: async () => ({ ok: true, items: [
        { direction: 'retrieval conflict evidence selection', lang: 'en',
          accepted: 2, rejected: 5, created_at: 1700000000 },
      ] }),
    } },
    _restoreResearchFromStore: async (...args) => { restored.push(args); },
  };
  const view = load('components/view.ts', landingPorts(registry, {
    openResearchRecord: async (id) => { opened.push(id); return true; },
    resumeSavedResearch: async () => {},
    researchIsRunning: () => false,
  }));
  view.showResearchLanding();
  await new Promise((r) => setTimeout(r, 10));
  const rows = [...document.querySelectorAll('.rs-recent-item')];
  assert.equal(rows.length, 1, 'archive fallback must render when the registry is empty');
  assert.ok(rows[0].textContent.includes('retrieval conflict evidence selection'));
  rows[0].click();
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(opened, [], 'archive rows must not route through task-registry resume');
  assert.deepEqual(restored, [['retrieval conflict evidence selection', 'en']]);
  dom.window.close();
});

test('landing keeps the recent section hidden when the archive is empty', async () => {
  const dom = landingDom();
  const registry = {
    t: (k) => k,
    _researchStream: null,
    Api: { research: {
      records: async () => ({ ok: true, items: [] }),
      list: async () => ({ ok: true, items: [] }),
    } },
  };
  const view = load('components/view.ts', landingPorts(registry, {
    openResearchRecord: async () => true,
    resumeSavedResearch: async () => {},
    researchIsRunning: () => false,
  }));
  view.showResearchLanding();
  await new Promise((r) => setTimeout(r, 10));
  const recent = document.getElementById('researchLandingRecent');
  assert.equal(recent.hidden, true, 'empty archive must not render a nagging empty block');
  assert.equal(recent.innerHTML, '');
  dom.window.close();
});
