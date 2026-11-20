/** The office brief of one person, projected from the same live reads as the office plus that person's
 * journal (their deliverables projection, journal.ts); nothing is parsed from a transcript. It holds only
 * what the office shows (docs/FRONTEND_ARCHITECTURE.md, "Office brief"): the decisions waiting on the owner
 * (a lead's open questions, overdue commitments); where things stand now (a lead's board in its displayed
 * language; a Worker's assignment steps, task, result and acceptance checks with the lead's verdicts and the
 * delivered checkpoint); the progress timeline (report_to_user reports, completed board tasks and merged
 * results, newest first, by local day) and the files (each report's attachments and published files).
 * Every item keeps the Turn or artifact it came from; the full transcript is the classic view's.
 * Pure. Entry point: projectBrief. */
import type { ProjectDispatchHistoryRow } from '../../api/project-dispatch.generated';
import { assignmentStatus, type CoordinationGraphStatus } from '../project-coordination-graph';
import {
  deliverableCopy, deliverableDays, deliverableVisual, type DeliverableVisual, type LeadDeliverable,
} from '../lead-deliverables/model';
import type { OpenQuestion } from './attention';
import type { Journal, JournalStatus } from './journal';
import { newestAssignments, type DocumentRef, type Person, type ReportKind, type TeamInput } from './model';

export type Verdict = 'met' | 'unmet' | 'unknown' | 'unreviewed';
/** The Turn an item came from; the ordinal, when known, lets the classic owner page an older Turn in. */
export interface TurnRef { turnId: string; ordinal?: number }
export interface BriefCheck { text: string; verdict: Verdict; evidence: string }
/** One open owner question of a lead: its ask_human guidance id and Turn bind the answer to it. */
export interface BriefDecision {
  guidanceId: string; conversationId: string; project: string;
  question: string; blocking: boolean; fallback: string; turn: TurnRef; at: number;
}
export type BriefLead =
  | { kind: 'result'; markdown: string; turn: TurnRef | null; status: CoordinationGraphStatus }
  | { kind: 'task'; markdown: string; turn: TurnRef | null; status: CoordinationGraphStatus }
  | { kind: 'report'; title: string; markdown: string; reportKind: ReportKind; at: number; unread: boolean; turn: TurnRef }
  | { kind: 'none' };
/** A Worker's assignment, step by step: dispatched, an attempt ran, its result came back, the lead reviewed it,
 * the checkpoint was merged. */
export type WorkerStep = 'assigned' | 'working' | 'reported' | 'reviewed' | 'merged';
export interface BriefStep { step: WorkerStep; done: boolean; failed: boolean }
export interface BriefFile {
  /** The item the shared opener opens (the artifact itself). */
  item: LeadDeliverable;
  name: string; visual: DeliverableVisual; sizeBytes: number; at: number; path: string;
}
export interface BriefEntry {
  item: LeadDeliverable; visual: DeliverableVisual; at: number;
  title: string; summary: string;
  /** The board task a completion closed (its displayed title). */
  task: string;
  files: BriefFile[];
}
export interface BriefJournal {
  status: JournalStatus;
  days: Array<{ start: number; entries: BriefEntry[] }>;
  files: BriefFile[];
  more: boolean; loadingMore: boolean;
}
export interface Brief {
  person: Person;
  decisions: BriefDecision[];
  overdue: Array<{ title: string; dueAt: number; slips: number }>;
  lead: BriefLead;
  /** A Worker's assignment: what it was asked to do (its brief's first paragraph) and its steps. */
  task: string;
  steps: BriefStep[];
  checks: BriefCheck[];
  review: { decision: 'accept' | 'rework' | 'reject'; note: string } | null;
  delivery: { checkpoint: string; status: string; reason: string } | null;
  board: {
    summary: string; done: number; total: number; updatedAt: number;
    tasks: Array<{ title: string; status: string; progress: string }>;
  } | null;
  journal: BriefJournal;
}

/** The task's first paragraph: what the lead asked this Worker to do. */
export function taskParagraph(brief: string): string {
  const blocks = brief.replace(/\r\n?/g, '\n').split(/\n\s*\n/).map(block => block.trim()).filter(Boolean);
  const meaningful = blocks.find(block => !/^(CONTEXT|BACKGROUND|背景)\b/i.test(block)) || blocks[0] || '';
  return meaningful;
}

function workerLead(row: ProjectDispatchHistoryRow | undefined, person: Person): BriefLead {
  // A team member without a readable assignment still has its conversation title as its task.
  if (!row) {
    const title = [person.name, person.detail].filter(Boolean).join(' — ');
    return title ? { kind: 'task', markdown: title, turn: null, status: person.state === 'working' ? 'running' : 'unknown' } : { kind: 'none' };
  }
  const status = assignmentStatus(row);
  const content = typeof row.execution?.result?.content === 'string' ? row.execution.result.content.trim() : '';
  const turn = row.execution?.turnId ? { turnId: row.execution.turnId } : null;
  if ((status === 'completed' || status === 'failed') && content) return { kind: 'result', markdown: content, turn, status };
  return { kind: 'task', markdown: taskParagraph(row.request.brief), turn, status };
}

function checks(row: ProjectDispatchHistoryRow | undefined): BriefCheck[] {
  if (!row) return [];
  const reviewed = row.review?.acceptanceResults || [];
  return (row.request.acceptance || []).map((text, index) => {
    const result = reviewed.find(item => item.check === index + 1) || reviewed.find(item => item.check === index);
    return { text, verdict: result ? result.verdict : 'unreviewed', evidence: result?.evidence || '' };
  });
}

function workerSteps(row: ProjectDispatchHistoryRow | undefined): BriefStep[] {
  if (!row) return [];
  const status = assignmentStatus(row);
  const reported = status === 'completed' || status === 'failed';
  const ran = reported || Boolean(row.execution?.attemptId) || status === 'running';
  const decision = row.review?.decision;
  const delivery = row.delivery?.status || '';
  return [
    { step: 'assigned', done: true, failed: false },
    { step: 'working', done: ran, failed: false },
    { step: 'reported', done: reported, failed: status === 'failed' },
    { step: 'reviewed', done: Boolean(decision), failed: decision === 'reject' || decision === 'rework' },
    { step: 'merged', done: delivery === 'delivered', failed: delivery === 'blocked' },
  ];
}

function fileOf(owner: LeadDeliverable, file: NonNullable<LeadDeliverable['files']>[number]): BriefFile {
  const item: LeadDeliverable = { id: `artifact:${file.artifactId}`, kind: file.kind, title: file.filename,
    projectPath: owner.projectPath, createdAt: owner.createdAt, source: owner.source, artifactId: file.artifactId };
  return { item, name: file.filename, visual: deliverableVisual(item), sizeBytes: file.sizeBytes || 0, at: owner.createdAt, path: file.path || '' };
}

const TIMELINE_KINDS: ReadonlySet<LeadDeliverable['kind']> = new Set(['report', 'completion', 'merge']);

/** The progress timeline (reports, completed tasks, merged results) by local day, and every file, newest first. */
export function projectJournal(journal: Journal): BriefJournal {
  const files: BriefFile[] = [];
  const entries = new Map<string, BriefEntry>();
  for (const item of journal.items) {
    if (!TIMELINE_KINDS.has(item.kind)) {
      if (item.artifactId) files.push({ item, name: item.title, visual: deliverableVisual(item), sizeBytes: 0, at: item.createdAt, path: '' });
      continue;
    }
    const copy = deliverableCopy(item);
    const attached = (item.files || []).map(file => fileOf(item, file));
    files.push(...attached);
    const taskKey = item.tasks?.[0]?.key;
    const task = item.kind === 'completion' && taskKey ? journal.taskTitles.get(taskKey) || item.tasks![0].title : '';
    entries.set(item.id, {
      item, visual: deliverableVisual(item), at: item.createdAt,
      title: task || copy.title, summary: task ? copy.title : copy.summary, task, files: attached,
    });
  }
  const timeline = journal.items.filter(item => entries.has(item.id));
  return {
    status: journal.status,
    days: deliverableDays(timeline).map(day => ({ start: day.start, entries: day.items.map(item => entries.get(item.id)!) })),
    files, more: Boolean(journal.nextCursor), loadingMore: journal.loadingMore,
  };
}

const NO_JOURNAL: Journal = { status: 'unavailable', items: [], taskTitles: new Map(), nextCursor: null, loadingMore: false };

export function projectBrief(person: Person, input: TeamInput | undefined, now: number, focus?: DocumentRef, journal: Journal = NO_JOURNAL): Brief {
  const brief: Brief = {
    person, decisions: [], overdue: [], lead: { kind: 'none' }, task: '', steps: [], checks: [], review: null, delivery: null, board: null,
    journal: projectJournal(journal),
  };
  if (!input) return brief;
  if (person.role === 'worker') {
    const rows = newestAssignments(input.leadId, input.assignments);
    const row = focus?.source === 'worker-result'
      ? (input.assignments || []).find(each => each.assignmentId === focus.assignmentId) || rows.get(person.id)
      : rows.get(person.id);
    brief.lead = workerLead(row, person);
    brief.task = row ? taskParagraph(row.request.brief) : [person.name, person.detail].filter(Boolean).join(' — ');
    brief.steps = workerSteps(row);
    brief.checks = checks(row);
    if (row?.review) brief.review = { decision: row.review.decision, note: row.review.note };
    const checkpoint = row?.delivery?.checkpointSha || row?.publication?.checkpointSha || '';
    if (checkpoint) brief.delivery = { checkpoint, status: row?.delivery?.status || 'published', reason: row?.delivery?.reason || '' };
    return brief;
  }
  const questions: ReadonlyArray<OpenQuestion> = input.questions || [];
  brief.decisions = questions.map(question => ({
    guidanceId: question.guidanceId, conversationId: question.conversationId, project: input.name,
    question: question.question, blocking: question.blocking, fallback: question.defaultText,
    turn: { turnId: question.turnId, ordinal: question.ordinal }, at: question.createdAt,
  }));
  brief.overdue = (input.progress?.commitments || [])
    .filter(commitment => commitment.state === 'open' && commitment.dueAt < now)
    .map(commitment => ({ title: commitment.title, dueAt: commitment.dueAt, slips: commitment.slips }));
  const latest = input.reports?.latest;
  if (latest) {
    brief.lead = { kind: 'report', title: latest.title, markdown: latest.content, reportKind: latest.kind, at: latest.createdAt,
      unread: Boolean(input.reports?.unread), turn: { turnId: latest.turnId, ordinal: latest.ordinal } };
  }
  const board = input.progress?.board;
  if (board) {
    // The board as the lead's board shows it: its own display translation where it has one.
    const shown = new Map((input.progress?.translation?.texts || []).map(entry => [entry.source, entry.text]));
    const text = (value: string) => shown.get(value) || value;
    const live = board.tasks.filter(task => task.status !== 'cancelled');
    const open = live.filter(task => task.status !== 'completed');
    brief.board = {
      summary: text(board.summary), done: live.length - open.length, total: live.length, updatedAt: board.updatedAt,
      tasks: [...open.filter(task => task.status === 'in_progress'), ...open.filter(task => task.status !== 'in_progress')]
        .map(task => ({ title: text(task.title), status: task.status, progress: task.progress ? text(task.progress) : '' })),
    };
  }
  return brief;
}
