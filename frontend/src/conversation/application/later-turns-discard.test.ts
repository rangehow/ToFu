// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  LATER_TURNS_DISCARD,
  LATER_TURNS_KEEP,
  laterTurnsDiscardCommandId,
  laterTurnsRefusal,
  reviewLaterTurnsDiscard,
  type LaterTurnsReviewPorts,
} from './later-turns-discard';

const closure = {
  count: 3,
  turnIds: ['owner-later', 'reply-later', 'branch-child'],
  turns: [
    { turnId: 'owner-later', laneId: 'main', actor: 'human', kind: 'ingested',
      status: 'completed', createdAt: 1_700_000_000_000, preview: 'please also check the docs' },
    { turnId: 'reply-later', laneId: 'main', actor: 'assistant', kind: 'reply',
      status: 'completed', createdAt: 1_700_000_060_000, preview: '' },
    { turnId: 'branch-child', laneId: 'lane_x', actor: 'assistant', kind: 'reply',
      status: 'completed', createdAt: 1_700_000_120_000, preview: 'branch reply' },
  ],
};

function refusalError(kind: string, laterTurns: unknown = closure) {
  return { status: 409, body: { ok: false, error: { kind, message: 'refused' }, laterTurns } };
}

function ports(choice: string | null) {
  const shown: Array<Parameters<LaterTurnsReviewPorts['showChoice']>[0]> = [];
  const translate = (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key;
  const value: LaterTurnsReviewPorts = {
    showChoice: async (config) => { shown.push(config); return choice; },
    translate,
    formatTime: (ms) => `t${ms}`,
  };
  return { value, shown };
}

describe('laterTurnsRefusal', () => {
  it('reads the typed 409 closure from a failed createAttempt', () => {
    const refusal = laterTurnsRefusal(refusalError('later_turns_unacknowledged'));
    expect(refusal?.kind).toBe('later_turns_unacknowledged');
    expect(refusal?.closure.turnIds).toEqual(['owner-later', 'reply-later', 'branch-child']);
    expect(laterTurnsRefusal(refusalError('later_turns_changed'))?.kind).toBe('later_turns_changed');
  });

  it('ignores other conflicts and malformed closures', () => {
    expect(laterTurnsRefusal({ status: 409, body: { error: { kind: 'lane_busy' } } })).toBeNull();
    expect(laterTurnsRefusal(refusalError('later_turns_unacknowledged', { count: 1 }))).toBeNull();
    expect(laterTurnsRefusal(refusalError('later_turns_unacknowledged',
      { ...closure, count: 2 }))).toBeNull();
    expect(laterTurnsRefusal(new Error('network'))).toBeNull();
  });
});

describe('reviewLaterTurnsDiscard', () => {
  it('offers keep first as default and lists who, when and the first line of each Turn', async () => {
    const { value, shown } = ports(LATER_TURNS_KEEP);
    const refusal = laterTurnsRefusal(refusalError('later_turns_unacknowledged'))!;
    expect(await reviewLaterTurnsDiscard(refusal, 'main', value)).toBeNull();
    const [dialog] = shown;
    expect(dialog.options.map((option) => option.value)).toEqual([
      LATER_TURNS_KEEP, LATER_TURNS_DISCARD]);
    expect(dialog.dismissValue).toBe(LATER_TURNS_KEEP);
    expect(dialog.title).toBe('laterTurns.title({"count":3,"s":"s"})');
    expect(dialog.message.split('\n')).toEqual([
      'laterTurns.lead',
      '',
      '• laterTurns.actorYou · t1700000000000 — please also check the docs',
      '• laterTurns.actorAssistant · t1700000060000 — laterTurns.noText',
      '• laterTurns.actorAssistant · t1700000120000 laterTurns.inBranch — branch reply',
    ]);
  });

  it('acknowledges exactly the listed Turns only when the owner discards them', async () => {
    const { value } = ports(LATER_TURNS_DISCARD);
    const refusal = laterTurnsRefusal(refusalError('later_turns_unacknowledged'))!;
    expect(await reviewLaterTurnsDiscard(refusal, 'main', value)).toEqual(closure.turnIds);
  });

  it('treats a dismissed dialog as keep', async () => {
    const { value } = ports(null);
    const refusal = laterTurnsRefusal(refusalError('later_turns_unacknowledged'))!;
    expect(await reviewLaterTurnsDiscard(refusal, 'main', value)).toBeNull();
  });

  it('explains a closure that changed after the owner confirmed it', async () => {
    const { value, shown } = ports(LATER_TURNS_KEEP);
    const refusal = laterTurnsRefusal(refusalError('later_turns_changed'))!;
    await reviewLaterTurnsDiscard(refusal, 'main', value);
    expect(shown[0].message.split('\n')[0]).toBe('laterTurns.changed');
  });
});

describe('laterTurnsDiscardCommandId', () => {
  it('is stable per acknowledged set and distinct across sets', () => {
    const base = 'regenerate:target:4';
    const id = laterTurnsDiscardCommandId(base, ['b', 'a']);
    expect(id).toBe(laterTurnsDiscardCommandId(base, ['a', 'b']));
    expect(id).not.toBe(laterTurnsDiscardCommandId(base, ['a', 'b', 'c']));
    expect(id.startsWith(`${base}:discard-2-`)).toBe(true);
    expect(id.length).toBeLessThanOrEqual(256);
  });
});
