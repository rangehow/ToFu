/** Lead deliverables: the one item model the classic sidebar list and the studio office share.
 *
 * Items come from the server projection (GET /api/v4/project/coordination/deliverables,
 * `deliverableItem` in contracts/project_coordination_v1.schema.json; docs/PROJECT_COORDINATION.md
 * "Lead deliverables"). The studio's desk documents and trophies name the same deliverables by the
 * same identities (`report:<turn>`, `merge:<assignment>:<turn>`), so `reportDeliverable` and
 * `mergeDeliverable` build the server's item from what the office already read.
 * Pure. Entry points: appendDeliverables, reportDeliverable, mergeDeliverable, deliverableTarget,
 * deliverableProject, deliverableAge. Dependencies: the generated wire types.
 */
import type { LeadDeliverable, LeadDeliverableKind, LeadDeliverablePage, LeadDeliverableTurn } from '../../api/project-coordination.generated';

export type { LeadDeliverable, LeadDeliverableKind, LeadDeliverablePage, LeadDeliverableTurn };

export const DELIVERABLE_KINDS: ReadonlyArray<LeadDeliverableKind> = ['report', 'file', 'deck', 'document', 'merge'];

/** What opening an item shows: the artifact, the report card, or the Worker's result Turn. */
export type DeliverableTarget =
  | { kind: 'artifact'; artifactId: string }
  | { kind: 'report'; item: LeadDeliverable }
  | { kind: 'turn'; turn: LeadDeliverableTurn };

export function deliverableTarget(item: LeadDeliverable): DeliverableTarget | null {
  if (item.artifactId) return { kind: 'artifact', artifactId: item.artifactId };
  if (item.kind === 'report') return { kind: 'report', item };
  if (item.result) return { kind: 'turn', turn: item.result };
  return item.source ? { kind: 'turn', turn: item.source } : null;
}

/** The title and message as the conversation shows them: a report's display translation when it has one. */
export function deliverableCopy(item: LeadDeliverable): { title: string; summary: string } {
  return item.translation ? { title: item.translation.title, summary: item.translation.summary }
    : { title: item.title, summary: item.summary || '' };
}

/** Older pages append below; a re-read first page replaces the items it covers. Ids stay unique. */
export function appendDeliverables(current: ReadonlyArray<LeadDeliverable>, page: ReadonlyArray<LeadDeliverable>): LeadDeliverable[] {
  const seen = new Set(current.map(item => item.id));
  return [...current, ...page.filter(item => !seen.has(item.id))];
}

export function reportDeliverable(report: {
  conversationId: string; turnId: string; ordinal?: number; title: string; content: string; createdAt: number; projectPath: string;
}): LeadDeliverable {
  const [first, ...rest] = report.content.split('\n\n');
  const summary = (first.trim() === report.title.trim() ? rest.join('\n\n') : report.content).slice(0, 4000);
  const source: LeadDeliverableTurn = { conversationId: report.conversationId, turnId: report.turnId };
  if (typeof report.ordinal === 'number') source.ordinal = report.ordinal;
  return {
    id: `report:${report.turnId}`, kind: 'report', title: report.title, projectPath: report.projectPath,
    createdAt: report.createdAt, source, summary,
  };
}

export function mergeDeliverable(result: {
  assignmentId: string; title: string; conversationId: string; turnId: string; projectPath: string; createdAt: number;
}): LeadDeliverable {
  return {
    id: `merge:${result.assignmentId}:${result.turnId}`, kind: 'merge', title: result.title || result.assignmentId,
    projectPath: result.projectPath, createdAt: result.createdAt, source: null,
    result: { conversationId: result.conversationId, turnId: result.turnId },
  };
}

/** The project's folder name: the sidebar is narrow and the full path is the tooltip. */
export function deliverableProject(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

const UNITS: ReadonlyArray<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 31_536_000_000], ['month', 2_592_000_000], ['day', 86_400_000], ['hour', 3_600_000], ['minute', 60_000],
];

export function deliverableAge(createdAt: number, now: number, locale?: string): string {
  if (!createdAt) return '';
  const difference = createdAt - now;
  const [unit, divisor] = UNITS.find(([, size]) => Math.abs(difference) >= size) || UNITS[UNITS.length - 1];
  return new Intl.RelativeTimeFormat(locale || undefined, { numeric: 'auto' }).format(Math.trunc(difference / divisor), unit);
}
