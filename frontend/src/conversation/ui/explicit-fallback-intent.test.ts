// @vitest-environment jsdom
/** The explicit 429 action reaches the exact mounted Turn, never ambient work. */
import { describe, expect, it, vi } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createClassicConversationRenderers } from './classic-conversation-renderers';
import { createConversationSurface } from './conversation-surface';
import type { ConversationIntent } from './surface-contracts';

describe('explicit fallback intent', () => {
  it('emits use-configured-fallback for the exact running main-lane Turn', () => {
    const state = createTurnState('conv');
    state.turnsById.answer = {
      turnId: 'answer', presentationId: 'answer', conversationId: 'conv', laneId: 'main',
      ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status: 'running',
      currentAttemptId: 'old', projectionRevision: 4,
      projection: { content: '', toolRounds: [], origin: { blockId: 'origin', initiator: 'human' } },
      settlement: {}, createdAt: 1, updatedAt: 1,
    };
    state.attemptsById.old = { attemptId: 'old', turnId: 'answer', status: 'running' };
    state.laneOrder.main = ['answer'];
    state.livePhase = {
      phase: 'retrying', model: 'primary', providerId: 'provider-a',
      statusCode: 429, dispatchMode: 'strict_model',
      detailKey: 'stream.phase.retryRateLimited', detailArgs: { model: 'primary' },
    };
    const host = document.createElement('main'); document.body.append(host);
    const intents: ConversationIntent[] = [];
    const surface = createConversationSurface(host, {
      ...createClassicConversationRenderers({
        renderSafeMarkdownHtml: value => value,
        configuredFallbackTarget: () => ({ model: 'fallback', providerId: 'provider-b' }),
      }),
      onIntent: vi.fn(intent => { intents.push(intent); }),
    });
    try {
      surface.render(selectConversationViewModel(state));
      const action = host.querySelector<HTMLButtonElement>(
        '[data-conversation-action="use-configured-fallback"]',
      )!;
      action.click();
      expect(intents).toEqual([{
        type: 'use-configured-fallback', conversationId: 'conv', turnId: 'answer',
        blockId: 'live-status', laneId: 'main',
      }]);
    } finally {
      surface.dispose(); host.remove();
    }
  });
});
