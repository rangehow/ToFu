import { describe, expect, it } from 'vitest';
import type { TransientTurnPresentation } from '../domain/transient-turn';
import { liveAttemptPresentation } from './view-model/live-turns';
import {
  configuredFallbackPhaseOffer,
  liveStatusPhase,
  presentConversationRateLimit,
  presentLivePhaseLabel,
} from './live-phase';
const text = (key: string, fallback: string, args?: Readonly<Record<string, string | number>>) =>
  (key ? `localized:${key}` : fallback) + (args ? JSON.stringify(args) : '');
function root(currentPhase: TransientTurnPresentation): TransientTurnPresentation {
  return { kind: 'attempt', phase: 'waiting', label: '', detail: '',
    goalProgress: { iteration: '4', role: 'worker', rootSummary: true, workers: [], currentPhase } };
}
describe('explicit 429 fallback offer', () => {
  const target = { model: 'fallback', providerId: 'provider-b', providerName: 'Provider B' };
  const turn = {
    conversationId: 'conv', turnId: 'answer', currentAttemptId: 'attempt',
    laneId: 'main', actor: 'assistant', kind: 'reply', status: 'running', projectionRevision: 9,
    projection: { origin: { initiator: 'human' } },
  };
  const direct = liveAttemptPresentation({
    phase: 'retrying', statusCode: 429, attempt: 3, model: 'primary',
    providerId: 'provider-a', dispatchMode: 'strict_model',
    detailKey: 'stream.phase.retryRateLimited', detailArgs: {
      model: 'primary', attempt: 3, reasonKey: 'stream.retryReason.rateLimited',
    },
  }, 'running');
  it('offers a different configured route for direct 429 and typed cooldown waits', () => {
    expect(configuredFallbackPhaseOffer(direct, turn, target)).toEqual({
      conversationId: 'conv', turnId: 'answer', attemptId: 'attempt', projectionRevision: 9,
      currentModel: 'primary', currentProviderId: 'provider-a', ...target,
    });
    const cooldown = liveAttemptPresentation({
      ...direct, statusCode: 0,
      detailKey: 'stream.phase.retryCooldownWait',
      detailArgs: { reasonKey: 'stream.retryReason.waitingForModel', model: 'primary' },
    }, 'running');
    expect(configuredFallbackPhaseOffer(cooldown, turn, target)?.attemptId).toBe('attempt');
  });
  it.each([
    ['missing fallback', direct, turn, null, false],
    ['same model', direct, turn, { model: 'primary' }, false],
    ['disabled', direct, turn, target, true],
    ['pool rescue', { ...direct, dispatchMode: 'pool_rescue' }, turn, target, false],
    ['missing dispatch mode', { ...direct, dispatchMode: undefined }, turn, target, false],
    ['non-main', direct, { ...turn, laneId: 'branch' }, target, false],
    ['Goal orchestration', direct, { ...turn, projection: { orchestration: { goalRunId: 'goal' } } }, target, false],
    ['delegated origin', direct, { ...turn, projection: { origin: { initiator: 'swarm' } } }, target, false],
    ['transient execution', direct, { ...turn, transientPresentation: {} }, target, false],
    ['generic retry', { ...direct, detailKey: 'stream.phase.retryGeneric', detailArgs: {} }, turn, target, false],
    ['network wait', { ...direct, detailKey: 'stream.phase.retryCooldownWait', detailArgs: { reasonKey: 'stream.retryReason.networkRecovery' } }, turn, target, false],
    ['quota wait', { ...direct, detailKey: 'stream.phase.waitingQuotaReset', detailArgs: {} }, turn, target, false],
    ['wrong phase', { ...direct, phase: 'waiting_model' }, turn, target, false],
    ['no current model', { ...direct, model: undefined }, turn, target, false],
    ['no exact attempt', direct, { ...turn, currentAttemptId: null }, target, false],
    ['settled turn', direct, { ...turn, status: 'interrupted' }, target, false],
  ])('hides for %s', (_name, phase, current, candidate, disabled) => {
    expect(configuredFallbackPhaseOffer(phase, current, candidate, disabled)).toBeNull();
  });
});

describe('shared live phase presentation', () => {
  it('shows the persistence blockage without stale model-routing progress', () => {
    const value = liveAttemptPresentation({
      phase: 'storage_wedged',
      modelRoute: { selectedModel: 'auto', resolvedModel: 'model-a',
        role: 'worker', tier: 'standard', kind: 'role_tier' },
    }, 'running');
    expect(presentLivePhaseLabel(value, text)).toBe('localized:stream.phase.storageWedged');
  });
  it.each([
    { phase: 'waiting_model' }, { phase: 'llm_thinking' }, { phase: 'thinking_active' }, { phase: 'responding' },
    { phase: 'llm_thinking', detailKey: 'stream.phase.preparingToolCall', detailArgs: { tool: 'write_file', elapsed: 3 } },
    { phase: 'retrying' }, { phase: 'tool_exec', tools: ['read_file', 'inspect_runtime'] },
    { phase: 'worker_starting', detailKey: 'stream.phase.workerStarting' },
    { phase: 'retrying', detailKey: 'stream.phase.retryRateLimited', detailArgs: { model: 'm' } },
    { phase: 'llm_thinking', detailKey: 'stream.phase.analyzingRound', detailArgs: { round: 4 }, detail: 'English' },
    { phase: 'third_party', detail: 'Plugin progress' },
  ])('uses identical normal and Goal root copy for $phase', raw => {
    const value = liveAttemptPresentation(raw, 'running');
    expect(presentLivePhaseLabel(liveStatusPhase(root(value)), text)).toBe(presentLivePhaseLabel(value, text));
  });
  it('keeps credit exhaustion out of the transient rate-limit badge', () => {
    expect(presentConversationRateLimit({
      phase: 'retrying', detailKey: 'stream.phase.retryReason',
      detailArgs: { reasonKey: 'stream.retryReason.keyBalanceExhausted', model: 'm' },
    })).toBeNull();
    expect(presentConversationRateLimit({
      phase: 'retrying', detailKey: 'stream.phase.retryRateLimited',
      detailArgs: { model: 'm' }, attempt: 1,
    })).toEqual({ model: 'm', attempt: 1 });
  });
  it('names client-derived content streams with their own localized copy', () => {
    expect(presentLivePhaseLabel(liveAttemptPresentation({ phase: 'thinking_active' }, 'running'), text))
      .toBe('localized:stream.thinking.active');
    expect(presentLivePhaseLabel(liveAttemptPresentation({ phase: 'responding' }, 'running'), text))
      .toBe('localized:stream.phase.generatingResponse');
  });
  it('keeps tool identity and routing evidence in Goal root context', () => {
    const current = liveAttemptPresentation({ phase: 'tool_exec', tools: ['canonical_tool'], modelRoute: {
      selectedModel: 'selected', resolvedModel: 'resolved', role: 'worker', tier: 'heavy', kind: 'role_tier',
    } }, 'running');
    const label = presentLivePhaseLabel(liveStatusPhase(root(current)), text);
    expect(label).toContain('canonical_tool');
    expect(label).toContain('stream.phase.modelRouted');
    expect(label).toContain('resolved');
  });
  it('never replaces a root storage warning with a joined worker phase', () => {
    const value = { ...root(liveAttemptPresentation({ phase: 'waiting_model' }, 'running')), phase: 'storage_wedged' };
    expect(liveStatusPhase(value)).toBe(value);
    expect(presentLivePhaseLabel(liveStatusPhase(value), text)).toBe('localized:stream.phase.storageWedged');
  });
  it('keeps executor preparing/queued distinct and shows the queue on the attempt it starts', () => {
    expect(liveAttemptPresentation({}, 'pending').phase).toBe('executor_preparing');
    expect(liveAttemptPresentation({ phase: 'executor_queued' }, 'pending').phase).toBe('executor_queued');
    // The queue frame is the attempt's first recorded event, which marks it running.
    expect(liveAttemptPresentation({ phase: 'executor_queued' }, 'running').phase).toBe('executor_queued');
    expect(liveAttemptPresentation({}, 'running').phase).toBe('waiting');
  });
});
