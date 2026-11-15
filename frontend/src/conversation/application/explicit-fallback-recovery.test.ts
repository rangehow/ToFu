import { describe, expect, it, vi } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { createTurnState, type TurnState } from '../domain/turn-store';
import {
  createExplicitFallbackRecovery,
  type ExplicitFallbackOffer,
  type ExplicitFallbackRecoveryPorts,
} from './explicit-fallback-recovery';

function turn(status: TurnRecord['status'] = 'running'): TurnRecord {
  return {
    turnId: 'answer', presentationId: 'answer', conversationId: 'conv', laneId: 'main',
    ordinal: 2, actor: 'assistant', kind: 'reply', runId: '', status,
    currentAttemptId: 'old', projectionRevision: 7,
    projection: { content: '', thinking: '', toolRounds: [], model: 'primary' },
    settlement: {}, createdAt: 1, updatedAt: 1,
  } as TurnRecord;
}

const offer: ExplicitFallbackOffer = {
  conversationId: 'conv', turnId: 'answer', attemptId: 'old', projectionRevision: 7,
  currentModel: 'primary', model: 'fallback', providerId: 'provider', providerName: 'Provider',
};

function fixture(options: {
  terminalStatus?: TurnRecord['status'];
  resumeOptions?: TurnRecord['settlement']['resumeOptions'];
  originalCompleted?: boolean;
  abortError?: unknown;
  lostAck?: boolean;
  acceptedBeforeLostAck?: boolean;
} = {}) {
  const state = createTurnState('conv');
  state.turnsById.answer = turn();
  state.attemptsById.old = { attemptId: 'old', turnId: 'answer', status: 'running' };
  state.laneOrder.main = ['answer'];
  const listeners = new Set<() => void>();
  const operations: unknown[] = [];
  const errors: unknown[] = [];
  const config = { model: 'fallback', preferredProviderId: 'provider', routing: { preferred_provider_id: 'provider' } };
  const inputUpdate = { contextSnapshot: { blockId: 'turn-context', snapshot: { model: 'fallback' } } };
  let settled = false;
  const ports: ExplicitFallbackRecoveryPorts = {
    hydrate: vi.fn(async () => {}),
    subscribe: vi.fn((_id, changed) => { listeners.add(changed); return () => listeners.delete(changed); }),
    readState: () => state,
    prepareTarget: vi.fn(async () => ({ config })),
    contextInputUpdate: () => ({ inputUpdate, expectedInputProjectionRevision: 3 }),
    regenerateInputUpdate: () => ({ inputUpdate: { content: 'original', ...inputUpdate }, expectedInputProjectionRevision: 3 }),
    abortAttempt: vi.fn(async () => {
      if (options.abortError) throw options.abortError;
    }),
    operateAttempt: vi.fn(async operation => {
      operations.push(operation);
      if (options.lostAck && operations.length === 1) {
        if (options.acceptedBeforeLostAck) {
          state.attemptsById.new = {
            attemptId: 'new', turnId: 'answer', commandId: operation.commandId,
            operation: operation.operation, status: 'running',
          };
        }
        throw new TypeError('lost ACK');
      }
      state.attemptsById.new = {
        attemptId: 'new', turnId: 'answer', commandId: operation.commandId,
        operation: operation.operation, status: 'running',
      };
      return { attempt: state.attemptsById.new };
    }),
    hasAuthoritativeCommand: (_id, commandId) =>
      Object.values(state.attemptsById).some(attempt => attempt?.commandId === commandId),
    reportError: error => errors.push(error),
    commandId: () => 'recovery-command',
  };
  const recovery = createExplicitFallbackRecovery(ports);
  const settle = () => {
    if (settled) throw new Error('test already settled');
    settled = true;
    if (options.originalCompleted) {
      state.turnsById.answer = { ...turn('completed'), currentAttemptId: 'old' };
      state.attemptsById.old = { attemptId: 'old', turnId: 'answer', status: 'completed' };
    } else {
      state.turnsById.answer = {
        ...turn(options.terminalStatus ?? 'interrupted'), currentAttemptId: 'old', projectionRevision: 8,
        settlement: { resumeOptions: options.resumeOptions ?? [
          { operation: 'checkpoint_resume', anchor: { type: 'tool_checkpoint', keptToolRounds: 1 } },
        ] },
      } as TurnRecord;
      state.attemptsById.old = { attemptId: 'old', turnId: 'answer', status: 'interrupted' };
    }
    for (const listener of [...listeners]) listener();
  };
  return { state, ports, operations, errors, recovery, settle, config, inputUpdate, listeners };
}

describe('explicit configured fallback recovery', () => {
  it('stops the exact attempt, waits for settlement, and forwards the first offered anchor and route config', async () => {
    const f = fixture();
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalledWith('old'));
    expect(f.operations).toHaveLength(0);
    f.settle();
    await running;
    expect(f.errors).toEqual([]);
    expect(f.operations).toEqual([{
      conversationId: 'conv', turnId: 'answer', operation: 'checkpoint_resume',
      commandId: 'recovery-command', config: f.config,
      resumeAnchor: { type: 'tool_checkpoint', keptToolRounds: 1 },
      inputUpdate: f.inputUpdate, expectedInputProjectionRevision: 3,
    }]);
    expect(f.listeners.size).toBe(0);
  });

  it('prefers the first continue offer and its exact anchor over checkpoint or regenerate', async () => {
    const f = fixture({ resumeOptions: [
      { operation: 'continue', anchor: { type: 'replay_only', contentChars: 0 } },
      { operation: 'checkpoint_resume', anchor: { type: 'tool_checkpoint', keptToolRounds: 2 } },
      { operation: 'regenerate', anchor: {} },
    ] });
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalled());
    f.settle();
    await running;
    expect(f.operations[0]).toMatchObject({ operation: 'continue',
      resumeAnchor: { type: 'replay_only', contentChars: 0 } });
  });

  it('uses regenerate only for the no-retained-progress pre-token offer', async () => {
    const f = fixture({ resumeOptions: [{ operation: 'regenerate', anchor: {} }] });
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalled());
    f.settle();
    await running;
    expect(f.operations[0]).toMatchObject({ operation: 'regenerate',
      commandId: 'recovery-command', config: f.config });
    expect(f.operations[0]).not.toHaveProperty('resumeAnchor');
    expect(f.operations[0]).toMatchObject({ inputUpdate: {
      content: 'original', ...f.inputUpdate,
    } });
  });

  it('never starts a successor when the original completed during stop', async () => {
    const f = fixture({ originalCompleted: true });
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalled());
    f.settle();
    await running;
    expect(f.operations).toHaveLength(0);
    expect(f.errors).toEqual([]);
  });

  it('leaves the original alone when abort is not acknowledged', async () => {
    const f = fixture({ abortError: new Error('timeout') });
    await f.recovery.use(offer);
    expect(f.operations).toHaveLength(0);
    expect(f.ports.subscribe).not.toHaveBeenCalled();
    expect(String(f.errors[0])).toContain('Stop was not confirmed');
  });

  it('latches repeated clicks to one successor', async () => {
    const f = fixture();
    const first = f.recovery.use(offer);
    const second = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalledTimes(1));
    f.settle();
    await Promise.all([first, second]);
    expect(f.operations).toHaveLength(1);
  });

  it('replays the identical command when a lost ACK was not committed', async () => {
    const f = fixture({ lostAck: true });
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalled());
    f.settle();
    await running;
    expect(f.ports.hydrate).toHaveBeenCalled();
    expect(f.errors).toEqual([]);
    expect(f.operations).toHaveLength(2);
    expect(f.operations[0]).toEqual(f.operations[1]);
    expect(f.operations.map((operation: any) => operation.commandId))
      .toEqual(['recovery-command', 'recovery-command']);
  });

  it('does not confuse a different successor with the exact stopped attempt', async () => {
    const f = fixture();
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalled());
    f.state.attemptsById.old = { attemptId: 'old', turnId: 'answer', status: 'interrupted' };
    f.state.turnsById.answer = { ...turn('running'), currentAttemptId: 'someone-else' };
    for (const listener of f.listeners) listener();
    expect(f.operations).toHaveLength(0);
    f.settle();
    await running;
    expect(f.operations).toHaveLength(1);
  });

  it('replays a repeatedly uncertain ACK without aborting or minting another command', async () => {
    const f = fixture({ lostAck: true });
    f.ports.operateAttempt = vi.fn(async operation => {
      f.operations.push(operation);
      throw new TypeError('still lost');
    });
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalledTimes(1));
    f.settle();
    await running;
    expect(f.operations).toHaveLength(2);
    await f.recovery.retry();
    expect(f.ports.abortAttempt).toHaveBeenCalledTimes(1);
    expect(f.operations).toHaveLength(3);
    expect(f.operations[2]).toEqual(f.operations[0]);
    await f.recovery.retry();
    expect(f.ports.abortAttempt).toHaveBeenCalledTimes(1);
    expect(f.operations).toHaveLength(4);
    expect(new Set(f.operations.map((operation: any) => operation.commandId)).size).toBe(1);
  });

  it('adopts a committed command after lost ACK instead of replaying it', async () => {
    const f = fixture({ lostAck: true, acceptedBeforeLostAck: true });
    const running = f.recovery.use(offer);
    await vi.waitFor(() => expect(f.ports.abortAttempt).toHaveBeenCalled());
    f.settle();
    await running;
    expect(f.operations).toHaveLength(1);
    expect(f.errors).toEqual([]);
    expect(f.ports.hydrate).toHaveBeenCalled();
  });
});
