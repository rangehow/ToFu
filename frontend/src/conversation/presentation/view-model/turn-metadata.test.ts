/** A whole-turn translation failure stays a visible fact until a retry or a translation replaces it. */
import { describe, expect, it } from 'vitest';
import type { TurnProjection, TurnRecord } from '../../../api/conversation-sync.generated';
import { createTurnState } from '../../domain/turn-store';
import { selectConversationViewModel } from '../conversation-view-model';
import type { TranslationActivity } from './types';

/* The verdict the whole-turn worker recorded on conv mwcuhvl0800rdd. */
const VERDICT = {
  reason: 'worker_error',
  error: 'Upstream rate-limit attempt budget exhausted (8/8)',
  at: 1794450199,
};

function reply(projection: TurnProjection = {}): TurnRecord {
  return { turnId: 'reply', presentationId: 'reply', conversationId: 'translation-failure',
    laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status: 'completed',
    currentAttemptId: null, createdAt: 1000, updatedAt: 2000, projectionRevision: 1, settlement: {},
    projection: { content: 'The answer', segments: [{ type: 'text', blockId: 'text:terminal',
      text: 'The answer', terminal: true, deliverable: true }], ...projection } };
}

function translationOf(turn: TurnRecord, activity?: TranslationActivity) {
  const state = createTurnState(turn.conversationId);
  state.turnsById[turn.turnId] = turn;
  state.laneOrder.main = [turn.turnId];
  const before = JSON.stringify(turn);
  const presentation = activity
    ? { translationActivityByTurn: new Map([[turn.turnId, activity]]) } : {};
  const translation = selectConversationViewModel(state, {}, presentation)
    .mainLane.turns[0].metadata.translation;
  expect(JSON.stringify(turn)).toBe(before);
  return translation;
}

describe('whole-turn translation failure', () => {
  it('surfaces the durable verdict after reload as a failed state with its cause', () => {
    expect(translationOf(reply({ translation: { sendFailure: VERDICT } }))).toMatchObject({
      failed: true, failureDetail: VERDICT.error,
      pending: false, completed: false, available: false, displayMode: 'original',
    });
  });

  it('withdraws the failure while a retry is pending', () => {
    const failed = reply({ translation: { sendFailure: VERDICT } });
    const retrying = translationOf(failed, { status: 'pending', message: 'Translating…' });
    expect(retrying).toMatchObject({ failed: false, pending: true });
    expect(retrying).not.toHaveProperty('failureDetail');
  });

  it('never reports a failure once a translation is available or completed', () => {
    expect(translationOf(reply({ translatedContent: '答案',
      translation: { status: 'completed', sendFailure: VERDICT } })))
      .toMatchObject({ failed: false, available: true, completed: true });
    expect(translationOf(reply({ translation: { status: 'skipped',
      skippedReason: 'already_target_language', sendFailure: VERDICT } })))
      .toMatchObject({ failed: false, completed: true });
    // A failed manual re-translation does not hide an existing translation.
    expect(translationOf(reply({ translatedContent: '答案', translation: { status: 'completed' } }),
      { status: 'failed', message: 'HTTP 429', error: 'HTTP 429' }))
      .toMatchObject({ failed: false, available: true });
  });

  it('reports a live failed task, whose cause outranks an older verdict', () => {
    expect(translationOf(reply(), { status: 'failed', message: 'HTTP 429', error: 'HTTP 429' }))
      .toMatchObject({ failed: true, failureDetail: 'HTTP 429' });
    expect(translationOf(reply({ translation: { sendFailure: VERDICT } }),
      { status: 'failed', error: 'HTTP 503' }).failureDetail).toBe('HTTP 503');
    expect(translationOf(reply(), { status: 'failed', message: 'Translation task expired' })
      .failureDetail).toBe('Translation task expired');
  });

  it('narrows the cause to plain text and never invents one', () => {
    expect(translationOf(reply({ translation: { sendFailure: { ...VERDICT, error: '' } } }))
      .failureDetail).toBe('worker_error');
    const markup = '<img src=x onerror=alert(1)>';
    expect(translationOf(reply({ translation: { sendFailure: { error: markup } } }))
      .failureDetail).toBe(markup);
    const opaque = translationOf(reply({ translation: { sendFailure: [VERDICT] } }));
    expect(opaque).toMatchObject({ failed: true });
    expect(opaque).not.toHaveProperty('failureDetail');
    // The contract's own failed status (legacy `_translateError`) is a failure too.
    expect(translationOf(reply({ translation: { status: 'failed', error: { message: 'legacy' } } })))
      .toMatchObject({ failed: true, failureDetail: 'legacy' });
  });

  it('reports no failure for a turn whose translation never failed', () => {
    for (const translation of [translationOf(reply()),
      translationOf(reply({ translation: { status: 'pending', taskId: 'task-1' } }))]) {
      expect(translation.failed).toBe(false);
      expect(translation).not.toHaveProperty('failureDetail');
    }
  });
});
