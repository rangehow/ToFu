"""The composer submits exactly the thinking depth its slider paints.

Conversations mwk9vstrxs3ul0, mwk9ys7et54nzu and mwka2vpcl12ma4 (2026-11-17)
showed Max or the declared default on the slider while every round compiled
``requested: default`` and Claude received no effort control. A provisional
default model skipped committing the depth to the conversation a send reads,
and an unset or ``default`` depth was painted on a rung but submitted as the
follow-the-model token. This drives the real retained composer path (slider,
commit, config inputs, submission), then runs the real conversation config
resolver and reasoning compiler on what it sent.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.llm.reasoning import reasoning_depth_restoration
from tests._jsdom import require_node_modules
from tests._runtime_sections import _require_frontend_bundler, runtime_section

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _route(creator: str, model: str, depths: list[str], default: str | None,
           can_disable: bool) -> dict:
    return {'creator_id': creator, 'model_id': model, 'provider_id': '',
            'offering_id': '', 'support': 'supported', 'availability': 'available',
            'can_disable': can_disable, 'default_depth': default, 'depths': depths,
            'restoration': reasoning_depth_restoration(depths)}


# Shapes of the live owner rows for these models (GET /api/v1/model-routing).
ROWS = [
    _route('anthropic', 'claude-opus-5.5',
           ['default', 'low', 'medium', 'high', 'xhigh', 'max'], 'medium', False),
    _route('moonshot', 'kimi-k3', ['default', 'low', 'high', 'max'], 'max', False),
    _route('meituan', 'LongCat-2.5', ['default', 'off', 'enabled'], None, True),
]

_RETAINED = {
    'main.js': ('_reasoningOptionsFor', '_composerReasoningOptions', '_isThinkingCapable',
                '_composerDepthIntent', '_routeThinkingDepth', 'selectThinkingDepth',
                '_updateDepthButtons'),
    'main/main_conv_lifecycle.js': ('_buildToolbarOverrides', '_buildConvSnapshot',
                                    '_buildConvConfigInputs', '_buildConvConfig',
                                    '_buildConvSettingsInputs', '_buildConvSubmission'),
}

_HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
function load(path, deps = {}) {
  const module = {exports: {}};
  const output = ts.transpileModule(fs.readFileSync(path, 'utf8'), {compilerOptions:
    {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
  new Function('module', 'exports', 'require', output)(module, module.exports, id => deps[id]);
  return module.exports;
}
const {createThinkingDepthSlider, effectiveThinkingDepth, projectReasoningControl, THINKING_DEPTH_LABELS} =
  load('frontend/src/features/thinking-depth-slider.ts');
const {captureComposerModel, commitComposerModel} =
  load('frontend/src/conversation/application/conversation-model.ts');
const project = load('frontend/src/conversation/application/conversation-project.ts');
const {captureConversationProject} = project;
const {createConversationSettingsResolution, buildConversationSettingsSnapshot} = load(
  'frontend/src/conversation/application/conversation-settings-resolution.ts',
  {'./conversation-project': project});
const en = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/en.json', 'utf8'));
const dom = new JSDOM(fs.readFileSync('index.html', 'utf8'));
var document = dom.window.document;
var runtimeScope = {}, localStorage = {setItem() {}};
var config = {}, serverModel = 'claude-opus-5.5', activeConvId = null, conversations = [];
var _reasoningControls, _registeredModels = [];
var searchMode = 'multi', fetchEnabled = true, codeExecEnabled = false, memoryEnabled = true;
var schedulerEnabled = false, browserEnabled = false, desktopEnabled = false;
var imageGenEnabled = false, imageGenMode = false, humanGuidanceEnabled = false;
var _igSelectedModel = '', _igSelectedProviderId = '', _igSelectedCount = 1;
var _igSelectedAspect = '1:1', _igSelectedResolution = '1K';
var autopilotEnabled = false, activeFlow = '', chatMode = 'chat', planMode = false;
var autoTranslate = false, autoApplyWrites = true, _i18nLang = 'en';
var _DEPTH_ICONS = {}, _DEPTH_ICON_FALLBACK = '';
function t(key) { return en[key] || key; }
function _modelShortName(id) { return id; }
function getActiveConv() { return conversations.find(conv => conv.id === activeConvId) || null; }
function captureActiveConversationSettings() {}
function _configForPersist() { return {}; }
function debugLog() {}
function _getConvProjectPath(conv) { return (conv && conv.projectPath) || ''; }
const requests = [];
const _conversationSettingsResolution = createConversationSettingsResolution({
  resolveConfig: async inputs => {
    requests.push(JSON.parse(JSON.stringify(inputs)));
    return {ok: true, settings: {}};
  },
  resolveSettings: async () => ({ok: true}),
  patchResolvedSettings: async () => ({ok: true, status: 200}),
  patchSettings: async () => ({ok: true, status: 200}),
});
const _thinkingDepthSlider = createThinkingDepthSlider({
  document, translate: t, commit: depth => selectThinkingDepth(depth)});
RETAINED_FUNCTIONS
const ROWS = ROWS_JSON;
const input = () => document.getElementById('thinkingDepthRange');
const label = () => input().closest('[data-thinking-depth-slider]')
  .querySelector('[data-depth-value]').textContent;
const marked = name => `${name} · ${en['mobile.default']}`;
// What _applyModelUI paints after a restore, a new chat or a model switch.
const paint = () => _updateDepthButtons(_composerDepthIntent(config.thinkingDepth));
function gesture(index) {
  input().value = String(index);
  input().dispatchEvent(new dom.window.Event('input', {bubbles: true}));
  input().dispatchEvent(new dom.window.Event('change', {bubbles: true}));
}
// main_send_pipeline: capture at send, commit to the conversation, submit.
async function send(conv) {
  const modelAtSend = captureComposerModel(config);
  commitComposerModel(conv, modelAtSend);
  await _buildConvSubmission(conv, captureConversationProject(conv), {});
  return requests.pop();
}
function open(conv, model, overrides = {}) {
  conversations = [conv]; activeConvId = conv.id;
  config = {model, _modelIsProvisional: !conv.model, modelRef: conv.modelRef || null,
    preferredProviderId: '', thinkingDepth: conv.thinkingDepth || null,
    defaultThinkingDepth: 'max', ...overrides};
  paint();
}
const results = {};
(async () => {
  _reasoningControls = ROWS;

  // A new chat on the provisional default model; the user drags to Max.
  config = {model: 'claude-opus-5.5', _modelIsProvisional: true, modelRef: null,
    preferredProviderId: '', thinkingDepth: 'medium', defaultThinkingDepth: 'medium'};
  activeConvId = null; conversations = [];
  paint();
  assert.equal(label(), marked('Med'));
  gesture(4);
  assert.equal(config.thinkingDepth, 'max');
  assert.equal(label(), 'Max');
  const fresh = {id: 'new-chat', title: 'New Chat', _localOnly: true};
  conversations = [fresh]; activeConvId = fresh.id;
  results.new_chat_max = await send(fresh);
  assert.equal(results.new_chat_max.overrides.thinkingDepth, 'max');
  assert.equal(fresh.thinkingDepth, 'max');

  // A conversation that never saved a depth paints the declared default rung
  // (not the user's global default) and submits that rung.
  const opus = {id: 'unset', model: 'claude-opus-5.5',
    modelRef: {creator_id: 'anthropic', model_id: 'claude-opus-5.5'}};
  open(opus, 'claude-opus-5.5');
  assert.equal(input().value, '1');
  assert.equal(label(), marked('Med'));
  results.unset_default_rung = await send(opus);
  assert.equal(results.unset_default_rung.overrides.thinkingDepth, 'medium');

  // A depth saved by the retired Default button follows the declared rung.
  const kimi = {id: 'kimi', model: 'kimi-k3', thinkingDepth: 'default',
    modelRef: {creator_id: 'moonshot', model_id: 'kimi-k3'}};
  open(kimi, 'kimi-k3');
  assert.equal(label(), marked('Max'));
  assert.equal((await send(kimi)).overrides.thinkingDepth, 'max');

  // An unavailable saved depth submits the rung its thumb parks on; the
  // persisted settings keep the saved choice for a wider route.
  kimi.thinkingDepth = 'xhigh';
  open(kimi, 'kimi-k3');
  assert.equal(input().value, '1');
  assert.match(input().getAttribute('aria-valuetext'), new RegExp(en['mobile.depthUnavailable']));
  const parked = await send(kimi);
  assert.equal(parked.overrides.thinkingDepth, 'high');
  assert.equal(parked.settings_conv_settings.thinkingDepth, 'xhigh');

  // Without a declared default rung the slider says Default and means it.
  const longcat = {id: 'longcat', model: 'LongCat-2.5',
    modelRef: {creator_id: 'meituan', model_id: 'LongCat-2.5'}};
  open(longcat, 'LongCat-2.5');
  assert.equal(input().getAttribute('aria-valuetext'), en['mobile.default']);
  assert.equal((await send(longcat)).overrides.thinkingDepth, 'default');

  // A background conversation (pending resume) resolves its own route.
  const background = {id: 'background', model: 'kimi-k3',
    modelRef: {creator_id: 'moonshot', model_id: 'kimi-k3'}};
  results.background_kimi = _buildConvConfigInputs(background);
  assert.equal(results.background_kimi.is_active, false);
  assert.equal(results.background_kimi.conv_settings.thinkingDepth, 'max');

  // Before routing controls arrive the legacy ladder paints and submits alike.
  _reasoningControls = undefined;
  const boot = {id: 'boot', model: 'claude-opus-5.5'};
  open(boot, 'claude-opus-5.5', {defaultThinkingDepth: 'high'});
  assert.equal(input().value, '3');
  assert.equal((await send(boot)).overrides.thinkingDepth, 'high');

  console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''


def _match(src: str, open_pos: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(open_pos, len(src)):
        if src[index] == opening:
            depth += 1
        elif src[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError(f'unbalanced {opening}{closing}')


def _extract_fn(src: str, name: str) -> str:
    match = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\(', src)
    assert match, f'{name} not found'
    # Default parameters may hold braces; the body starts after the list.
    params_end = _match(src, match.end() - 1, '(', ')')
    return src[match.start():_match(src, src.index('{', params_end), '{', '}')]


@pytest.fixture(scope='module')
def composer_requests() -> dict:
    _require_frontend_bundler()
    require_node_modules('jsdom', 'typescript')
    functions = '\n'.join(
        _extract_fn(runtime_section(section, scope_prelude=False), name)
        for section, names in _RETAINED.items() for name in names)
    script = (_HARNESS.replace('RETAINED_FUNCTIONS', functions)
              .replace('ROWS_JSON', json.dumps(ROWS)))
    result = subprocess.run([shutil.which('node'), '-e', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _claude_profile() -> dict:
    data = json.loads((ROOT / 'lib/model_info/model_inference_defaults.json').read_text())
    return next(group['inference_profile'] for group in data['groups']
                if 'claude-opus-5' in group['model_ids'])


def _compiled(request: dict) -> dict:
    from lib.conv_config import resolve_conv_config
    from lib.llm.reasoning import capture_intent, compile_reasoning
    from lib.tasks_pkg.model_config import _resolve_model_config

    config = resolve_conv_config(request['conv_settings'], request['overrides'],
                                 request['server_defaults'], is_active=request['is_active'])
    resolved = _resolve_model_config(dict(config), 'composer-depth-submission')
    body = {'model': resolved['model'], 'messages': [], '_reasoning_intent': capture_intent(
        thinking_enabled=resolved['thinking_enabled'], thinking_depth=resolved['thinking_depth'],
        preset=resolved['preset'], temperature=resolved['temperature'])}
    compile_reasoning(body, resolved['model'], inference_profile=_claude_profile())
    return body


def test_composer_submits_the_rung_its_slider_paints(composer_requests):
    assert set(composer_requests) == {'new_chat_max', 'unset_default_rung', 'background_kimi'}


@pytest.mark.parametrize('scenario, effort', [
    ('new_chat_max', 'max'),
    ('unset_default_rung', 'medium'),
    ('background_kimi', 'max'),
])
def test_backend_sends_the_painted_effort_to_the_provider(composer_requests, scenario, effort):
    body = _compiled(composer_requests[scenario])
    resolution = body['_reasoning_resolution']
    assert (resolution['actualMode'], resolution['actualValue']) == ('exact', effort)
    assert body['effort'] == effort
    assert body['thinking']['type'] == 'adaptive'
