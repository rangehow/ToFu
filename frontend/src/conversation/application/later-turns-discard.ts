/**
 * Responsibility: the owner's review before regenerating a Turn discards the
 * later Turns of its conversation (a resume completes the Turn in place and
 * never asks).
 *
 * The server refuses such a command with a 409 LaterTurnsRefusal listing the
 * exact deletion closure (contracts/conversation_sync_v3.yaml). This module
 * reads that refusal, renders the localized review (how many Turns and, for
 * each, who, when and its first line) with "keep" first and focused, and
 * yields the Turn ids to acknowledge only when the owner chose to discard
 * them. It never computes the closure itself: only the server's list can match
 * the authority's atomic check, which refuses a stale acknowledgement.
 * Entry points: laterTurnsRefusal, reviewLaterTurnsDiscard,
 * laterTurnsDiscardCommandId. Dependencies: injected choice dialog,
 * translator and time formatting; no DOM or module state.
 */
import type { LaterTurnsClosure, LaterTurnSummary } from '../../api/conversation-sync.generated';
import { assertConversationSyncSchema } from '../../api/conversation-sync.generated';

export type LaterTurnsRefusalKind = 'later_turns_unacknowledged' | 'later_turns_changed';

export interface LaterTurnsRefusal {
  readonly kind: LaterTurnsRefusalKind;
  readonly closure: LaterTurnsClosure;
}

export interface LaterTurnsChoiceOption {
  readonly value: string;
  readonly label: string;
  readonly subtitle?: string;
}

export interface LaterTurnsReviewPorts {
  showChoice(config: {
    readonly title: string;
    readonly message: string;
    readonly options: readonly LaterTurnsChoiceOption[];
    readonly dismissValue: string;
  }): Promise<string | null>;
  translate(key: string, params?: Record<string, unknown>): string;
  formatTime(epochMs: number): string;
}

export const LATER_TURNS_KEEP = 'keep';
export const LATER_TURNS_DISCARD = 'discard';

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {};
}

/** The typed later-Turn refusal a failed createAttempt carries, or null. */
export function laterTurnsRefusal(error: unknown): LaterTurnsRefusal | null {
  const failure = record(error);
  const body = record(failure.body ?? failure.response);
  const kind = record(body.error).kind;
  if (kind !== 'later_turns_unacknowledged' && kind !== 'later_turns_changed') return null;
  try {
    const closure = assertConversationSyncSchema<LaterTurnsClosure>(
      'LaterTurnsClosure', body.laterTurns);
    if (closure.count !== closure.turnIds.length) return null;
    return { kind, closure };
  } catch {
    return null;
  }
}

function plural(count: number): Record<string, unknown> {
  return { count, s: count === 1 ? '' : 's' };
}

function speaker(turn: LaterTurnSummary, translate: LaterTurnsReviewPorts['translate']): string {
  if (turn.actor === 'human') return translate('laterTurns.actorYou');
  if (turn.actor === 'assistant' || turn.actor === 'planner') {
    return translate('laterTurns.actorAssistant');
  }
  return translate('laterTurns.actorOther');
}

/** One line per discarded Turn: who, when, first line, branch marker. */
export function laterTurnsReviewMessage(
  refusal: LaterTurnsRefusal, targetLaneId: string, ports: LaterTurnsReviewPorts,
): string {
  const lead = refusal.kind === 'later_turns_changed'
    ? ports.translate('laterTurns.changed')
    : ports.translate('laterTurns.lead');
  const rows = refusal.closure.turns.map((turn) => {
    const when = turn.createdAt ? ports.formatTime(turn.createdAt) : '';
    const text = turn.preview || ports.translate('laterTurns.noText');
    const branch = turn.laneId === targetLaneId ? '' : ` ${ports.translate('laterTurns.inBranch')}`;
    return `• ${speaker(turn, ports.translate)}${when ? ` · ${when}` : ''}${branch} — ${text}`;
  });
  return [lead, '', ...rows].join('\n');
}

/**
 * Ask the owner; resolve the exact Turn ids to acknowledge, or null to keep
 * every later Turn. Keep is the first option, the default and the dismissal.
 */
export async function reviewLaterTurnsDiscard(
  refusal: LaterTurnsRefusal, targetLaneId: string, ports: LaterTurnsReviewPorts,
): Promise<readonly string[] | null> {
  const count = refusal.closure.count;
  const choice = await ports.showChoice({
    title: ports.translate('laterTurns.title', plural(count)),
    message: laterTurnsReviewMessage(refusal, targetLaneId, ports),
    options: [
      { value: LATER_TURNS_KEEP, label: ports.translate('laterTurns.keep'),
        subtitle: ports.translate('laterTurns.keepHint') },
      { value: LATER_TURNS_DISCARD, label: ports.translate('laterTurns.discard', plural(count)),
        subtitle: ports.translate('laterTurns.discardHint') },
    ],
    dismissValue: LATER_TURNS_KEEP,
  });
  return choice === LATER_TURNS_DISCARD ? [...refusal.closure.turnIds] : null;
}

/** A distinct, bounded command identity for each acknowledged closure. */
export function laterTurnsDiscardCommandId(
  baseCommandId: string, turnIds: readonly string[],
): string {
  let hash = 0x811c9dc5;
  for (const char of [...turnIds].sort().join('\n')) {
    hash = Math.imul(hash ^ char.charCodeAt(0), 0x01000193) >>> 0;
  }
  return `${baseCommandId}:discard-${turnIds.length}-${hash.toString(16).padStart(8, '0')}`;
}
