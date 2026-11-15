import { describe, expect, it } from 'vitest';
import { attentionQueue, deriveAttention, headline, openQuestions, type QuestionTurn } from './attention';

const LEAD = 'coordinator-lead';

describe('owner attention adapter', () => {
  const ask = (turnId: string, ordinal: number, round: Record<string, unknown>): QuestionTurn =>
    ({ turnId, ordinal, actor: 'assistant', createdAt: ordinal * 10, projection: { toolRounds: [{ toolName: 'ask_human', ...round }] } });

  it('keeps a non-blocking question open until a human Turn follows, a blocking one while it awaits', () => {
    const turns = [
      ask('t1', 1, { guidanceId: 'g1', guidanceQuestion: '**Only you** can unblock\nmore', guidanceBlocking: false, status: 'done' }),
      ask('t2', 2, { guidanceId: 'g2', guidanceQuestion: 'Blocking?', guidanceBlocking: true, status: 'awaiting_human' }),
      ask('t3', 3, { guidanceId: 'g3', guidanceQuestion: 'Answered', guidanceBlocking: true, status: 'done' }),
    ];
    expect(openQuestions(LEAD, turns).map(each => each.guidanceId)).toEqual(['g1', 'g2']);
    const answered = [...turns, { turnId: 'h', ordinal: 4, actor: 'human', createdAt: 40 }];
    expect(openQuestions(LEAD, answered).map(each => each.guidanceId)).toEqual(['g2']);
    expect(headline('**Only you** can unblock\nmore')).toBe('Only you can unblock');
  });

  it('treats a round without guidanceBlocking as blocking: open while it awaits, closed once answered', () => {
    const waiting = ask('t1', 1, { guidanceId: 'g1', guidanceQuestion: 'Ship it today?', status: 'awaiting_human' });
    const [open] = openQuestions(LEAD, [waiting]);
    expect(open).toMatchObject({ guidanceId: 'g1', blocking: true });
    const answered = ask('t1', 1, { guidanceId: 'g1', guidanceQuestion: 'Ship it today?', status: 'done' });
    expect(openQuestions(LEAD, [answered])).toEqual([]);
  });

  it('emits the HARNESS-1C shape, ids and tier order', () => {
    const attention = deriveAttention(1000, [{
      path: '/p', coordinatorId: LEAD,
      questions: [
        { conversationId: LEAD, turnId: 't1', ordinal: 1, guidanceId: 'g1', blocking: false, question: 'Later', defaultText: 'keep going', createdAt: 5 },
        { conversationId: LEAD, turnId: 't2', ordinal: 2, guidanceId: 'g2', blocking: true, question: 'Now', defaultText: '', createdAt: 9 },
      ],
      progress: { projectPath: '/p', conversationId: LEAD, bindingRevision: 1, board: null, commitments: [
        { title: 'Ship', firstDueAt: 500, dueAt: 900, slips: 1, createdAt: 1, state: 'open' },
        { title: 'Later', firstDueAt: 2000, dueAt: 2000, slips: 0, createdAt: 1, state: 'open' },
        { title: 'Kept', firstDueAt: 100, dueAt: 100, slips: 0, createdAt: 1, state: 'kept' },
      ] },
      reports: { projectPath: '/p', conversationId: LEAD, bindingRevision: 1, unread: true,
        latest: { turnId: 'r1', ordinal: 3, title: 'Delivered', kind: 'delivery', createdAt: 700, content: '' } },
    }]);
    expect(attentionQueue(attention).map(item => item.id)).toEqual([
      `question:${LEAD}:g2`, `question:${LEAD}:g1`, 'commitment:/p:Ship:900', `delivery:${LEAD}:r1`,
    ]);
    expect(attention.counts).toEqual({ decisions: 2, exceptions: 1, deliverables: 1, digests: 0 });
    expect(attention.decisions[1].default).toBe('keep going');
  });
});

