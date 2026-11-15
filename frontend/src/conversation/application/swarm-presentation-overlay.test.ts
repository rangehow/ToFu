/** compose reads only the newest assistant Turns it may present, however long the transcript. */
import { expect, it } from 'vitest';
import { editEntityRecord } from '../../core/entity-record';
import type { TurnState } from '../domain/turn-store';
import { swarmPresentationOverlay } from './swarm-presentation-overlay';

const guard = { armed: false };
const reply = (turnId: string, actor: string, swarm: boolean) => ({
  turnId, conversationId: 'conv', laneId: 'main', actor, projectionRevision: 1, updatedAt: 1,
  projection: { content: turnId, toolRounds: swarm
    ? [{ toolName: 'spawn_agents', _swarm: true, _swarmRunId: `run-${turnId}`, _swarmAgents: [] }] : [] },
});

/** `history` guarded Turns, then `recent` alternating human/assistant Turns. */
function transcript(history: number, recent: number, swarm: (index: number) => boolean, publish: boolean): TurnState {
  const turns: Record<string, unknown> = {};
  const ids: string[] = [];
  for (let index = 0; index < history; index += 1) {
    const turnId = `history-${index}`;
    ids.push(turnId);
    turns[turnId] = new Proxy(reply(turnId, index % 2 ? 'assistant' : 'human', false), {
      get(target, key, receiver) {
        if (guard.armed) throw new Error(`read unchanged Turn ${turnId}.${String(key)}`);
        return Reflect.get(target, key, receiver);
      },
    });
  }
  for (let index = 0; index < recent; index += 1) {
    const turnId = `recent-${index}`;
    ids.push(turnId);
    turns[turnId] = reply(turnId, index % 2 ? 'assistant' : 'human', swarm(index));
  }
  const laneOrder = { main: ids };
  return { conversationId: 'conv', queueItems: [],
    turnsById: (publish ? editEntityRecord(turns).finish() : turns) as TurnState['turnsById'],
    laneOrder: (publish ? editEntityRecord(laneOrder).finish() : laneOrder) as TurnState['laneOrder'],
  } as unknown as TurnState;
}

it('leaves a long transcript beyond the newest candidates unread', () => {
  const state = transcript(400, 140, () => false, true);
  guard.armed = true;
  try {
    expect(swarmPresentationOverlay.compose('conv', state, state, () => null)).toBe(state);
    expect(swarmPresentationOverlay.compose('conv', state, state, () => null)).toBe(state);
  } finally { guard.armed = false; }
});

it.each([true, false])('presents exactly the newest 64 assistant Turns (published records: %s)', publish => {
  // 200 assistant Turns, all with swarm rounds: only the newest 64 are candidates.
  const state = transcript(0, 400, () => true, publish);
  const composed = swarmPresentationOverlay.compose('conv', state, state, () => null);
  const presented = Array.from({ length: 200 }, (_, index) => `recent-${2 * index + 1}`)
    .filter(turnId => composed.turnsById[turnId] !== state.turnsById[turnId]);
  expect(presented).toEqual(Array.from({ length: 64 }, (_, index) => `recent-${2 * (136 + index) + 1}`));
});

it('follows a lane change to its new newest Turn', () => {
  const state = transcript(0, 140, () => false, true);
  expect(swarmPresentationOverlay.compose('conv', state, state, () => null)).toBe(state);
  const turns = editEntityRecord(state.turnsById as Record<string, unknown>);
  turns.record.launch = reply('launch', 'assistant', true);
  const lanes = editEntityRecord(state.laneOrder as Record<string, string[]>);
  lanes.record.main = [...state.laneOrder.main!, 'launch'];
  const next = { ...state, turnsById: turns.finish(), laneOrder: lanes.finish() } as unknown as TurnState;
  const composed = swarmPresentationOverlay.compose('conv', next, next, () => null);
  expect(composed.turnsById.launch).not.toBe(next.turnsById.launch);
  expect(composed.turnsById.launch?.projection.toolRounds?.[0]?._swarmRunId).toBe('run-launch');
});
