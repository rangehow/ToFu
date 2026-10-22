// @vitest-environment jsdom
/** Replay acknowledgement requires applied authority, not a returned callback. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createConversationTurnRuntime } from './turn-runtime';
import type { ConversationSyncApi, ConversationSyncSnapshot, TurnRecord } from '../api/conversation-sync.generated';

class Source extends EventTarget {
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly url: string) { super(); }
  close() { this.readyState = 2; }
  emit(type: string, fields: object, cursor: string) {
    this.dispatchEvent(new MessageEvent(type, { lastEventId: cursor, data: JSON.stringify({
      contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv', type, ...fields,
    }) }));
  }
}

function turn(revision: number, status: 'running' | 'completed', id = 'answer'): TurnRecord {
  return { turnId: id, presentationId: id, conversationId: 'conv', actor: 'assistant',
    laneId: 'main', ordinal: id === 'answer' ? 1 : 2, kind: 'reply', runId: '',
    status, currentAttemptId: id === 'answer' ? 'attempt' : null,
    projectionRevision: revision, createdAt: 1, updatedAt: revision, settlement: {},
    projection: { content: status === 'completed' ? 'The complete committed answer' : '' } };
}

function snapshot(head: number): ConversationSyncSnapshot {
  return { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
    conversationRevision: head, syncSeq: head, cursor: `cursor-${head}`,
    serverBootId: 'boot', heartbeatIntervalMs: 15000, settings: {}, queueItems: [],
    turns: [turn(head === 1 ? 1 : 2, head === 1 ? 'running' : 'completed')],
    attempts: [{ attemptId: 'attempt', turnId: 'answer', conversationId: 'conv',
      commandId: 'command', taskId: 'task', operation: 'generate',
      baseProjectionRevision: 1, resumeAnchor: {}, createdAt: 1,
      status: head === 1 ? 'running' : 'completed' }] };
}

const cleanups: Array<() => void> = [];
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); vi.useRealTimers(); });

function fixture() {
  const sources: Source[] = [];
  const settings = vi.fn();
  const metadata = vi.fn();
  const api = { snapshot: vi.fn(async () => snapshot(1)),
    eventsUrl: (_id: string, cursor: string) => `/events?after=${cursor}` };
  const runtime = createConversationTurnRuntime({ api: api as unknown as ConversationSyncApi,
    isActive: () => true, renderState: () => true, applySettings: settings,
    applySnapshotMetadata: metadata, onProtocolError: vi.fn(), eventSourceFactory: url => {
      const source = new Source(url); sources.push(source); return source as unknown as EventSource;
    } });
  const conversation = { id: 'conv' };
  cleanups.push(() => runtime.disposeConversation('conv'));
  return { runtime, conversation, api, sources, settings, metadata };
}

describe('required conversation projection ingestion', () => {
  it('does not acknowledge a stale full snapshot after a newer partial command acknowledgement', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    let resolve!: (value: ConversationSyncSnapshot) => void;
    f.api.snapshot.mockImplementationOnce(() => new Promise(yes => { resolve = yes; }))
      .mockResolvedValue(snapshot(3));
    const hydration = f.runtime.hydrateConversation(f.conversation);
    const rejected = expect(hydration).rejects.toThrow(/snapshot.*(stale|applied)/i);
    await vi.advanceTimersByTimeAsync(0);
    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 3, turn: turn(3, 'completed', 'unrelated'),
    } });
    const settingsBefore = f.settings.mock.calls.length;
    const metadataBefore = f.metadata.mock.calls.length;
    resolve(snapshot(2));
    await rejected;
    expect(store.getState().turnsById.answer?.status).toBe('running');
    expect(f.sources).toHaveLength(1);
    expect(f.settings).toHaveBeenCalledTimes(settingsBefore);
    expect(f.metadata).toHaveBeenCalledTimes(metadataBefore);
    await vi.advanceTimersByTimeAsync(2000);
    expect(f.api.snapshot).toHaveBeenCalledTimes(3);
    expect(store.getState().turnsById.answer?.status).toBe('completed');
    expect(store.getState().turnsById.answer?.projection.content).toBe('The complete committed answer');
    expect(f.sources).toHaveLength(2);
    expect(f.sources[1].url).toContain('after=cursor-3');
  });

  it.each(['turn.upsert', 'turn.deleted'] as const)('repairs a silently rejected %s instead of consuming its replay cursor', async type => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 3, turn: turn(3, 'completed', 'unrelated'),
    } });
    f.api.snapshot.mockResolvedValue(snapshot(3));
    f.sources[0].emit(type, { syncSeq: 2, occurredAt: 1,
      payload: { conversationRevision: 2,
        ...(type === 'turn.upsert' ? { turns: [turn(2, 'completed')] }
          : { deletedTurnIds: ['answer'] }),
      } }, 'cursor-2');
    await vi.advanceTimersByTimeAsync(0);
    expect(f.api.snapshot).toHaveBeenCalledTimes(2);
    expect(store.getState().turnsById.answer?.status).toBe('completed');
    expect(f.sources[1].url).toContain('after=cursor-3');
  });
});
