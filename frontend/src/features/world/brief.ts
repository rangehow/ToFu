/** What a person's panel leads with, projected from the same live reads as the office: what they did
 * (a Worker's delivered result or current task; a lead's latest owner report), the evidence behind it
 * (acceptance checks with the lead's verdicts, the delivered checkpoint and its delivery state; a lead's
 * board) and the decisions waiting on the owner (its open questions, and overdue commitments). Every
 * item keeps the Turn it came from, so "show in conversation" opens the raw transcript at that Turn.
 * Pure. Entry point: projectBrief. */
import type { ProjectDispatchHistoryRow } from '../../api/project-dispatch.generated';
import { assignmentStatus, type CoordinationGraphStatus } from '../project-coordination-graph';
import type { OpenQuestion } from './attention';
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
export interface Brief {
  person: Person;
  decisions: BriefDecision[];
  overdue: Array<{ title: string; dueAt: number; slips: number }>;
  lead: BriefLead;
  checks: BriefCheck[];
  review: { decision: 'accept' | 'rework' | 'reject'; note: string } | null;
  delivery: { checkpoint: string; status: string; reason: string } | null;
  board: { summary: string; done: number; total: number; tasks: Array<{ title: string; status: string; progress: string }> } | null;
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

export function projectBrief(person: Person, input: TeamInput | undefined, now: number, focus?: DocumentRef): Brief {
  const brief: Brief = { person, decisions: [], overdue: [], lead: { kind: 'none' }, checks: [], review: null, delivery: null, board: null };
  if (!input) return brief;
  if (person.role === 'worker') {
    const rows = newestAssignments(input.leadId, input.assignments);
    const row = focus?.source === 'worker-result'
      ? (input.assignments || []).find(each => each.assignmentId === focus.assignmentId) || rows.get(person.id)
      : rows.get(person.id);
    brief.lead = workerLead(row, person);
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
    const live = board.tasks.filter(task => task.status !== 'cancelled');
    brief.board = {
      summary: board.summary, done: live.filter(task => task.status === 'completed').length, total: live.length,
      tasks: live.filter(task => task.status !== 'completed').map(task => ({ title: task.title, status: task.status, progress: task.progress || '' })),
    };
  }
  return brief;
}
