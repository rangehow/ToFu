import { entityRecordValues, isPublishedEntityRecord } from '../domain/turn-store';
/**
 * Pure ordered reads over normalized TurnState for retained feature adapters.
 *
 * This is the sole migration seam for features that need transcript facts but
 * do not own conversation state. Callers receive Turn records and projections,
 * never the positional compatibility message document.
 */
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { activeTurnAttemptId, activeTurnAttempts } from '../../core/turn-state';
import type { TurnState, TurnStateUpdate } from '../domain/turn-store';

export function orderedConversationTurns(
  state: TurnState | null | undefined,
  laneId = 'main',
): ReadonlyArray<TurnRecord> {
  if (!state) return [];
  return (state.laneOrder[laneId] ?? []).flatMap((turnId) => {
    const turn = state.turnsById[turnId];
    return turn ? [turn] : [];
  });
}

export function latestConversationTurn(
  state: TurnState | null | undefined,
  predicate: (turn: TurnRecord) => boolean = () => true,
  laneId = 'main',
): TurnRecord | null {
  // Reads from the end only: the latest Turn of a long lane is near its end.
  const turnIds = state?.laneOrder[laneId] ?? [];
  for (let index = turnIds.length - 1; index >= 0; index -= 1) {
    const turn = state!.turnsById[turnIds[index]];
    if (turn && predicate(turn)) return turn;
  }
  return null;
}

export function conversationHasActor(
  state: TurnState | null | undefined,
  actor: TurnRecord['actor'],
  laneId = 'main',
): boolean {
  return orderedConversationTurns(state, laneId)
    .some((turn) => turn.actor === actor);
}

const timelineArchiveIds = new WeakMap<object, ReadonlyArray<string>>();
const archiveIdsByTurns = new WeakMap<object, ReadonlyArray<string>>();

function sameArchiveInputs(prior: TurnRecord | undefined, next: TurnRecord | undefined): boolean {
  if (prior === next) return true;
  if (!prior || !next) return false;
  return prior.conversationId === next.conversationId
    && prior.projection?.activityTimeline === next.projection?.activityTimeline
    && prior.projection?.compaction?.archiveId === next.projection?.compaction?.archiveId;
}

/**
 * Compaction archives this conversation's loaded Turns reference: running or
 * settled `context_compaction` receipts and manual compaction Turns. Unchanged
 * Turns keep their timeline identity, so each timeline is scanned once. With
 * the reducer receipt that produced `state`, a transition whose Turns keep
 * these inputs reuses the previous result instead of visiting every Turn.
 */
export function conversationCompactionArchiveIds(
  state: TurnState | null | undefined,
  update?: TurnStateUpdate,
): ReadonlyArray<string> {
  if (!state) return [];
  const cacheable = isPublishedEntityRecord(state.turnsById);
  const known = cacheable ? archiveIdsByTurns.get(state.turnsById) : undefined;
  if (known) return known;
  const before = update?.previousState;
  const prior = before && archiveIdsByTurns.get(before.turnsById);
  if (cacheable && prior && update!.changes && !update!.changes.reset && before!.conversationId === state.conversationId
      && update!.changes.turnIds.every(id => sameArchiveInputs(before!.turnsById[id], state.turnsById[id]))) {
    archiveIdsByTurns.set(state.turnsById, prior);
    return prior;
  }
  const ids = new Set<string>();
  for (const turn of entityRecordValues(state.turnsById)) {
    if (!turn || turn.conversationId !== state.conversationId) continue;
    const { activityTimeline, compaction } = turn.projection ?? {};
    if (compaction?.archiveId) ids.add(compaction.archiveId);
    if (!activityTimeline) continue;
    let archived = timelineArchiveIds.get(activityTimeline);
    if (!archived) {
      archived = activityTimeline.entries.flatMap((entry) => (
        entry.reasonCode === 'context_compaction' && entry.archiveId
          ? [entry.archiveId] : []
      ));
      timelineArchiveIds.set(activityTimeline, archived);
    }
    for (const id of archived) ids.add(id);
  }
  const result = [...ids];
  if (cacheable) archiveIdsByTurns.set(state.turnsById, result);
  return result;
}

/** Active attempt ids across every lane, derived only from live Turns. */
export function activeConversationAttemptIds(
  state: TurnState | null | undefined,
): ReadonlyArray<string> {
  return [...activeTurnAttempts(state).keys()];
}

/** Active main-lane attempt, used by queue-vs-steer and abort commands. */
export function activeMainConversationAttemptId(
  state: TurnState | null | undefined,
): string | null {
  return latestConversationTurn(state, turn => Boolean(activeTurnAttemptId(turn)))?.currentAttemptId ?? null;
}
