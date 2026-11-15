import { describe, expect, it } from 'vitest';
import { reportEvents } from './events';
import {
  assignmentOtherRunning, assignmentReported, assignmentRunning, LEAD, memberRunning, PROJECT, reportDelivery, reporterBusy,
  reporterIdle, REPORTER, reportProgress, teamPage, tofu,
} from './testing/recorded';

const titles = new Map([[REPORTER, reporterBusy.title]]);

describe('report walk-ins come only from a real report between two reads', () => {
  it('walks the Worker in when its assignment reaches completed with a new result revision', () => {
    const before = tofu({ team: teamPage(memberRunning, reporterBusy), assignments: [assignmentOtherRunning, assignmentRunning] });
    const after = tofu({ team: teamPage(memberRunning, reporterIdle), assignments: [assignmentOtherRunning, assignmentReported] });
    expect(reportEvents(before, after, titles)).toEqual([{
      kind: 'worker-report', team: PROJECT, person: REPORTER, lead: LEAD,
      assignmentId: '3c3d9702-19a5-5fcf-ba5d-7685af881da6', resultRevision: 711, turnId: '2587198f-b0dd-40c8-bff1-64a3bbfbd3d1',
      title: 'XUECHENG-DELETE-1', summary: 'I deleted the 学城 document 「ignore」 (contentId 2798016815) and confirmed it is deleted.',
    }]);
  });

  it('never animates the first read, a re-read of the same result, or a missing read', () => {
    const reported = tofu({ assignments: [assignmentOtherRunning, assignmentReported] });
    expect(reportEvents(tofu({ assignments: null, reports: null }), reported, titles)).toEqual([]);
    expect(reportEvents(reported, reported, titles)).toEqual([]);
    expect(reportEvents(reported, tofu({ assignments: null }), titles)).toEqual([]);
  });

  it('ignores attempt and tool churn: busy flips without a completed result are not reports', () => {
    const working = tofu({ team: teamPage(memberRunning, reporterBusy) });
    const stopped = tofu({ team: teamPage(memberRunning, reporterIdle) });
    expect(reportEvents(working, stopped, titles)).toEqual([]);
    expect(reportEvents(stopped, working, titles)).toEqual([]);
  });

  it('walks the lead in when its latest owner report changes', () => {
    const before = tofu({ reports: reportProgress });
    const after = tofu({ reports: reportDelivery });
    expect(reportEvents(before, after, titles)).toEqual([{
      kind: 'lead-report', team: PROJECT, person: LEAD, turnId: reportDelivery.latest!.turnId, reportKind: 'delivery',
      title: reportDelivery.latest!.title, at: reportDelivery.latest!.createdAt,
    }]);
    expect(reportEvents(after, after, titles)).toEqual([]);
  });
});
