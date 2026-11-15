/** Paper preview changes on a long transcript: only Turns the reducer receipt
 * names are compared, with the same receipts as a full comparison. */
import { expect, it } from 'vitest';
import { editEntityRecord } from '../../core/entity-record';
import { createTurnState, type TurnState, type TurnStateUpdate } from '../../core/turn-state';
import { createPaperPreviewChanges } from './preview-changes';

const guard = { armed: false };
const reply = (turnId: string, ordinal: number, projection: Record<string, unknown>) => ({
  turnId, conversationId: 'conv', laneId: 'main', ordinal, actor: 'assistant', status: 'running', projection,
});
const texChange = (commandId: string, path: string) => ({ fileChanges: {
  commandId, state: 'applied', files: [{ changeId: `${commandId}-file`, path, ok: true, source: 'local' }] } });

function transcript(): TurnState {
  const turns: Record<string, unknown> = {};
  for (let index = 0; index < 400; index += 1) {
    turns[`history-${index}`] = new Proxy(reply(`history-${index}`, index, texChange(`old-${index}`, `old-${index}.tex`)), {
      get(target, key, receiver) {
        if (guard.armed) throw new Error(`read unchanged Turn history-${index}.${String(key)}`);
        return Reflect.get(target, key, receiver);
      },
    });
  }
  turns.live = reply('live', 1000, { content: 'drafting' });
  return { ...createTurnState('conv'), turnsById: editEntityRecord(turns).finish() as TurnState['turnsById'] };
}

function write(previousState: TurnState, live: Record<string, unknown>, receipt: boolean): [TurnState, TurnStateUpdate] {
  const draft = editEntityRecord<unknown>(previousState.turnsById);
  draft.record.live = live;
  return [{ ...previousState, turnsById: draft.finish() as TurnState['turnsById'] }, {
    previousState, action: { type: 'event' } as TurnStateUpdate['action'],
    ...(receipt ? { changes: { turnIds: ['live'], attemptIds: [], reset: false } } : {}),
  }];
}

function run(receipt: boolean) {
  const received: unknown[] = [];
  const owner = createPaperPreviewChanges({ changed: rows => received.push(...rows), activated: () => {} });
  owner.activateConversation('conv');
  const before = transcript();
  const [streamed, delta] = write(before, reply('live', 1000, { content: 'drafting more' }), receipt);
  const [edited, edit] = write(streamed, reply('live', 1000, texChange('cmd-1', 'paper/main.tex')), receipt);
  guard.armed = receipt;
  try {
    owner.observe('conv', streamed, delta);
    owner.observe('conv', edited, edit);
  } finally { guard.armed = false; }
  return received;
}

it('compares only the Turns a receipt names, with the same changes as a full comparison', () => {
  const viaReceipt = run(true);
  expect(viaReceipt).toEqual(run(false));
  expect(viaReceipt).toMatchObject([{ conversationId: 'conv', changeId: 'cmd-1', file: 'paper/main.tex' }]);
});
