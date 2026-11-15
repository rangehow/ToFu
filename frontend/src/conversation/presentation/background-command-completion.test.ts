/** Background evidence on a long transcript: a write that keeps every evidence
 * input must not revisit the rest; the answer always equals a full scan. */
import { expect, it } from 'vitest';
import { editEntityRecord } from '../../core/entity-record';
import { createTurnState, type TurnState, type TurnStateUpdate } from '../domain/turn-store';
import { createBackgroundActivityEvidenceObserver } from './background-command-completion';

const guard = { armed: false };
const reply = (turnId: string, projection: Record<string, unknown>) => ({
  turnId, conversationId: 'conv', laneId: 'main', ordinal: 1, actor: 'assistant', status: 'completed', projection,
});
const round = (commandId: string) => ({ toolCallId: commandId, results: [{ backgroundCommandId: commandId }] });

function transcript(live: Record<string, unknown>): TurnState {
  const turns: Record<string, unknown> = { live };
  for (let index = 0; index < 400; index += 1) {
    turns[`history-${index}`] = new Proxy(reply(`history-${index}`,
      { toolRounds: index % 40 ? [] : [round(`history-command-${index}`)] }), {
      get(target, key, receiver) {
        if (guard.armed) throw new Error(`read unchanged Turn history-${index}.${String(key)}`);
        return Reflect.get(target, key, receiver);
      },
    });
  }
  return { ...createTurnState('conv'), turnsById: editEntityRecord(turns).finish() as TurnState['turnsById'] };
}

function write(previousState: TurnState, live: Record<string, unknown>): [TurnState, TurnStateUpdate] {
  const draft = editEntityRecord<unknown>(previousState.turnsById);
  draft.record.live = live;
  return [{ ...previousState, turnsById: draft.finish() as TurnState['turnsById'] }, {
    previousState, action: { type: 'event' } as TurnStateUpdate['action'],
    changes: { turnIds: ['live'], attemptIds: [], reset: false },
  }];
}

it('skips the transcript for a text delta and still detects a new background command', () => {
  const observe = createBackgroundActivityEvidenceObserver();
  const reference = createBackgroundActivityEvidenceObserver();
  const rounds = [round('live-command-1')];
  const before = transcript(reply('live', { content: 'a', toolRounds: rounds }));
  expect(observe(before)).toBe(true);
  expect(reference(before)).toBe(true);

  const [streamed, delta] = write(before, reply('live', { content: 'ab', toolRounds: rounds }));
  guard.armed = true;
  expect(observe(streamed, delta)).toBe(false);
  guard.armed = false;
  expect(reference(streamed)).toBe(false);

  const [launched, launch] = write(streamed, reply('live', { content: 'ab', toolRounds: [...rounds, round('live-command-2')] }));
  expect(observe(launched, launch)).toBe(true);
  expect(reference(launched)).toBe(true);
  // A receipt that does not follow the last observed state proves nothing.
  const [, stale] = write(before, reply('live', { content: 'abc', toolRounds: rounds }));
  expect(observe(streamed, stale)).toBe(reference(streamed));
});
