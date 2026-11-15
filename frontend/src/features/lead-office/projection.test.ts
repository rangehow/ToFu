import { describe, expect, it } from 'vitest';
import type { ProjectDispatchHistoryRow, ProjectDispatchPage } from '../../api/project-dispatch.generated';
import type { CoordinatorProgressBoard, CoordinatorReportState } from '../../api/project-coordination.generated';
import { briefTitle, deskStateOf, projectLeadOffice, type LiveStatus, type OfficeInput, type OfficeProjectFacts } from './projection';

const LEAD = 'coordinator-lead';

function row(id: string, options: {
  status?: string | null; recipient?: string; brief?: string; coordinator?: string; mode?: 'consult' | 'files' | 'project' | 'coordinator';
  review?: ProjectDispatchHistoryRow['review']; revoked?: boolean; resultRevision?: number | null;
} = {}): ProjectDispatchHistoryRow {
  const status = options.status === undefined ? 'running' : options.status;
  return {
    assignmentId: id,
    request: { projectPath: '/repo', coordinatorId: options.coordinator ?? LEAD, recipientId: options.recipient ?? `worker-${id}`,
      brief: options.brief ?? `Task ${id}\nMore detail`, profile: { mode: options.mode ?? 'project', tools: [], readPaths: [], writePaths: [] } },
    grantRevision: 1, revoked: options.revoked ?? false, canonicalDelivery: false,
    execution: status === null ? null : { turnId: `turn-${id}`, attemptId: `attempt-${id}`, taskId: `task-${id}`, status,
      resultRevision: options.resultRevision === undefined ? 1 : options.resultRevision, result: {} },
    review: options.review ?? null,
  };
}
const page = (rows: ProjectDispatchHistoryRow[], olderCursor: string | null = null): ProjectDispatchPage =>
  ({ assignments: rows, olderCursor, newerCursor: null });

function input(overrides: Partial<OfficeInput> = {}): OfficeInput {
  return {
    altitude: 'floor', leadId: LEAD, lead: { streaming: false, awaitingHuman: false },
    board: null, report: undefined, assignments: page([]), live: () => null, current: '', ...overrides,
  };
}

describe('deskStateOf', () => {
  const none: LiveStatus = null;
  it.each([
    ['running', 'working'], ['queued', 'queued'], ['pending', 'queued'], ['blocked', 'blocked'],
    ['completed', 'review'], ['failed', 'failed'], ['cancelled', null], ['aborted', null], ['mystery', null],
  ] as const)('maps execution %s to %s', (status, expected) => {
    expect(deskStateOf(row('a', { status }), none)).toBe(expected);
  });

  it('lets a reviewed result or failure leave the office', () => {
    const review = { decision: 'accept' as const, note: '', resultRevision: 2 };
    expect(deskStateOf(row('a', { status: 'completed', resultRevision: 2, review }), none)).toBeNull();
    expect(deskStateOf(row('a', { status: 'failed', resultRevision: null, review: { ...review, decision: 'reject' } }), none)).toBeNull();
    // A newer result than the one reviewed is waiting again.
    expect(deskStateOf(row('a', { status: 'completed', resultRevision: 3, review }), none)).toBe('review');
  });

  it('keeps a running attempt even after its grant was revoked, but drops revoked finished work', () => {
    expect(deskStateOf(row('a', { status: 'running', revoked: true }), none)).toBe('working');
    expect(deskStateOf(row('a', { status: 'completed', revoked: true }), none)).toBeNull();
  });

  it('upgrades a desk to working only from live evidence', () => {
    const reviewed = row('a', { status: 'completed', review: { decision: 'accept', note: '', resultRevision: 1 } });
    expect(deskStateOf(reviewed, 'running')).toBe('working');
    expect(deskStateOf(row('b', { status: 'queued' }), 'done')).toBe('queued');
  });
});

describe('projectLeadOffice', () => {
  it('projects one desk per Worker that needs its lead, oldest first', () => {
    const view = projectLeadOffice(input({ assignments: page([
      row('new', { status: 'queued' }),
      row('repeat-new', { recipient: 'worker-shared', status: 'running' }),
      row('repeat-old', { recipient: 'worker-shared', status: 'completed' }),
      row('done', { status: 'completed', review: { decision: 'accept', note: '', resultRevision: 1 } }),
      row('foreign', { coordinator: 'coordinator-other' }),
      row('forward', { mode: 'coordinator' }),
      row('self', { recipient: LEAD }),
      row('old', { status: 'completed' }),
    ]) }));
    expect(view.desks.map(desk => [desk.id, desk.conversationId, desk.state])).toEqual([
      ['old', 'worker-old', 'review'],
      ['repeat-new', 'worker-shared', 'working'],
      ['new', 'worker-new', 'queued'],
    ]);
    expect(view.coverage).toBe('complete');
  });

  it('names a desk by the first line of its brief and dresses it by that wording', () => {
    const view = projectLeadOffice(input({ assignments: page([
      row('a', { brief: '\n  [image ref: /api/images/x.png] Research the parser options  \nDetails' }),
      row('b', { brief: 'Write code for the parser' }),
    ]) }));
    expect(view.desks.map(desk => [desk.title, desk.model])).toEqual([
      ['Write code for the parser', 'engineer'],
      ['Research the parser options', 'researcher'],
    ]);
  });

  it('names a desk by its Worker conversation\'s own title, else by the brief without its dated request label', () => {
    const view = projectLeadOffice(input({
      assignments: page([
        row('a', { brief: 'Owner request (2026-11-15): "我们的工作室界面也太粗糙了"\nDo: …' }),
        row('b', { brief: 'Owner bug (2026-11-15): the extension rejects a URL with a hash route\nDetails' }),
        row('c', { brief: 'Owner request (2026-11-15): "Make the parser fast"' }),
      ]),
      title: id => id === 'worker-c' ? 'Parser speed: profile and fix the hot loop' : '',
    }));
    expect(view.desks.map(desk => desk.title)).toEqual([
      'Parser speed: profile and fix the hot loop',
      'the extension rejects a URL with a hash route',
      '我们的工作室界面也太粗糙了',
    ]);
    expect(briefTitle('Owner request (2026-11-15): ', 'fallback')).toBe('fallback');
  });

  it('upgrades a desk from the live catalog without a new read', () => {
    const rows = page([row('a', { status: 'queued' })]);
    expect(projectLeadOffice(input({ assignments: rows })).desks[0].state).toBe('queued');
    expect(projectLeadOffice(input({ assignments: rows, live: id => id === 'worker-a' ? 'running' : null })).desks[0].state).toBe('working');
  });

  it('adds a running worker chat without an assignment on the page as a working desk, once', () => {
    const view = projectLeadOffice(input({
      assignments: page([row('a', { status: 'completed' })]),
      running: [{ conversationId: 'worker-a', title: 'Already a desk' }, { conversationId: 'chat-b', title: 'Write the parser docs' },
        { conversationId: LEAD, title: 'The lead itself' }],
    }));
    expect(view.desks.map(desk => [desk.id, desk.conversationId, desk.state, desk.title, desk.model])).toEqual([
      ['a', 'worker-a', 'review', 'Task a', 'general'],
      ['conversation:chat-b', 'chat-b', 'working', 'Write the parser docs', 'writer'],
    ]);
  });

  it('derives the lead pose with asking ahead of replying', () => {
    expect(projectLeadOffice(input()).lead.state).toBe('idle');
    expect(projectLeadOffice(input({ lead: { streaming: true, awaitingHuman: false } })).lead.state).toBe('replying');
    expect(projectLeadOffice(input({ lead: { streaming: true, awaitingHuman: true } })).lead.state).toBe('asking');
  });

  it('shows only this lead\'s unread report and its board tally', () => {
    const report = (conversationId: string, unread: boolean): CoordinatorReportState => ({ projectPath: '/repo', conversationId,
      bindingRevision: 1, unread, latest: { turnId: 'turn-r', ordinal: 3, title: 'Parser shipped', kind: 'delivery', createdAt: 1, content: '' } });
    expect(projectLeadOffice(input({ report: report(LEAD, true) })).lead.report).toBe('Parser shipped');
    expect(projectLeadOffice(input({ report: report(LEAD, false) })).lead.report).toBe('');
    expect(projectLeadOffice(input({ report: report('coordinator-old', true) })).lead.report).toBe('');
    const board: CoordinatorProgressBoard = { revision: 4, updatedAt: 1, summary: '', tasks: [
      { title: 'a', status: 'completed' }, { title: 'b', status: 'in_progress' }, { title: 'c', status: 'pending' }, { title: 'd', status: 'cancelled' },
    ] };
    expect(projectLeadOffice(input({ board })).lead.board).toEqual({ done: 2, total: 4 });
    expect(projectLeadOffice(input({ board: { ...board, tasks: [] } })).lead.board).toBeNull();
  });

  it('passes the open conversation through for the room it frames', () => {
    expect(projectLeadOffice(input({ current: 'worker-a' })).current).toBe('worker-a');
  });

  it('reports coverage of the assignment read', () => {
    expect(projectLeadOffice(input({ assignments: undefined })).coverage).toBe('loading');
    expect(projectLeadOffice(input({ assignments: null })).coverage).toBe('unavailable');
    expect(projectLeadOffice(input({ assignments: page([], 'older') })).coverage).toBe('complete');
  });

  it('adds one room per project at the headquarters, in the sidebar\'s folder order', () => {
    const facts: OfficeProjectFacts[] = [
      { projectPath: '/work/zeta/', coordinatorConversationId: 'coordinator-z', availability: 'available', activeWorkers: 2, openQuestions: 1,
        lastReport: { title: 'Zeta report', unread: true } },
      { projectPath: '/work/alpha', coordinatorConversationId: 'coordinator-a', availability: 'available', activeWorkers: 3, overdueCommitments: 1 },
      { projectPath: '/work/loose', coordinatorConversationId: '', availability: 'unbound' },
      { projectPath: '/work/broken', coordinatorConversationId: 'coordinator-b', availability: 'available', activityError: 'storage' },
    ];
    const folders = [{ name: 'Zeta', color: '#336699', paths: ['/work/zeta'] }, { name: 'Alpha', paths: ['/work/alpha', '/work/alpha-docs'] }];
    const view = projectLeadOffice(input({ altitude: 'headquarters', projects: facts, folders }));
    expect(view.projects.map(project => [project.name, project.coordinatorId, project.workers, project.questions, project.report, project.color])).toEqual([
      ['Zeta', 'coordinator-z', 2, 1, 'Zeta report', '#336699'],
      ['Alpha', 'coordinator-a', 3, 0, '', ''],
      ['broken', 'coordinator-b', null, 0, '', ''],
      ['loose', '', null, 0, '', ''],
    ]);
    expect(view.projectsCoverage).toBe('complete');
    expect(projectLeadOffice(input({ altitude: 'headquarters', projects: undefined })).projectsCoverage).toBe('loading');
    expect(projectLeadOffice(input({ altitude: 'headquarters', projects: null })).projectsCoverage).toBe('unavailable');
    // The floor never shows project rooms, whatever it is handed.
    expect(projectLeadOffice(input({ projects: facts, folders })).projects).toEqual([]);
  });
});
