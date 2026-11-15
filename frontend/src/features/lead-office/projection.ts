/** Lead office projection: one pure function from confirmed lead facts to
 * everything the office shows. Rooms, poses and marks are projected facts,
 * never decoration. A Worker desk exists while its assignment needs its lead:
 * queued, blocked, running, a result awaiting review, or a failure not yet
 * reviewed. Reviewed, cancelled, revoked and evidence-less history leaves the
 * office (the lead's sidebar archive keeps every conversation it started). A
 * live conversation flag only upgrades a desk to working; the assignment page
 * stays the authority for every other state. A worker chat this lead started
 * that the catalog shows running without an assignment on the page
 * (create_conversation, or older than the page) is a working desk as well, so
 * the office never says less than the sidebar's running count. The
 * headquarters altitude adds one floor per project from the owner's project
 * summary read. The open conversation is the room the office frames.
 * Entry points: projectLeadOffice, deskStateOf.
 */
import type { ProjectDispatchHistoryRow, ProjectDispatchPage } from '../../api/project-dispatch.generated';
import type { CoordinatorProgressBoard, CoordinatorReportState } from '../../api/project-coordination.generated';
import { assignmentStatus, cleanTitle } from '../project-coordination-graph';
import { coordinatorProgressTally } from '../coordinator-progress';
import { modelForRole, type CharacterModel } from '../office-game/office-character';

export type OfficeAltitude = 'floor' | 'headquarters';
export type DeskState = 'working' | 'queued' | 'blocked' | 'review' | 'failed';
export type LeadState = 'replying' | 'asking' | 'idle';
/** A Worker conversation's live run state from the loaded catalog (null when unknown). */
export type LiveStatus = 'running' | 'done' | 'failed' | 'cancelled' | null;

export interface OfficeDesk {
  /** Assignment identity, or `conversation:<id>` for a running chat without one. */
  id: string;
  conversationId: string;
  title: string;
  state: DeskState;
  model: CharacterModel;
}
export interface OfficeProject {
  path: string;
  name: string;
  color: string;
  /** '' when the project has no available coordinator. */
  coordinatorId: string;
  /** Active Workers, or null when the project's activity could not be read. */
  workers: number | null;
  questions: number;
  overdue: number;
  /** The title of the project's unread report, '' when none. */
  report: string;
}
export interface OfficeLead {
  conversationId: string;
  state: LeadState;
  /** The title of the latest report while it is unread, '' otherwise. */
  report: string;
  board: { done: number; total: number } | null;
}
export interface OfficeView {
  altitude: OfficeAltitude;
  lead: OfficeLead;
  desks: OfficeDesk[];
  projects: OfficeProject[];
  /** The conversation open in the main view ('' when none). */
  current: string;
  /** Desks: loading before the first assignment read, unavailable after a failed one. */
  coverage: 'loading' | 'complete' | 'unavailable';
  /** Project floors at the headquarters altitude, with the same meanings. */
  projectsCoverage: 'loading' | 'complete' | 'unavailable';
}
/** The owner's project summary fields the office reads (GET /api/v4/router/projects). */
export interface OfficeProjectFacts {
  projectPath: string;
  coordinatorConversationId: string;
  availability: 'unbound' | 'available' | 'unavailable';
  activeWorkers?: number;
  openQuestions?: number;
  overdueCommitments?: number;
  lastReport?: { title: string; unread: boolean } | null;
  activityError?: string;
}
export interface OfficeFolder { name: string; color?: string; paths: readonly string[]; }
export interface OfficeInput {
  altitude: OfficeAltitude;
  leadId: string;
  lead: { streaming: boolean; awaitingHuman: boolean };
  board: CoordinatorProgressBoard | null;
  report: CoordinatorReportState | undefined;
  /** undefined before the first read; null when the read failed. */
  assignments: ProjectDispatchPage | null | undefined;
  live(conversationId: string): LiveStatus;
  /** Worker chats this lead started that the loaded catalog shows running. */
  running?: readonly { conversationId: string; title: string }[];
  /** A conversation's own title from the loaded catalog ('' when unknown or untitled). */
  title?(conversationId: string): string;
  /** Headquarters only: undefined before the first read; null when it failed. */
  projects?: readonly OfficeProjectFacts[] | null;
  folders?: readonly OfficeFolder[];
  current: string;
}

const norm = (path: string) => path.replace(/[/\\]+$/, '');
const basename = (path: string) => norm(path).split(/[/\\]/).pop() || path;

/** Desk state of one assignment row, or null when it no longer needs its lead. */
export function deskStateOf(row: ProjectDispatchHistoryRow, live: LiveStatus): DeskState | null {
  if (live === 'running') return 'working';
  const status = assignmentStatus(row);
  if (status === 'running') return 'working';
  if (row.revoked) return null;
  const resultRevision = row.execution?.resultRevision;
  const reviewed = !!row.review && (resultRevision == null || row.review.resultRevision >= resultRevision);
  switch (status) {
    case 'queued': case 'waiting': return 'queued';
    case 'blocked': return 'blocked';
    case 'completed': return reviewed ? null : 'review';
    case 'failed': return reviewed ? null : 'failed';
    default: return null;
  }
}

/** A dated request label a lead writes ahead of the quoted request ("Owner request (2026-11-15): \"…"). */
const REQUEST_LABEL = /^(?:[\p{L}]+\s+){0,2}\(\d{4}-\d{2}-\d{2}\)\s*[:：]\s*/u;
const QUOTES = /^["“”'‘’「『]+|["“”'‘’」』]+$/gu;
/** A brief's first line without its request label and quotes. */
export function briefTitle(brief: string, fallback: string): string {
  const line = brief.split('\n').map(item => item.trim()).find(Boolean) || '';
  const clean = cleanTitle(line, 400).replace(REQUEST_LABEL, '').replace(QUOTES, '').trim();
  return cleanTitle(clean || fallback, 120);
}
/** The Worker conversation's own title names the desk; the brief names it until the catalog has one. */
function deskTitle(input: OfficeInput, conversationId: string, brief: string, fallback: string): string {
  const own = input.title?.(conversationId).trim();
  return own ? cleanTitle(own, 120) : briefTitle(brief, fallback);
}

function desks(input: OfficeInput): OfficeDesk[] {
  const rows = input.assignments?.assignments || [];
  const seen = new Set<string>();
  const result: OfficeDesk[] = [];
  // The page is newest first: the newest assignment of a Worker is its desk.
  for (const row of rows) {
    const recipient = row.request.recipientId;
    if (row.request.coordinatorId !== input.leadId || row.request.profile?.mode === 'coordinator'
      || !recipient || recipient === input.leadId || seen.has(recipient)) continue;
    seen.add(recipient);
    const state = deskStateOf(row, input.live(recipient));
    if (!state) continue;
    const brief = row.request.brief || '';
    const title = deskTitle(input, recipient, brief, row.assignmentId);
    result.push({ id: row.assignmentId, conversationId: recipient, title, state, model: modelForRole(title) });
  }
  // Oldest first keeps every floor in place: a new delegation adds a floor at the bottom.
  result.reverse();
  for (const worker of input.running || []) {
    if (!worker.conversationId || worker.conversationId === input.leadId || seen.has(worker.conversationId)) continue;
    seen.add(worker.conversationId);
    const title = cleanTitle(worker.title || worker.conversationId, 120);
    result.push({ id: `conversation:${worker.conversationId}`, conversationId: worker.conversationId, title,
      state: 'working', model: modelForRole(title) });
  }
  return result;
}

function projects(input: OfficeInput): OfficeProject[] {
  if (input.altitude !== 'headquarters' || !input.projects) return [];
  const folders = input.folders || [];
  const folderOf = (path: string) => folders.findIndex(folder => folder.paths.some(item => norm(item) === norm(path)));
  return input.projects.map(entry => {
    const index = folderOf(entry.projectPath), folder = index >= 0 ? folders[index] : undefined;
    const available = entry.availability === 'available' && !!entry.coordinatorConversationId;
    return { order: index < 0 ? folders.length : index, room: {
      path: entry.projectPath, name: folder?.name || basename(entry.projectPath), color: folder?.color || '',
      coordinatorId: available ? entry.coordinatorConversationId : '',
      workers: available && !entry.activityError && typeof entry.activeWorkers === 'number' ? entry.activeWorkers : null,
      questions: entry.openQuestions || 0, overdue: entry.overdueCommitments || 0,
      report: entry.lastReport?.unread ? entry.lastReport.title : '',
    } };
  }).sort((a, b) => a.order - b.order || a.room.name.localeCompare(b.room.name)).map(item => item.room);
}

export function projectLeadOffice(input: OfficeInput): OfficeView {
  const latest = input.report?.unread && input.report.conversationId === input.leadId ? input.report.latest : null;
  const tally = input.board && input.board.tasks.length ? coordinatorProgressTally(input.board) : null;
  const page = input.assignments;
  return {
    altitude: input.altitude,
    lead: {
      conversationId: input.leadId,
      state: input.lead.awaitingHuman ? 'asking' : input.lead.streaming ? 'replying' : 'idle',
      report: latest?.title || '',
      board: tally ? { done: tally.finished, total: tally.total } : null,
    },
    desks: desks(input),
    projects: projects(input),
    current: input.current,
    coverage: page === undefined ? 'loading' : page === null ? 'unavailable' : 'complete',
    projectsCoverage: input.altitude !== 'headquarters' ? 'complete'
      : input.projects === undefined ? 'loading' : input.projects === null ? 'unavailable' : 'complete',
  };
}
