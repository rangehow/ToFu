// @vitest-environment jsdom
/** A conflict names the Turn holding the lane; the store follows its stream. */
import { expect, it, vi } from 'vitest';
import type { TurnRecord } from '../api/conversation-sync.generated';
import { createTurnCommands } from './turn-command-execution';
import { createTurnStore } from './turn-state';

function setup(revision = 1, currentAttemptId: string | null = null) {
  const store = createTurnStore('conv');
  store.dispatch({ type: 'snapshot', snapshot: { conversationRevision: 1, turns: [{
    turnId: 'turn', presentationId: 'turn', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', runId: '', status: currentAttemptId ? 'running' : 'completed',
    currentAttemptId, projectionRevision: revision, createdAt: 1, updatedAt: 1, settlement: {},
    projection: { content: 'local' } } as TurnRecord], attempts: [], queueItems: [] } });
  const createAttempt = vi.fn();
  const followConflict = vi.fn();
  const commands = createTurnCommands({ api: { createAttempt } as never }, {
    ensureRuntimeStore: () => store, readRuntimeStore: () => store, isCurrentStore: () => true,
    resolveConversation: conversation => conversation, captureConversation: conversation => () => conversation,
    hydrateConversation: vi.fn(), hydrateUnbound: vi.fn(), loadCompleteTurn: vi.fn(),
    bindConversation: vi.fn(), connectAttempt: vi.fn(), followConflict,
  });
  return { store, commands, createAttempt, followConflict };
}

const conflict = (fields: Record<string, unknown> = {}) => ({ status: 409, body: {
  error: { kind: 'lane_busy' },
  conflictTurn: { conversationId: 'conv', turnId: 'turn', currentAttemptId: 'live-attempt', projectionRevision: 4, ...fields },
} });

it('follows the conflicting attempt through the sync stream without adopting a record', async () => {
  const f = setup();
  const error = conflict();
  f.createAttempt.mockRejectedValue(error);
  await expect(f.commands.runOperation(f.store, 'turn', 'retry')).rejects.toBe(error);
  expect(f.followConflict).toHaveBeenCalledExactlyOnceWith(f.store, 'live-attempt');
  expect(f.store.getState().turnsById.turn?.projectionRevision).toBe(1);
  expect(f.store.getState().commandPending.turn).toBeUndefined();
});

it('needs no catch-up for a conflict the store already holds', async () => {
  const f = setup(4, 'live-attempt');
  f.createAttempt.mockRejectedValue(conflict());
  await expect(f.commands.runOperation(f.store, 'turn', 'retry')).rejects.toBeDefined();
  expect(f.followConflict).not.toHaveBeenCalled();
});

it.each(['conversationId', 'turnId'] as const)('ignores a conflict naming a foreign %s', async field => {
  const f = setup();
  f.createAttempt.mockRejectedValue(conflict({ [field]: 'foreign' }));
  await expect(f.commands.runOperation(f.store, 'turn', 'retry')).rejects.toBeDefined();
  expect(f.followConflict).not.toHaveBeenCalled();
});
