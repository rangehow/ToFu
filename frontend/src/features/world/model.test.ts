import { describe, expect, it } from 'vitest';
import { openQuestions } from './attention';
import { deskDocuments, personName, projectOffice, trophiesOf, type PersonLabels } from './model';
import {
  askTurn, assignmentOtherRunning, assignmentReported, assignmentRunning, LEAD, memberRunning, PROJECT, reportDelivery,
  reporterBusy, reporterIdle, reportProgress, REPORTER, RUNNER, teamPage, tofu, toolChurnTurn,
} from './testing/recorded';

const labels: PersonLabels = { router: 'Global Router', watchdog: 'Watchdog', lead: team => `${team} lead` };
const NOW = 1794533400000;

describe('office projection on recorded live shapes', () => {
  it('seats a Worker only while its attempt runs or its assignment waits, and counts everyone else as routine', () => {
    const office = projectOffice(NOW, [tofu({
      team: teamPage(memberRunning, reporterIdle, { ...reporterIdle, conversationId: 'worker-idle-2', title: '[OLD-1] Done' }),
      assignments: [assignmentOtherRunning, assignmentReported],
    })], labels);
    const team = office.teams[0];
    expect(team.workers.map(worker => worker.id)).toEqual([RUNNER]);
    expect(team.workers[0].state).toBe('working');
    expect(team.routine).toBe(2);
    expect(team.history).toBe(true);
  });

  it("never seats the owner's own chats in the project folder: only this lead's Workers are staff", () => {
    // Recorded 11-13: the owner's running chat in the tofu folder has no coordinator.
    const ownerChat = { ...memberRunning, conversationId: 'mvspd8z2ijozkh', coordinatorId: null,
      title: '今天Gemini 4 carbon又出了，每次新出模型我都得手动和你说让你去添加' };
    const office = projectOffice(NOW, [tofu({ team: teamPage(memberRunning, ownerChat), assignments: [assignmentOtherRunning] })], labels);
    expect(office.people.has('mvspd8z2ijozkh')).toBe(false);
    expect(office.teams[0].routine).toBe(0);
    expect(office.people.has(RUNNER)).toBe(true);
  });

  it('keeps a Worker whose attempt just ended at its desk until the assignment read that may hold its report lands', () => {
    const input = tofu({ team: teamPage(memberRunning, reporterIdle), assignments: [assignmentOtherRunning, assignmentRunning] });
    const released = projectOffice(NOW, [{ ...input, assignments: [assignmentOtherRunning, assignmentReported] }], labels);
    expect(released.people.has(REPORTER)).toBe(false);
    const held = projectOffice(NOW, [{ ...input, assignments: [assignmentOtherRunning, assignmentReported], held: new Set([REPORTER]) }], labels);
    expect(held.people.get(REPORTER)?.state).toBe('idle');
  });

  it('takes who is busy from the office busy facts when present, over a stale team row', () => {
    // The team row still says the REPORTER runs; the busy facts (the attempt registry) say only the RUNNER does.
    const office = projectOffice(NOW, [tofu({
      team: teamPage(memberRunning, reporterBusy), assignments: [assignmentOtherRunning, assignmentReported], busy: new Set([RUNNER]),
    })], labels);
    expect(office.teams[0].workers.map(worker => [worker.id, worker.state])).toEqual([[RUNNER, 'working']]);
  });

  it('flags the Watchdog only for an owner item: its open signals are its own queue', () => {
    // 2026-11-18: 15 open quota signals kept the Watchdog at "needs attention" while its brief listed nothing.
    const watchdog = (questions: ReturnType<typeof openQuestions>) => tofu({
      kind: 'watchdog', key: '/watchdog', leadId: 'watchdog-conversation', name: '', team: teamPage(),
      assignments: null, progress: null, reports: null, questions,
    });
    const quiet = projectOffice(NOW, [watchdog([])], labels);
    expect(quiet.people.get('watchdog-conversation')?.emphasis).toBeNull();
    expect(quiet.attention.counts).toMatchObject({ decisions: 0, exceptions: 0 });
    const asking = projectOffice(NOW, [watchdog([{ conversationId: 'watchdog-conversation', turnId: 't', ordinal: 1,
      guidanceId: 'hg_fault', blocking: true, question: 'Restart the server?', defaultText: '', createdAt: 1 }])], labels);
    expect(asking.people.get('watchdog-conversation')?.emphasis).toBe('decision');
  });

  it('is busy exactly when the team row says an attempt runs, waiting when assigned but not running', () => {
    const office = projectOffice(NOW, [tofu({ team: teamPage(reporterIdle), assignments: [assignmentRunning] })], labels);
    expect(office.people.get(REPORTER)?.state).toBe('waiting');
    const running = projectOffice(NOW, [tofu({ team: teamPage(reporterBusy), assignments: [assignmentRunning] })], labels);
    expect(running.people.get(REPORTER)?.state).toBe('working');
  });

  it('names people by recognizable roles and task tags, never by conversation id', () => {
    const office = projectOffice(NOW, [
      tofu(),
      { ...tofu(), key: '/router', kind: 'router', name: '', leadId: 'coordinator-router', team: teamPage(), assignments: null },
      { ...tofu(), key: '/watchdog', kind: 'watchdog', name: '', leadId: 'coordinator-watchdog', team: teamPage(), assignments: null },
    ], labels);
    const names = [...office.people.values()].map(person => person.name);
    expect(names).toEqual(expect.arrayContaining(['tofu lead', 'Global Router', 'Watchdog', 'SCALE-REPO-2', 'XUECHENG-DELETE-1']));
    for (const name of names) expect(name).not.toMatch(/worker-|coordinator-|[0-9a-f]{8}-[0-9a-f]{4}/);
    expect(personName('Plain title without a tag')).toEqual({ name: 'Plain title without a tag', detail: '' });
  });

  it('marks a decision only for a real open owner question, never for tool activity', () => {
    const quiet = projectOffice(NOW, [tofu({ questions: openQuestions(LEAD, [toolChurnTurn]) })], labels);
    expect(quiet.people.get(LEAD)?.emphasis).toBe('exception');
    expect(quiet.attention.decisions).toHaveLength(0);
    const asking = projectOffice(NOW, [tofu({ questions: openQuestions(LEAD, [askTurn, toolChurnTurn]) })], labels);
    expect(asking.people.get(LEAD)?.emphasis).toBe('decision');
    expect(asking.attention.decisions.map(item => item.source.guidanceId)).toEqual(['hg_069759980950']);
  });

  it('does not flag settled cards or ordinary progress as unresolved owner action', () => {
    const settled = { ...askTurn, projection: { toolRounds: [
      { toolName: 'ask_human', guidanceId: 'settled', guidanceQuestion: 'Keep queued?',
        guidanceBlocking: false, status: 'done', guidanceResponse: 'Keep queued' },
      { toolName: 'ask_human', guidanceId: 'replaced', guidanceQuestion: 'Old question?',
        guidanceBlocking: false, status: 'done', guidanceSuperseded: true },
    ] } };
    const office = projectOffice(NOW, [tofu({ questions: openQuestions(LEAD, [settled, toolChurnTurn]),
      progress: { ...tofu().progress!, commitments: [] }, reports: reportProgress })], labels);
    expect(office.people.get(LEAD)?.emphasis).toBeNull();
    expect(office.attention.counts).toEqual({ decisions: 0, exceptions: 0, deliverables: 0, digests: 0 });
    expect(office.documents[0].kind).toBe('report');
  });

  it('marks an exception for an overdue commitment and nothing for a lead with none', () => {
    const calm = projectOffice(NOW, [tofu({ progress: { ...tofu().progress!, commitments: [] } })], labels);
    expect(calm.people.get(LEAD)?.emphasis).toBeNull();
    expect([...calm.people.values()].every(person => person.emphasis === null)).toBe(true);
  });

  it('puts a trophy on the shelf only for an accepted, canonically delivered result with its Turn', () => {
    const office = projectOffice(NOW, [tofu({ assignments: [assignmentOtherRunning, assignmentReported] })], labels);
    expect(office.trophies).toEqual([{
      id: `trophy:${assignmentReported.assignmentId}`, team: PROJECT, author: REPORTER, title: 'XUECHENG-DELETE-1',
      ref: { source: 'worker-result', conversationId: REPORTER, assignmentId: assignmentReported.assignmentId,
        resultRevision: 711, turnId: '2587198f-b0dd-40c8-bff1-64a3bbfbd3d1' },
    }]);
    const rework = { ...assignmentReported, review: { ...assignmentReported.review!, decision: 'rework' as const } };
    const unpublished = { ...assignmentReported, canonicalDelivery: false };
    expect(projectOffice(NOW, [tofu({ assignments: [rework, unpublished, assignmentRunning] })], labels).trophies).toEqual([]);
  });

  it('interleaves trophies across teams, newest first within each team', () => {
    const second = { ...assignmentReported, assignmentId: 'second' };
    const otherRow = { ...assignmentReported, assignmentId: 'other-1',
      request: { ...assignmentReported.request, coordinatorId: 'coordinator-other' } };
    const other = { ...tofu(), key: '/other', leadId: 'coordinator-other', assignments: [otherRow] };
    expect(trophiesOf([tofu({ assignments: [assignmentReported, second] }), other]).map(trophy => trophy.id))
      .toEqual([`trophy:${assignmentReported.assignmentId}`, 'trophy:other-1', 'trophy:second']);
  });

  it("hangs the Global Router's board on the owner's whiteboard, one note per live task", () => {
    const router = { ...tofu(), key: '/router', kind: 'router' as const, name: '', leadId: 'coordinator-router' };
    const office = projectOffice(NOW, [tofu(), router], labels);
    expect(office.whiteboard?.owner).toBe('coordinator-router');
    expect(office.whiteboard?.board.tasks.map(task => task.status)).toEqual(['in_progress', 'completed', 'pending']);
    expect(projectOffice(NOW, [tofu()], labels).whiteboard).toBeNull();
  });

  it('puts a document on the owner desk only for an unread lead report, a deliverable only for a delivery', () => {
    expect(deskDocuments([tofu()])).toEqual([]);
    const [report] = deskDocuments([tofu({ reports: reportProgress })]);
    expect(report).toMatchObject({ kind: 'report', author: LEAD, team: PROJECT,
      ref: { source: 'lead-report', conversationId: LEAD, turnId: reportProgress.latest!.turnId, reportKind: 'progress' } });
    const [delivery] = deskDocuments([tofu({ reports: reportDelivery })]);
    expect(delivery.kind).toBe('deliverable');
  });
});
