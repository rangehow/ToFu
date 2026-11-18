/** Owner attention for the Tofu Tower HUD: the HARNESS-1C model (docs/ROUTER.md R10 "Owner attention").
 * Tiers in fixed order: decision > exception > deliverable > digest. Nothing leaves by age or by a
 * client acknowledgement; an item leaves only when its receipt closes.
 *
 * RouterAttention mirrors frontend/src/api/router.generated.ts on feat/owner-attention-20261112
 * (841936e17b). Until GET /api/v4/router/attention is live, deriveAttention emits the same shape from
 * reads that already exist. When that route lands, import the generated type and read
 * project.routerAttention instead; this adapter and the mirror are then deleted.
 * Entry points: openQuestions, deriveAttention. Dependencies: wire types only (pure). */
import type { CoordinatorProgressState, CoordinatorReportState } from '../../api/project-coordination.generated';

export type AttentionTier = 'decision' | 'exception' | 'deliverable' | 'digest';
export type AttentionKind = 'question' | 'question_unverified' | 'question_answered' | 'question_resolved_by_default'
  | 'question_superseded' | 'overdue_commitment' | 'fault' | 'delivery';
export interface RouterAttentionItem {
  id: string; tier: AttentionTier; kind: AttentionKind; project: string; title: string; since: number;
  blocking: boolean; default: string;
  source: { conversationId?: string; turnId?: string; guidanceId?: string; signalId?: string; project?: string; dueAt?: number };
  evidence: { turnId?: string; firstDueAt?: number; slips?: number; reportKind?: 'delivery'; unread?: boolean; card?: string };
}
export interface RouterAttention {
  generatedAt: number;
  decisions: RouterAttentionItem[];
  exceptions: RouterAttentionItem[];
  deliverables: RouterAttentionItem[];
  digests: RouterAttentionItem[];
  counts: { decisions: number; exceptions: number; deliverables: number; digests: number };
}

export interface OpenQuestion {
  conversationId: string; turnId: string; ordinal: number; guidanceId: string; blocking: boolean;
  question: string; defaultText: string; createdAt: number;
}

/** The fields of a Conversation Sync v3 Turn this module reads; tool rounds stay loosely typed. */
export interface QuestionTurn {
  turnId: string; ordinal: number; actor: string; createdAt: number;
  projection?: { toolRounds?: ReadonlyArray<unknown> } | null;
}

/** ask_human questions in a lead's newest Turns that are still open. A blocking question is open while
 * its round awaits a human; a non-blocking one until a human Turn follows it in the same conversation. */
export function openQuestions(conversationId: string, turns: ReadonlyArray<QuestionTurn>): OpenQuestion[] {
  const open: OpenQuestion[] = [];
  for (const turn of turns) {
    for (const raw of turn.projection?.toolRounds || []) {
      const round = raw as Record<string, unknown> | null;
      if (!round || round.toolName !== 'ask_human') continue;
      const question = typeof round.guidanceQuestion === 'string' ? round.guidanceQuestion.trim() : '';
      if (!question) continue;
      // The backend marks only a non-blocking round (guidanceBlocking: false); a blocking round has no field.
      const blocking = round.guidanceBlocking !== false;
      const answered = blocking
        ? round.status !== 'awaiting_human'
        : turns.some(later => later.actor === 'human' && later.ordinal > turn.ordinal);
      // A lead withdrew it once the conversation settled it (the owner-question index omits it too).
      if (answered || round.guidanceWithdrawn) continue;
      open.push({
        conversationId, turnId: turn.turnId, ordinal: turn.ordinal, blocking, question, createdAt: turn.createdAt,
        guidanceId: typeof round.guidanceId === 'string' ? round.guidanceId : `${turn.turnId}:${open.length}`,
        defaultText: typeof round.guidanceDefault === 'string' ? round.guidanceDefault : '',
      });
    }
  }
  return open;
}

/** First readable line of a markdown question, without emphasis markers. */
export function headline(text: string): string {
  const line = text.split('\n').map(part => part.trim()).find(Boolean) || '';
  return line.replace(/^#+\s*/, '').replace(/\*\*|__|`/g, '').trim();
}

export interface AttentionProjectSource {
  path: string;
  coordinatorId: string;
  /** null: the lead's Turns were not read, so its questions are unknown, not absent. */
  questions: ReadonlyArray<OpenQuestion> | null;
  progress: CoordinatorProgressState | null;
  reports: CoordinatorReportState | null;
}

export function deriveAttention(now: number, projects: ReadonlyArray<AttentionProjectSource>): RouterAttention {
  const decisions: RouterAttentionItem[] = [];
  const exceptions: RouterAttentionItem[] = [];
  const deliverables: RouterAttentionItem[] = [];
  for (const project of projects) {
    for (const question of project.questions || []) {
      decisions.push({
        id: `question:${project.coordinatorId}:${question.guidanceId}`, tier: 'decision', kind: 'question',
        project: project.path, title: headline(question.question), since: question.createdAt,
        blocking: question.blocking, default: question.defaultText,
        source: { conversationId: question.conversationId, turnId: question.turnId, guidanceId: question.guidanceId },
        evidence: { turnId: question.turnId },
      });
    }
    for (const commitment of project.progress?.commitments || []) {
      if (commitment.state !== 'open' || commitment.dueAt >= now) continue;
      exceptions.push({
        id: `commitment:${project.path}:${commitment.title}:${commitment.dueAt}`, tier: 'exception',
        kind: 'overdue_commitment', project: project.path, title: commitment.title, since: commitment.dueAt,
        blocking: false, default: '', source: { project: project.path, dueAt: commitment.dueAt },
        evidence: { firstDueAt: commitment.firstDueAt, slips: commitment.slips },
      });
    }
    const latest = project.reports?.latest;
    if (latest && latest.kind === 'delivery' && project.reports?.unread) {
      deliverables.push({
        id: `delivery:${project.coordinatorId}:${latest.turnId}`, tier: 'deliverable', kind: 'delivery',
        project: project.path, title: latest.title, since: latest.createdAt, blocking: false, default: '',
        source: { conversationId: project.coordinatorId, turnId: latest.turnId },
        evidence: { reportKind: 'delivery', unread: true },
      });
    }
  }
  decisions.sort((a, b) => Number(b.blocking) - Number(a.blocking) || a.since - b.since);
  exceptions.sort((a, b) => a.since - b.since);
  deliverables.sort((a, b) => b.since - a.since);
  return {
    generatedAt: now, decisions, exceptions, deliverables, digests: [],
    counts: { decisions: decisions.length, exceptions: exceptions.length, deliverables: deliverables.length, digests: 0 },
  };
}

/** Tray order: every decision, then exceptions, then deliverables, then digests. */
export function attentionQueue(attention: RouterAttention): RouterAttentionItem[] {
  return [...attention.decisions, ...attention.exceptions, ...attention.deliverables, ...attention.digests];
}
