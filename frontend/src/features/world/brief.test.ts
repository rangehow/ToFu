import { describe, expect, it } from 'vitest';
import { openQuestions } from './attention';
import { projectBrief, taskParagraph } from './brief';
import { projectOffice, type PersonLabels } from './model';
import {
  askTurn, assignmentOtherRunning, assignmentReported, LEAD, memberRunning, REPORTER, reporterIdle, reportProgress, RUNNER, teamPage, tofu,
} from './testing/recorded';

const labels: PersonLabels = { router: 'Global Router', watchdog: 'Watchdog', lead: team => `${team} lead` };
const NOW = 1794533400000;

describe('a person panel leads with what they did, the evidence and the decisions', () => {
  it("leads a reported Worker with its delivered result, the lead's verdicts and the delivered checkpoint", () => {
    const input = tofu({ team: teamPage(memberRunning, reporterIdle), assignments: [assignmentOtherRunning, assignmentReported], held: new Set([REPORTER]) });
    const person = projectOffice(NOW, [input], labels).people.get(REPORTER)!;
    const brief = projectBrief(person, input, NOW, { source: 'worker-result', conversationId: REPORTER,
      assignmentId: assignmentReported.assignmentId, resultRevision: 711, turnId: '2587198f-b0dd-40c8-bff1-64a3bbfbd3d1' });
    expect(brief.lead).toEqual({ kind: 'result', status: 'completed', turn: { turnId: '2587198f-b0dd-40c8-bff1-64a3bbfbd3d1' },
      markdown: 'I deleted the 学城 document 「ignore」 (contentId 2798016815) and confirmed it is deleted.' });
    expect(brief.checks.map(check => check.verdict)).toEqual(['met', 'met', 'met']);
    expect(brief.checks[1].evidence).toContain('delete_doc');
    expect(brief.review).toEqual({ decision: 'accept', note: 'Accepted: the owner-authorized deletion of exactly one 学城 document.' });
    expect(brief.delivery).toEqual({ checkpoint: '47aa9b62c3657c521030a5a5e6be56bdbfebe385', status: 'delivered', reason: '' });
    expect(brief.decisions).toEqual([]);
  });

  it('leads a working Worker with its task, skipping the context preamble', () => {
    const input = tofu();
    const person = projectOffice(NOW, [input], labels).people.get(RUNNER)!;
    const brief = projectBrief(person, input, NOW);
    expect(brief.lead).toMatchObject({ kind: 'task', markdown: 'Remove the per-project repo lock bottleneck.' });
    expect(brief.checks[0].verdict).toBe('unreviewed');
    expect(taskParagraph('Only line')).toBe('Only line');
  });

  it("leads a lead with the owner's open decisions, overdue commitments, latest report and board", () => {
    const input = tofu({ reports: reportProgress, questions: openQuestions(LEAD, [askTurn]) });
    const person = projectOffice(NOW, [input], labels).people.get(LEAD)!;
    const brief = projectBrief(person, input, NOW);
    expect(brief.decisions).toEqual([{ guidanceId: openQuestions(LEAD, [askTurn])[0].guidanceId, conversationId: LEAD, project: input.name,
      question: '**Only you** can approve the Paper A run: keep 8 jobs or cut to 4?',
      blocking: true, fallback: '', turn: { turnId: askTurn.turnId, ordinal: 530 }, at: askTurn.createdAt }]);
    expect(brief.overdue.map(item => item.slips)).toEqual([15]);
    expect(brief.lead).toMatchObject({ kind: 'report', reportKind: 'progress', unread: true,
      turn: { turnId: reportProgress.latest!.turnId, ordinal: 528 } });
    expect(brief.board).toMatchObject({ done: 1, total: 3 });
    expect(brief.board!.tasks.map(task => task.status)).toEqual(['in_progress', 'pending']);
  });
});
