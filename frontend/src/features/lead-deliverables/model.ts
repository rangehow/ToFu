/** Lead deliverables: the one item model the conversation's right-hand panel and the studio office share.
 *
 * Items come from the server projection (GET /api/v4/project/coordination/deliverables,
 * `deliverableItem` in contracts/project_coordination_v1.schema.json; docs/PROJECT_COORDINATION.md
 * "Lead deliverables"). The studio's desk documents and trophies name the same deliverables by the
 * same identities (`report:<turn>`, `merge:<assignment>:<turn>`), so `reportDeliverable` and
 * `mergeDeliverable` build the server's item from what the office already read.
 * Pure. Entry points: appendDeliverables, reportDeliverable, mergeDeliverable, deliverableTarget,
 * deliverableProject, deliverableAge, deliverableVisual, deliverableDays, deliverableDayLabel,
 * deliverablePreview. Dependencies: the generated wire types.
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

/** The project's folder name: the panel is narrow and the full path is the tooltip. */
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

/** What a row looks like: a report by its own kind, a file by its format. */
export type DeliverableVisual =
  | 'delivery' | 'progress' | 'decision' | 'blocked' | 'report'
  | 'deck' | 'pdf' | 'sheet' | 'image' | 'document' | 'file' | 'merge';

const SHEET_FORMATS = new Set(['xlsx', 'xls', 'csv', 'tsv']);
const IMAGE_FORMATS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']);

export function deliverableVisual(item: LeadDeliverable): DeliverableVisual {
  if (item.kind === 'report') return item.reportKind || 'report';
  if (item.kind !== 'file') return item.kind;
  const format = (item.format || /\.([a-z0-9]+)$/i.exec(item.title)?.[1] || '').toLowerCase();
  if (format === 'pdf') return 'pdf';
  if (SHEET_FORMATS.has(format)) return 'sheet';
  if (IMAGE_FORMATS.has(format)) return 'image';
  return 'file';
}

export interface DeliverableDay { key: string; start: number; items: LeadDeliverable[] }

function dayStart(time: number): number {
  const day = new Date(time);
  day.setHours(0, 0, 0, 0);
  return day.getTime();
}

/** Consecutive items by local calendar day, in list order (newest first). */
export function deliverableDays(items: ReadonlyArray<LeadDeliverable>): DeliverableDay[] {
  const days: DeliverableDay[] = [];
  for (const item of items) {
    const start = item.createdAt ? dayStart(item.createdAt) : 0;
    const key = String(start);
    const last = days[days.length - 1];
    if (last && last.key === key) last.items.push(item);
    else days.push({ key, start, items: [item] });
  }
  return days;
}

/** "Today", "Yesterday", then the weekday and date (with the year once it differs). */
export function deliverableDayLabel(start: number, now: number, locale?: string): string {
  if (!start) return '';
  const days = Math.round((dayStart(now) - start) / 86_400_000);
  if (days >= 0 && days < 2) {
    const word = new Intl.RelativeTimeFormat(locale || undefined, { numeric: 'auto' }).format(-days, 'day');
    return word.charAt(0).toLocaleUpperCase(locale || undefined) + word.slice(1);
  }
  const sameYear = new Date(start).getFullYear() === new Date(now).getFullYear();
  return new Intl.DateTimeFormat(locale || undefined, {
    weekday: 'short', month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
  }).format(start);
}

/** The hover preview: the whole (displayed) title, the exact time and the report's opening paragraph as plain text. */
export function deliverablePreview(item: LeadDeliverable, locale?: string): string {
  const copy = deliverableCopy(item);
  // A translated title keeps the original as written right below it.
  const lines = copy.title === item.title ? [item.title] : [copy.title, item.title];
  if (item.createdAt) lines.push(new Date(item.createdAt).toLocaleString(locale || undefined));
  const opening = copy.summary.trim().split(/\n\s*\n/)[0]
    .replace(/^#+\s*/gm, '').replace(/^\s*[-*]\s+/gm, '• ').replace(/(\*\*|__|`)/g, '').trim();
  if (opening) lines.push('', opening);
  if (item.files?.length) lines.push('', ...item.files.map(file => `↗ ${file.filename}`));
  return lines.join('\n');
}
