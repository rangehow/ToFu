/** Tofu office projection: who sits where and what each person is doing, from live coordination reads.
 * One team zone per bound project with its lead at the head desk; the Global Router sits outside the
 * owner's office and the Watchdog at the door. A Worker has a desk only while it has unfinished work: an
 * attempt running (`busyOf`: the office busy facts when they cover busy completely, else the team row's
 * `busy`, i.e. the task registry's running set) or an assignment that is queued, waiting or blocked. Every
 * other team member is routine and only counted (`routine`).
 * Emphasis follows owner attention (attention.ts): a decision only for a real open owner question, an
 * exception only for an overdue commitment, a lead's unread blocked report or an open Watchdog incident
 * (on the Watchdog); a desk document only for an unread lead report (a deliverable when its kind is
 * delivery) or a Worker result that really arrived.
 * Pure. Entry points: projectOffice, personName, deskDocuments. Dependencies: wire types, the shared
 * assignment status, the office game's character resolver. */
import type { ProjectDispatchHistoryRow } from '../../api/project-dispatch.generated';
import type {
  CoordinatorConversationPage, CoordinatorProgressState, CoordinatorReportState,
} from '../../api/project-coordination.generated';
import { assignmentStatus, cleanTitle, type CoordinationGraphStatus } from '../project-coordination-graph';
import { modelForRole, type CharacterModel } from '../office-game/office-character';
import { deriveAttention, type OpenQuestion, type RouterAttention } from './attention';
import { stableHash } from './hash';

export type TeamKind = 'router' | 'project' | 'watchdog';
export type PersonRole = 'router' | 'lead' | 'worker' | 'watchdog';
/** working: an attempt runs now. waiting: assigned work that is not running (queued, deferred, blocked). */
export type PersonState = 'working' | 'waiting' | 'idle';
export type Emphasis = 'decision' | 'exception' | null;

export interface AssignmentRef {
  assignmentId: string; status: CoordinationGraphStatus; resultRevision: number | null; turnId: string | null;
}
export interface Person {
  id: string; role: PersonRole; team: string; name: string; detail: string; model: CharacterModel;
  state: PersonState; emphasis: Emphasis; assignment: AssignmentRef | null;
}
export interface TeamBoard {
  done: number; active: number; total: number; summary: string;
  /** Live (not cancelled) tasks in board order: the whiteboard draws one note per task. */
  tasks: ReadonlyArray<{ title: string; status: string }>;
}
export interface Team {
  key: string; kind: TeamKind; name: string; color: string; lead: Person; workers: Person[];
  /** Recent team members without a desk (finished or idle); `history` when older pages were not read. */
  routine: number; history: boolean; board: TeamBoard | null; failed: boolean;
}
export type ReportKind = 'progress' | 'decision' | 'blocked' | 'delivery';
export type DocumentRef =
  | { source: 'lead-report'; conversationId: string; turnId: string; reportKind: ReportKind }
  | { source: 'worker-result'; conversationId: string; assignmentId: string; resultRevision: number; turnId: string };
export interface DeskDocument {
  id: string; kind: 'deliverable' | 'report'; author: string; team: string; title: string; at: number; ref: DocumentRef;
}
/** A delivered result the lead accepted: a trophy on the owner's shelf; it opens that Worker's result. */
export interface Trophy {
  id: string; team: string; author: string; title: string;
  ref: Extract<DocumentRef, { source: 'worker-result' }>;
}
export interface Office {
  teams: Team[]; people: Map<string, Person>; attention: RouterAttention; documents: DeskDocument[];
  /** The owner's task board on the office whiteboard: the Global Router's board. */
  whiteboard: { owner: string; board: TeamBoard } | null;
  trophies: Trophy[];
}

export interface TeamInput {
  key: string; kind: TeamKind; name: string; color: string; leadId: string;
  team: CoordinatorConversationPage | null;
  assignments: ReadonlyArray<ProjectDispatchHistoryRow> | null;
  progress: CoordinatorProgressState | null;
  reports: CoordinatorReportState | null;
  questions: ReadonlyArray<OpenQuestion> | null;
  failed: boolean;
  /** Workers whose attempt just ended stay at their desk until the assignment read that may hold their
   * report lands, so a real report can still walk from the desk. */
  held?: ReadonlySet<string>;
  /** Conversations with a running attempt from the office busy facts; absent: the team rows' `busy`. */
  busy?: ReadonlySet<string>;
  /** Open Watchdog incidents (office alarm facts); only the Watchdog's team carries them. */
  alarms?: number;
}
export interface PersonLabels { router: string; watchdog: string; lead(team: string): string }

const SEATED: ReadonlySet<CoordinationGraphStatus> = new Set(['running', 'queued', 'waiting', 'blocked']);
/** Workers whose title names no role still look like distinct people, stable per conversation. */
const CAST: ReadonlyArray<CharacterModel> = ['executor', 'engineer', 'researcher', 'writer', 'analyst', 'reviewer', 'general'];

/** A Worker is named by its task: the bracketed or CODE: tag its lead gave it, else the title's start. */
export function personName(title: string): { name: string; detail: string } {
  const text = cleanTitle(title, 160).trim();
  const bracket = /^\[([^\]]{1,40})\]\s*([\s\S]*)$/.exec(text);
  if (bracket) return { name: bracket[1], detail: bracket[2] || '' };
  const code = /^([A-Z][A-Z0-9-]{2,40}):\s*([\s\S]*)$/.exec(text);
  if (code) return { name: code[1], detail: code[2] || '' };
  return { name: text, detail: '' };
}

export function firstLine(text: string): string {
  return text.split('\n').map(line => line.trim()).find(Boolean) || '';
}

function workerModel(id: string, title: string): CharacterModel {
  const role = modelForRole(title);
  return role === 'general' ? CAST[stableHash(id) % CAST.length] : role;
}

function board(progress: CoordinatorProgressState | null): TeamBoard | null {
  const value = progress?.board;
  if (!value) return null;
  const live = value.tasks.filter(task => task.status !== 'cancelled');
  return {
    done: live.filter(task => task.status === 'completed').length,
    active: live.filter(task => task.status === 'in_progress').length,
    total: live.length, summary: value.summary,
    tasks: live.map(task => ({ title: task.title, status: task.status })),
  };
}

function leadPerson(input: TeamInput, labels: PersonLabels, busy: boolean): Person {
  const role: PersonRole = input.kind === 'router' ? 'router' : input.kind === 'watchdog' ? 'watchdog' : 'lead';
  return {
    id: input.leadId, role, team: input.key,
    name: role === 'router' ? labels.router : role === 'watchdog' ? labels.watchdog : labels.lead(input.name),
    detail: '', model: role === 'router' ? 'general' : role === 'watchdog' ? 'reviewer' : 'coordinator',
    state: busy ? 'working' : 'idle', emphasis: null, assignment: null,
  };
}

/** Who has a running attempt: one authority per input, the busy facts when present, else the team rows. */
export function busyOf(input: TeamInput): ReadonlySet<string> {
  return input.busy || new Set((input.team?.conversations || []).filter(member => member.busy).map(member => member.conversationId));
}

/** The newest assignment per Worker this lead dispatched (rows arrive newest first). */
export function newestAssignments(leadId: string, rows: ReadonlyArray<ProjectDispatchHistoryRow> | null): Map<string, ProjectDispatchHistoryRow> {
  const newest = new Map<string, ProjectDispatchHistoryRow>();
  for (const row of rows || []) {
    const recipient = row.request.recipientId;
    if (!recipient || row.request.coordinatorId !== leadId || row.request.profile?.mode === 'coordinator') continue;
    if (!newest.has(recipient)) newest.set(recipient, row);
  }
  return newest;
}

function projectTeam(input: TeamInput, labels: PersonLabels): Team {
  const members = input.team?.conversations || [];
  const busy = busyOf(input);
  const titles = new Map(members.map(member => [member.conversationId, member.title]));
  const workers: Person[] = [];
  const seated = new Set<string>([input.leadId]);
  const seat = (id: string, title: string, row: ProjectDispatchHistoryRow | undefined) => {
    const status = row ? assignmentStatus(row) : null;
    const running = busy.has(id);
    if (!running && !(status && SEATED.has(status)) && !input.held?.has(id)) return;
    seated.add(id);
    const { name, detail } = personName(title);
    workers.push({
      id, role: 'worker', team: input.key, name, detail, model: workerModel(id, title),
      state: running ? 'working' : status && SEATED.has(status) ? 'waiting' : 'idle', emphasis: null,
      assignment: row ? {
        assignmentId: row.assignmentId, status: status!, resultRevision: row.execution?.resultRevision ?? null,
        turnId: row.execution?.turnId ?? null,
      } : null,
    });
  };
  // Oldest assignment first, so a newly started Worker takes the next free desk.
  for (const [recipient, row] of [...newestAssignments(input.leadId, input.assignments)].reverse()) {
    seat(recipient, titles.get(recipient) || firstLine(row.request.brief), row);
  }
  // Only this lead's Workers work on its floor; the owner's own chats in the project folder are not staff.
  const staff = members.filter(member => member.coordinatorId === input.leadId && member.conversationId !== input.leadId);
  for (const member of staff) {
    if (!seated.has(member.conversationId) && (busy.has(member.conversationId) || input.held?.has(member.conversationId))) seat(member.conversationId, member.title, undefined);
  }
  const routine = staff.filter(member => !seated.has(member.conversationId)).length;
  return {
    key: input.key, kind: input.kind, name: input.name, color: input.color,
    lead: leadPerson(input, labels, busy.has(input.leadId)), workers, routine,
    history: Boolean(input.team?.nextCursor), board: board(input.progress), failed: input.failed,
  };
}

/** Any member of a team as a person, seated or not: a document stays openable after its author left the floor. */
export function memberPerson(input: TeamInput, id: string, labels: PersonLabels): Person | null {
  if (id === input.leadId) return leadPerson(input, labels, busyOf(input).has(id));
  const member = input.team?.conversations.find(each => each.conversationId === id);
  const row = newestAssignments(input.leadId, input.assignments).get(id);
  const title = member?.title || (row ? firstLine(row.request.brief) : '');
  if (!title) return null;
  const { name, detail } = personName(title);
  return {
    id, role: 'worker', team: input.key, name, detail, model: workerModel(id, title),
    state: busyOf(input).has(id) ? 'working' : 'idle', emphasis: null,
    assignment: row ? { assignmentId: row.assignmentId, status: assignmentStatus(row), resultRevision: row.execution?.resultRevision ?? null,
      turnId: row.execution?.turnId ?? null } : null,
  };
}

/** Accepted, canonically delivered results of each team's own Workers, newest first within a team and
 * interleaved across teams (rows carry no timestamps), so every team's latest win is on the shelf. */
export function trophiesOf(inputs: ReadonlyArray<TeamInput>): Trophy[] {
  const lists = inputs.map(input => {
    const titles = new Map((input.team?.conversations || []).map(member => [member.conversationId, member.title]));
    const won: Trophy[] = [];
    for (const row of input.assignments || []) {
      const recipient = row.request.recipientId;
      const execution = row.execution;
      if (!recipient || row.request.coordinatorId !== input.leadId || row.request.profile?.mode === 'coordinator') continue;
      if (!row.canonicalDelivery || row.review?.decision !== 'accept' || row.delivery?.status !== 'delivered') continue;
      if (!execution?.turnId || typeof execution.resultRevision !== 'number') continue;
      won.push({
        id: `trophy:${row.assignmentId}`, team: input.key, author: recipient,
        title: personName(titles.get(recipient) || firstLine(row.request.brief)).name,
        ref: { source: 'worker-result', conversationId: recipient, assignmentId: row.assignmentId,
          resultRevision: execution.resultRevision, turnId: execution.turnId },
      });
    }
    return won;
  });
  const shelf: Trophy[] = [];
  for (let rank = 0; lists.some(list => rank < list.length); rank += 1) {
    for (const list of lists) if (rank < list.length) shelf.push(list[rank]);
  }
  return shelf;
}

/** Unread lead reports wait on the owner's desk; a delivery report is a deliverable. */
export function deskDocuments(inputs: ReadonlyArray<TeamInput>): DeskDocument[] {
  const documents: DeskDocument[] = [];
  for (const input of inputs) {
    const latest = input.reports?.unread ? input.reports.latest : null;
    if (!latest) continue;
    documents.push({
      id: `report:${input.leadId}:${latest.turnId}`, kind: latest.kind === 'delivery' ? 'deliverable' : 'report',
      author: input.leadId, team: input.key, title: latest.title || firstLine(latest.content), at: latest.createdAt,
      ref: { source: 'lead-report', conversationId: input.leadId, turnId: latest.turnId, reportKind: latest.kind },
    });
  }
  return documents.sort((a, b) => a.at - b.at);
}

export function projectOffice(now: number, inputs: ReadonlyArray<TeamInput>, labels: PersonLabels): Office {
  const teams = inputs.map(input => projectTeam(input, labels));
  const attention = deriveAttention(now, inputs.map(input => ({
    path: input.key, coordinatorId: input.leadId, questions: input.questions,
    progress: input.progress, reports: input.reports,
  })));
  const people = new Map<string, Person>();
  for (const team of teams) {
    people.set(team.lead.id, team.lead);
    for (const worker of team.workers) people.set(worker.id, worker);
  }
  for (const item of attention.exceptions) {
    const lead = teams.find(team => team.key === item.project)?.lead;
    if (lead) lead.emphasis = 'exception';
  }
  for (const input of inputs) {
    const lead = people.get(input.leadId);
    if (lead && input.reports?.unread && input.reports.latest?.kind === 'blocked') lead.emphasis = 'exception';
    if (lead && input.kind === 'watchdog' && input.alarms) lead.emphasis = 'exception';
  }
  for (const item of attention.decisions) {
    const asker = item.source.conversationId ? people.get(item.source.conversationId) : null;
    if (asker) asker.emphasis = 'decision';
  }
  const router = teams.find(team => team.kind === 'router');
  return {
    teams, people, attention, documents: deskDocuments(inputs),
    whiteboard: router?.board ? { owner: router.lead.id, board: router.board } : null,
    trophies: trophiesOf(inputs),
  };
}
