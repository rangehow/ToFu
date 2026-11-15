import { describe, expect, it } from 'vitest';
import type { AttemptRecord, TurnRecord } from '../../api/conversation-sync.generated';
import { createTurnState, type TurnState, type TurnStateUpdate } from '../domain/turn-store';
import {
  createConversationViewModelSelector,
  selectConversationViewModel,
  type ConversationViewModel,
} from './conversation-view-model';

/* `report_to_user` appends a settled report Turn at the lane tail while the
 * Coordinator reply that publishes it is still running, and storage stamps the
 * lane tail as that report's parentTurnId. The report reads inside the reply,
 * right after the call whose result names it; it never trails the live reply
 * as a separate, newer answer, and nothing moves when the reply settles. */

function turn(id: string, ordinal: number, fields: Partial<TurnRecord>): TurnRecord {
  return {
    turnId: id, presentationId: id, conversationId: 'c', laneId: 'main', ordinal,
    actor: 'assistant', kind: 'output', runId: '', status: 'completed',
    projectionRevision: 1, createdAt: ordinal * 10, updatedAt: ordinal * 10 + 1,
    projection: { content: id }, settlement: {},
    ...fields,
  } as TurnRecord;
}

function attempt(turnId: string, fields: Partial<AttemptRecord> = {}): AttemptRecord {
  return {
    attemptId: `attempt-${turnId}`, conversationId: 'c', turnId,
    commandId: `cmd-${turnId}`, taskId: `task-${turnId}`, operation: 'generate',
    status: 'running', baseProjectionRevision: 0, resumeAnchor: {}, createdAt: 10,
    startedAt: 10, settledAt: null, ...fields,
  } as unknown as AttemptRecord;
}

function state(turns: TurnRecord[], attempts: AttemptRecord[]): TurnState {
  const next = createTurnState('c');
  next.laneOrder.main = turns.map((item) => item.turnId);
  next.turnsById = Object.fromEntries(turns.map((item) => [item.turnId, item]));
  next.attemptsById = Object.fromEntries(attempts.map((item) => [item.attemptId, item]));
  return next;
}

const request = turn('request', 1, { actor: 'human', kind: 'input', parentTurnId: null });

/* The settled shape coordinator_reports.py appends: storage names the current
 * lane tail as parent and stable segments add one terminal deliverable text. */
const report = (id: string, ordinal: number, createdAt: number, parentTurnId: string,
  kind = 'progress') => turn(id, ordinal, {
  kind: `coordinator_report.${kind}`, parentTurnId, createdAt, updatedAt: createdAt,
  projection: {
    content: `${id} title\n\n${id} message`,
    origin: { blockId: 'origin', initiator: 'brain' },
    segments: [{ type: 'text', blockId: 'text:terminal', text: `${id} title\n\n${id} message`,
      deliverable: true, terminal: true }],
  } as TurnRecord['projection'],
});

const call = (id: string) => ({
  type: 'tool_use', blockId: `tool:${id}`, id, name: 'get_project_workers',
  input: '{}', result: { status: 'done', content: 'workers' },
});

/* The real result content is the JSON receipt CoordinatorReports.publish returns. */
const publish = (id: string, reportId?: string) => ({
  type: 'tool_use', blockId: `tool:${id}`, id, name: 'report_to_user',
  input: JSON.stringify({ title: 'Status', message: 'Body', kind: 'progress', idempotency_key: id }),
  ...(reportId ? { result: { status: 'done', content: JSON.stringify({
    conversationId: 'c', published: true, turnId: reportId,
  }) } } : {}),
});

const reply = (status: string, segments: unknown[], fields: Partial<TurnRecord> = {}) => turn('reply', 2, {
  parentTurnId: 'request', status, currentAttemptId: 'attempt-reply',
  projection: { content: '', segments } as TurnRecord['projection'], ...fields,
} as Partial<TurnRecord>);

const lane = (value: TurnState | ConversationViewModel) => ('mainLane' in value
  ? value : selectConversationViewModel(value, {}, {})).mainLane;
const order = (value: TurnState | ConversationViewModel) => lane(value).turns.map((item) => item.turnId);

/* Calls and reports of one Turn in reading order; other blocks are not at issue. */
function reading(value: TurnState | ConversationViewModel, turnId = 'reply'): string[] {
  const target = lane(value).turns.find((item) => item.turnId === turnId);
  return (target?.blocks ?? []).flatMap((block) => {
    if (block.kind === 'tool') return [`call:${block.toolCallId}`];
    if (block.kind === 'coordinator-report') return [`report:${block.reportTurnId}`];
    return [];
  });
}

describe('coordinator report placement', () => {
  it('reads a report inside the live reply, right after the call that published it', () => {
    const value = state([
      request,
      reply('running', [call('a'), publish('p1', 'report-1'), call('b')]),
      report('report-1', 3, 50, 'reply'),
    ], [attempt('reply')]);
    expect(order(value)).toEqual(['request', 'reply']);
    expect(reading(value)).toEqual(['call:a', 'call:p1', 'report:report-1', 'call:b']);
  });

  it('places each report after its own call when one reply publishes several', () => {
    const value = state([
      request,
      reply('running', [publish('p1', 'report-1'), call('a'), publish('p2', 'report-2'), call('b')]),
      report('report-1', 3, 50, 'reply'),
      report('report-2', 4, 60, 'report-1', 'delivery'),
    ], [attempt('reply')]);
    expect(order(value)).toEqual(['request', 'reply']);
    expect(reading(value)).toEqual([
      'call:p1', 'report:report-1', 'call:a', 'call:p2', 'report:report-2', 'call:b',
    ]);
    const blocks = lane(value).turns[1]!.blocks;
    const second = blocks.find((block) => block.kind === 'coordinator-report'
      && block.reportTurnId === 'report-2');
    expect(second).toMatchObject({ reportKind: 'delivery', title: 'report-2 title', createdAt: 60 });
  });

  it('reads the receipt from the live round before the segment checkpoint carries it', () => {
    // The task projection stores the call's receipt on its round at once; the
    // segment's own result stays `searching` until the next checkpoint.
    const live = { ...publish('p1'), result: { content: null, status: 'searching' } };
    const value = state([
      request,
      reply('running', [call('a'), live, call('b')], {
        projection: { content: '', segments: [call('a'), live, call('b')], toolRounds: [
          { toolName: 'report_to_user', toolCallId: 'p1', roundNum: 2, status: 'done',
            toolContent: JSON.stringify({ conversationId: 'c', turnId: 'report-1', published: true }) },
        ] } as unknown as TurnRecord['projection'],
      }),
      report('report-1', 3, 50, 'reply'),
    ], [attempt('reply')]);
    expect(order(value)).toEqual(['request', 'reply']);
    expect(reading(value)).toEqual(['call:a', 'call:p1', 'report:report-1', 'call:b']);
  });

  it('keeps a report whose call has not returned yet at the end of the live reply', () => {
    const value = state([
      request,
      reply('running', [call('a'), publish('p1')]),
      report('report-1', 3, 50, 'reply'),
    ], [attempt('reply')]);
    expect(order(value)).toEqual(['request', 'reply']);
    expect(reading(value)).toEqual(['call:a', 'call:p1', 'report:report-1']);
  });

  it('keeps the same reading after the reply settles', () => {
    const value = state([
      request,
      reply('completed', [call('a'), publish('p1', 'report-1'), call('b')], { updatedAt: 999 }),
      report('report-1', 3, 50, 'reply'),
    ], [attempt('reply', { status: 'completed', settledAt: 70 })]);
    expect(order(value)).toEqual(['request', 'reply']);
    expect(reading(value)).toEqual(['call:a', 'call:p1', 'report:report-1', 'call:b']);
  });

  it('follows the call that names the report even when queued work sits between them', () => {
    const queuedInput = turn('queued-input', 3, { actor: 'human', kind: 'input', parentTurnId: 'reply' });
    const queuedOutput = turn('queued-output', 4, {
      parentTurnId: 'queued-input', status: 'pending', currentAttemptId: 'attempt-queued-output',
    });
    const value = state([
      request,
      reply('running', [publish('p1', 'report-1'), call('a')]),
      queuedInput, queuedOutput,
      report('report-1', 5, 50, 'queued-output'),
    ], [attempt('reply'), attempt('queued-output', {
      status: 'pending', startedAt: null, queueBinding: { queueId: 'q', state: 'pending' },
    } as Partial<AttemptRecord>)]);
    expect(order(value)).toEqual(['request', 'reply', 'queued-input']);
    expect(reading(value)).toEqual(['call:p1', 'report:report-1', 'call:a']);
  });

  it('leaves a report published after its reply settled as its own Turn', () => {
    const value = state([
      request,
      reply('completed', [call('a')], { updatedAt: 999 }),
      report('later', 3, 80, 'reply'),
    ], [attempt('reply', { status: 'completed', settledAt: 70 })]);
    expect(order(value)).toEqual(['request', 'reply', 'later']);
    expect(reading(value)).toEqual(['call:a']);
  });

  it('never folds a report into a human Turn', () => {
    expect(order(state([request, report('after-input', 2, 15, 'request')], [])))
      .toEqual(['request', 'after-input']);
  });

  it('moves a report to its call when the call result lands as a delta', () => {
    const selector = createConversationViewModelSelector();
    const before = state([
      request,
      reply('running', [publish('p1'), call('a')]),
      report('report-1', 3, 50, 'reply'),
    ], [attempt('reply')]);
    selector.observe(before);
    expect(reading(selector.select(before))).toEqual(['call:p1', 'call:a', 'report:report-1']);

    const landed = {
      ...before,
      turnsById: {
        ...before.turnsById,
        reply: reply('running', [publish('p1', 'report-1'), call('a')], { projectionRevision: 2 }),
      },
    };
    selector.observe(landed, {
      previousState: before, action: { type: 'event' } as TurnStateUpdate['action'],
      changes: { turnIds: ['reply'], attemptIds: [], reset: false },
    });
    const view = selector.select(landed);
    expect(order(view)).toEqual(['request', 'reply']);
    expect(reading(view)).toEqual(['call:p1', 'report:report-1', 'call:a']);
  });

  it('repaints a folded report when its own Turn changes as a delta', () => {
    const selector = createConversationViewModelSelector();
    const before = state([
      request,
      reply('running', [publish('p1', 'report-1')]),
      report('report-1', 3, 50, 'reply'),
    ], [attempt('reply')]);
    selector.observe(before);
    selector.select(before);
    const original = report('report-1', 3, 50, 'reply');
    const translated = {
      ...original, projectionRevision: 2,
      projection: { ...original.projection, translatedContent: '已翻译的汇报' },
    } as TurnRecord;
    const after = { ...before, turnsById: { ...before.turnsById, 'report-1': translated } };
    selector.observe(after, {
      previousState: before, action: { type: 'event' } as TurnStateUpdate['action'],
      changes: { turnIds: ['report-1'], attemptIds: [], reset: false },
    });
    const folded = lane(selector.select(after)).turns[1]!.blocks
      .find((block) => block.kind === 'coordinator-report');
    expect(folded?.kind === 'coordinator-report' && folded.text.translatedMarkdown)
      .toBe('已翻译的汇报');
    // The folded row's title is the displayed text's, so it translates too.
    expect(folded?.kind === 'coordinator-report' && folded.title).toBe('已翻译的汇报');
    expect(folded?.source).toBe(translated);
  });
});
