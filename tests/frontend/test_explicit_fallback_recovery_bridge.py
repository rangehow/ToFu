"""Explicit 429 fallback recovery uses the real model picker without persisting a draft."""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import native_module_path, runtime_section

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _slice_fn(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(signature)


def _run() -> dict:
    require_node_modules('jsdom')
    toolbar = runtime_section('main/main_toolbar_ui.js')
    store = runtime_section('main/conversation_turn_store.js')
    phase = Path(native_module_path(
        '.native/explicit-fallback-live-phase.js',
        ROOT / 'frontend/src/conversation/presentation/live-phase.ts',
    )).read_text()
    functions = '\n'.join([
        _slice_fn(toolbar, 'function _registeredFallbackRoute('),
        _slice_fn(toolbar, 'function selectModelRoute('),
        _slice_fn(toolbar, 'function _captureFallbackSelectionEffects('),
        _slice_fn(toolbar, 'function _restoreFallbackSelectionEffects('),
        _slice_fn(store, '  async prepareTarget(').replace(
            '  async prepareTarget(offer, state, signal) {',
            'async function _prepareFallbackTarget(offer, state, signal) {', 1,
        ),
    ])
    script = phase + textwrap.dedent(r'''
        const store = new Map();
        global.localStorage = {
          getItem(key) { return store.has(key) ? store.get(key) : null; },
          setItem(key, value) { store.set(key, String(value)); },
          removeItem(key) { store.delete(key); },
        };
        global.runtimeScope = {
          isChatModel: row => row.capabilities.includes('text'),
          recentModels() { return JSON.parse(store.get('tofu_recent_models') || '[]'); },
          pushRecentModel(id) {
            const next = this.recentModels().filter(item => item !== id);
            next.unshift(id); store.set('tofu_recent_models', JSON.stringify(next.slice(0, 5)));
          },
        };
        const conv = {id:'conv', model:'primary', modelRef:{creator_id:'creator', model_id:'primary'},
          preferredProviderId:'provider-a', thinkingDepth:'low'};
        global.conversations = [conv];
        global.getActiveConv = () => conv;
        global.config = {model:'primary', modelRef:{creator_id:'creator', model_id:'primary'},
          preferredProviderId:'provider-a', routing:{preferred_provider_id:'provider-a'},
          thinkingDepth:'low', _modelIsProvisional:false};
        global.serverModel = 'primary';
        global._registeredModelsLoaded = true;
        global._registeredModels = [
          {model_id:'primary', provider_id:'provider-a', creator_id:'creator', routing_v2:true,
            capabilities:['text']},
          {model_id:'fallback', provider_id:'provider-b', creator_id:'creator', routing_v2:true,
            provider_name:'Provider B', capabilities:['text']},
        ];
        global._configuredFallbackModelId = 'fallback';
        global.captureComposerModel = value => ({model:value.model, modelRef:value.modelRef,
          preferredProviderId:value.preferredProviderId, thinkingDepth:value.thinkingDepth});
        global.commitComposerModel = (conversation, selection) => Object.assign(conversation, selection);
        global.captureActiveConversationSettings = () => Object.assign(conv,
          global.captureComposerModel(config));
        global._applyModelUI = model => { config.model = model || serverModel;
          config._modelIsProvisional = !model; };
        global._refreshModelPickerSelection = () => {};
        global._configForPersist = () => config;
        global.debugLog = () => {};
        let releaseResolve;
        let resolvedConfig;
        const resolution = new Promise(resolve => { releaseResolve = resolve; });
        global._buildConvConfig = async () => {
          await resolution;
          // A live conversation settings response may restore the old model
          // while the fallback route read is in flight.
          const current = JSON.parse(JSON.stringify(conv));
          _applyModelUI(current.model || null);
          config.modelRef = current.modelRef;
          config.preferredProviderId = current.preferredProviderId;
          config.routing = {preferred_provider_id: current.preferredProviderId};
          return resolvedConfig;
        };
        __FUNCTIONS__
        (async () => {
          store.set('claude_client_config', JSON.stringify(config));
          store.set('tofu_recent_models', JSON.stringify(['primary', 'older']));
          const offer = {conversationId:'conv', turnId:'answer', attemptId:'old',
            projectionRevision:4, currentModel:'primary', model:'fallback'};
          const state = {livePhase:{phase:'retrying', model:'primary', statusCode:429,
            dispatchMode:'strict_model', detailKey:'stream.phase.retryRateLimited', detailArgs:{}},
            commandPending:{}, turnsById:{answer:{conversationId:'conv', turnId:'answer',
              laneId:'main', actor:'assistant', kind:'reply', status:'running', currentAttemptId:'old',
              projectionRevision:4, projection:{model:'primary'}}}};
          const pending = _prepareFallbackTarget(offer, state, new AbortController().signal);
          await Promise.resolve();
          resolvedConfig = {model:'fallback', modelRef:config.modelRef,
            preferredProviderId:config.preferredProviderId, routing:config.routing,
            chatMode:'chat'};
          releaseResolve();
          const prepared = await pending;
          console.log(JSON.stringify({
            target:_registeredFallbackRoute('conv'), prepared,
            restoredConfig:config, restoredConv:conv,
            recent:runtimeScope.recentModels(),
            stored:JSON.parse(store.get('claude_client_config')),
          }));
        })().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
    ''').replace('__FUNCTIONS__', functions)
    result = subprocess.run([shutil.which('node'), '-e', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_recovery_uses_route_picker_then_restores_the_original_composer_choice():
    result = _run()
    assert result['target'] == {
        'model': 'fallback', 'providerId': 'provider-b', 'providerName': 'Provider B',
    }
    assert result['prepared']['config'] == {
        'model': 'fallback',
        'modelRef': {'creator_id': 'creator', 'model_id': 'fallback'},
        'preferredProviderId': 'provider-b',
        'routing': {'preferred_provider_id': 'provider-b'},
        'chatMode': 'chat',
    }
    for restored in (result['restoredConfig'], result['restoredConv']):
        assert restored['model'] == 'primary'
        assert restored['modelRef']['model_id'] == 'primary'
        assert restored['preferredProviderId'] == 'provider-a'
    assert result['recent'] == ['primary', 'older']
    assert result['stored']['model'] == 'primary'
    assert result['stored']['preferredProviderId'] == 'provider-a'
