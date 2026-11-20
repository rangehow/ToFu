import { describe, expect, it } from 'vitest';
import { findQuestionFocus, type QuestionFocusTurn } from './question-focus';

const ask = (turnId: string, ordinal: number, round: Record<string, unknown>): QuestionFocusTurn =>
  ({ turnId, ordinal, actor: 'assistant', projection: { toolRounds: [{ toolName: 'ask_human', ...round }] } });
const human = (turnId: string, ordinal: number): QuestionFocusTurn => ({ turnId, ordinal, actor: 'human' });

describe('"answer in conversation" lands on the question and replies to it', () => {
  const turns = [
    ask('t1', 1, { guidanceId: 'hg_old', guidanceBlocking: false, guidanceQuestion: 'Old question?' }),
    human('t2', 2),
    ask('t3', 3, { guidanceId: 'hg_queue', status: 'awaiting_human', guidanceQuestion: 'Which compute?' }),
    ask('t4', 4, { guidanceId: 'hg_done', status: 'done', guidanceQuestion: 'Answered already?' }),
  ];

  it('opens a named question wherever it is, with a reply quote that carries its id', () => {
    expect(findQuestionFocus(turns, 'hg_old')).toEqual({ turnId: 't1', ordinal: 1, guidanceId: 'hg_old',
      quote: 'Old question?\n\n[question hg_old]' });
  });

  it('without a name, picks the newest question still open', () => {
    expect(findQuestionFocus(turns)).toMatchObject({ turnId: 't3', guidanceId: 'hg_queue' });
    expect(findQuestionFocus(turns.slice(0, 2))).toBeNull();
  });

  it('skips a question its lead withdrew, but still opens it by name', () => {
    const withdrawn = [...turns, ask('t5', 5, { guidanceId: 'hg_withdrawn', status: 'awaiting_human',
      guidanceQuestion: 'Settled?', guidanceWithdrawn: { reason: 'answered in chat', at: 1 } })];
    expect(findQuestionFocus(withdrawn)).toMatchObject({ guidanceId: 'hg_queue' });
    expect(findQuestionFocus(withdrawn, 'hg_withdrawn')).toMatchObject({ turnId: 't5' });
  });

  it('skips a non-blocking question the owner settled on its default', () => {
    const confirmed = [...turns, ask('t5', 5, { guidanceId: 'hg_confirmed', status: 'done', guidanceBlocking: false,
      guidanceQuestion: 'Venue?', guidanceDefaultAccepted: { at: 1, by: 1 } })];
    expect(findQuestionFocus(confirmed)).toMatchObject({ guidanceId: 'hg_queue' });
  });

  it('numbers a form question like its form', () => {
    const form = [ask('t9', 9, { guidanceId: 'hg_form', status: 'awaiting_human', guidanceQuestion: 'summary',
      guidanceBatchQuestions: [{ question: 'Budget?' }, { question: 'Venue?' }] })];
    expect(findQuestionFocus(form)!.quote).toBe('1. Budget?\n\n2. Venue?\n\n[question hg_form]');
  });
});
