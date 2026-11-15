/** Office events from backend truth. A report walk-in happens only when a report really arrived between
 * two successful reads of the same source in this session; the first read of a source is the baseline and
 * never animates:
 * - worker-report: an assignment of this lead's Worker reached `completed` with a result revision it did
 *   not have before (`execution.resultRevision`, the result the Worker delivered to its lead);
 * - lead-report: the lead's latest owner report (`reports.latest.turnId`) changed.
 * Pure. Entry point: reportEvents. */
import type { ProjectDispatchHistoryRow } from '../../api/project-dispatch.generated';
import { firstLine, newestAssignments, personName, type ReportKind, type TeamInput } from './model';

export type OfficeEvent =
  | { kind: 'worker-report'; team: string; person: string; lead: string; assignmentId: string; resultRevision: number;
    turnId: string; title: string; summary: string }
  | { kind: 'lead-report'; team: string; person: string; turnId: string; reportKind: ReportKind; title: string; at: number };

function completed(row: ProjectDispatchHistoryRow | undefined): row is ProjectDispatchHistoryRow & {
  execution: NonNullable<ProjectDispatchHistoryRow['execution']> & { resultRevision: number; turnId: string };
} {
  return row?.execution?.status === 'completed' && typeof row.execution.resultRevision === 'number' && Boolean(row.execution.turnId);
}

function resultText(row: ProjectDispatchHistoryRow): string {
  const content = row.execution?.result?.content;
  return typeof content === 'string' ? content : '';
}

export function reportEvents(previous: TeamInput, next: TeamInput, titles: ReadonlyMap<string, string>): OfficeEvent[] {
  const events: OfficeEvent[] = [];
  if (previous.assignments && next.assignments) {
    const before = new Map(previous.assignments.map(row => [row.assignmentId, row]));
    for (const [recipient, row] of newestAssignments(next.leadId, next.assignments)) {
      if (!completed(row)) continue;
      const earlier = before.get(row.assignmentId);
      if (earlier && completed(earlier) && earlier.execution.resultRevision >= row.execution.resultRevision) continue;
      events.push({
        kind: 'worker-report', team: next.key, person: recipient, lead: next.leadId, assignmentId: row.assignmentId,
        resultRevision: row.execution.resultRevision, turnId: row.execution.turnId,
        title: personName(titles.get(recipient) || firstLine(row.request.brief)).name, summary: firstLine(resultText(row)),
      });
    }
  }
  const latest = next.reports?.latest;
  if (previous.reports && latest && previous.reports.latest?.turnId !== latest.turnId) {
    events.push({
      kind: 'lead-report', team: next.key, person: next.leadId, turnId: latest.turnId, reportKind: latest.kind,
      title: latest.title || firstLine(latest.content), at: latest.createdAt,
    });
  }
  return events;
}
