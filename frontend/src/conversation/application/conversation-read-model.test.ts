import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { editEntityRecord } from '../../core/entity-record';
import { createTurnState, type TurnState, type TurnStateUpdate } from '../domain/turn-store';
import {
  conversationCompactionArchiveIds, latestConversationTurn, orderedConversationTurns,
} from './conversation-read-model';

function turn(turnId: string, projection: Record<string, unknown>, conversationId = 'conv') {
  return {
    turnId, presentationId: turnId, runId: '', conversationId, laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
    projectionRevision: 1, createdAt: 1, updatedAt: 1, settlement: {}, projection,
  } as never;
}

function timeline(...entries: Array<Record<string, unknown>>) {
  return {
    blockId: 'activity-timeline', version: 1,
    entries: entries.map((entry, index) => ({
      id: `row-${index}`, spanId: `row-${index}`, seq: index, occurredAt: index,
      kind: 'status', status: 'succeeded', severity: 'info', count: 1, ...entry,
    })),
  };
}

describe('conversationCompactionArchiveIds', () => {
  it('collects receipts and compaction Turns of this conversation once', () => {
    const state = createTurnState('conv');
    state.turnsById.auto = turn('auto', { activityTimeline: timeline(
      { reasonCode: 'context_compaction', status: 'running', archiveId: 'archive-auto' },
      { kind: 'tool' },
      { reasonCode: 'retrying', severity: 'warning', archiveId: 'not-a-receipt' },
    ) });
    state.turnsById.manual = turn('manual', {
      compaction: { blockId: 'compaction', archiveId: 'archive-manual' },
      activityTimeline: timeline({ reasonCode: 'context_compaction', archiveId: 'archive-auto' }),
    });
    state.turnsById.foreign = turn('foreign', {
      compaction: { blockId: 'compaction', archiveId: 'archive-elsewhere' },
    }, 'other-conversation');

    expect([...conversationCompactionArchiveIds(state)].sort())
      .toEqual(['archive-auto', 'archive-manual']);
    expect(conversationCompactionArchiveIds(null)).toEqual([]);
  });
});

describe('receipt-driven reads of a long transcript', () => {
  const guarded = (count: number, project: (index: number) => Record<string, unknown>) => {
    const guard = { armed: false };
    const turns: Record<string, unknown> = {};
    for (let index = 0; index < count; index += 1) {
      turns[`history-${index}`] = new Proxy(turn(`history-${index}`, project(index)), {
        get(target, key, receiver) {
          if (guard.armed) throw new Error(`read unchanged Turn history-${index}.${String(key)}`);
          return Reflect.get(target, key, receiver);
        },
      });
    }
    return { guard, turns };
  };
  const write = (previousState: TurnState, turns: Record<string, unknown>): [TurnState, TurnStateUpdate] => {
    const draft = editEntityRecord(previousState.turnsById);
    Object.assign(draft.record, turns);
    return [{ ...previousState, turnsById: draft.finish() as TurnState['turnsById'] }, {
      previousState, action: { type: 'event' } as TurnStateUpdate['action'],
      changes: { turnIds: Object.keys(turns), attemptIds: [], reset: false },
    }];
  };

  it('reuses archive ids for a write that keeps every timeline, and recomputes exactly otherwise', () => {
    const { guard, turns } = guarded(400, index => (index % 50 ? {} : {
      activityTimeline: timeline({ reasonCode: 'context_compaction', archiveId: `archive-${index}` }) }));
    const before: TurnState = { ...createTurnState('conv'),
      turnsById: editEntityRecord({ ...turns, live: turn('live', { content: 'a' }) }).finish() as TurnState['turnsById'] };
    const ids = conversationCompactionArchiveIds(before);
    const [streamed, delta] = write(before, { live: turn('live', { content: 'ab' }) });
    guard.armed = true;
    // A streamed text delta changes one Turn of a long transcript.
    expect(conversationCompactionArchiveIds(streamed, delta)).toBe(ids);
    guard.armed = false;
    const [compacted, receipt] = write(streamed, { live: turn('live', { activityTimeline: timeline(
      { reasonCode: 'context_compaction', archiveId: 'archive-live' }) }) });
    const full = conversationCompactionArchiveIds({ ...compacted, turnsById: { ...compacted.turnsById } });
    expect(conversationCompactionArchiveIds(compacted, receipt)).toEqual(full);
    expect(full).toContain('archive-live');
  });

  it('reads the latest Turn of a lane from its end only, matching the ordered scan', () => {
    const turnIds = Array.from({ length: 300 }, (_, index) => `turn-${index}`);
    const plain = Object.fromEntries(turnIds.map((turnId, index) => [turnId,
      { ...(turn(turnId, {}) as Record<string, unknown>), ordinal: index, actor: index % 2 ? 'assistant' : 'human' }]));
    let reads = 0;
    const state: TurnState = { ...createTurnState('conv'), laneOrder: { main: turnIds },
      turnsById: new Proxy(plain, { get(target, key) { reads += 1; return Reflect.get(target, key); } }) as TurnState['turnsById'] };
    const human = (candidate: TurnRecord) => candidate.actor === 'human';
    expect(latestConversationTurn(state, human)?.turnId).toBe('turn-298');
    expect(reads).toBe(2);
    expect(latestConversationTurn(state, human)).toBe(orderedConversationTurns(state).filter(human).at(-1));
    expect(latestConversationTurn(state, () => false)).toBeNull();
  });
});
