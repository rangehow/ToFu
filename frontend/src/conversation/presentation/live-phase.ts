/** Pure presentation over the backend-authored live phase in TurnState. */

type UnknownRecord = Record<string, unknown>;

export interface ConversationRateLimitPresentation {
  model: string;
  attempt: number;
}

export interface ConfiguredFallbackTarget {
  model: string;
  providerId?: string;
  providerName?: string;
}

export interface ExplicitFallbackPhaseOffer extends ConfiguredFallbackTarget {
  conversationId: string;
  turnId: string;
  attemptId: string;
  projectionRevision: number;
  currentModel: string;
  currentProviderId?: string;
}

/** The live phase is an offer hint; the click path revalidates its exact Turn
 * and resolves this same catalog row through the existing model route picker. */
export function configuredFallbackPhaseOffer(
  phase: unknown,
  turn: {
    conversationId?: unknown;
    turnId?: unknown;
    currentAttemptId?: unknown;
    status?: unknown;
    laneId?: unknown;
    actor?: unknown;
    kind?: unknown;
    projectionRevision?: unknown;
    projection?: unknown;
    transientPresentation?: unknown;
  } | null | undefined,
  target: ConfiguredFallbackTarget | null | undefined,
  fallbackDisabled = false,
): ExplicitFallbackPhaseOffer | null {
  if (!phase || typeof phase !== 'object' || Array.isArray(phase)
      || !turn || turn.transientPresentation
      || !presentConversationRateLimit(phase)) return null;
  const value = phase as UnknownRecord;
  const currentModel = typeof value.model === 'string' ? value.model.trim() : '';
  const currentAttemptId = typeof turn.currentAttemptId === 'string'
    ? turn.currentAttemptId : '';
  const targetModel = typeof target?.model === 'string' ? target.model.trim() : '';
  if (value.dispatchMode !== 'strict_model' || fallbackDisabled || !currentModel
      || !currentAttemptId || !targetModel || targetModel === currentModel
      || (turn.status !== 'running' && turn.status !== 'pending')
      || typeof turn.conversationId !== 'string' || !turn.conversationId
      || typeof turn.turnId !== 'string' || !turn.turnId
      || turn.laneId !== 'main' || turn.actor !== 'assistant'
      || (turn.kind !== 'reply' && turn.kind !== 'text')) return null;
  const projection = turn.projection && typeof turn.projection === 'object'
      && !Array.isArray(turn.projection)
    ? turn.projection as UnknownRecord : {};
  const origin = projection.origin && typeof projection.origin === 'object'
      && !Array.isArray(projection.origin)
    ? projection.origin as UnknownRecord : {};
  const initiator = typeof origin.initiator === 'string' ? origin.initiator : '';
  if (initiator && initiator !== 'human') return null;
  if (projection.orchestration || projection._projectDispatchAdmission
      || projection.flowDefinition || projection.flowBuiltin || projection.flowId
      || projection.autopilot || projection.delegateMode) return null;
  const projectionRevision = Number(turn.projectionRevision);
  return {
    conversationId: String(turn.conversationId || ''),
    turnId: String(turn.turnId || ''),
    attemptId: currentAttemptId,
    projectionRevision: Number.isInteger(projectionRevision) && projectionRevision >= 0
      ? projectionRevision : 0,
    currentModel,
    ...(typeof value.providerId === 'string' && value.providerId
      ? { currentProviderId: value.providerId } : {}),
    model: targetModel,
    ...(target?.providerId ? { providerId: target.providerId } : {}),
    ...(target?.providerName ? { providerName: target.providerName } : {}),
  };
}

/** Return sidebar-visible rate-limit detail, or null for every other wait. */
export function presentConversationRateLimit(
  phase: unknown,
): ConversationRateLimitPresentation | null {
  if (!phase || typeof phase !== 'object' || Array.isArray(phase)) return null;
  const value = phase as UnknownRecord;
  if (value.phase !== 'retrying') return null;
  const detailArgs = value.detailArgs && typeof value.detailArgs === 'object'
      && !Array.isArray(value.detailArgs)
    ? value.detailArgs as UnknownRecord : {};
  // Subscription/billing quota exhaustion is NOT a transient rate limit: it
  // won't recover in seconds, so it must not light the "限流中/rate-limited"
  // chip. It renders through its own detailKey/reasonKey copy instead.
  const quotaExhausted = value.detailKey === 'stream.phase.subscriptionQuotaReached'
    || value.detailKey === 'stream.phase.waitingQuotaReset'
    || detailArgs.reasonKey === 'stream.retryReason.subscriptionQuotaReached';
  if (quotaExhausted
      || detailArgs.reasonKey === 'stream.retryReason.keyBalanceExhausted') return null;
  const rateLimited = value.detailKey === 'stream.phase.retryRateLimited'
    || detailArgs.reasonKey === 'stream.retryReason.waitingForModel'
    || detailArgs.reasonKey === 'stream.retryReason.rateLimited';
  if (!rateLimited) return null;
  const attempt = Number(value.attempt || 0);
  return {
    model: typeof detailArgs.model === 'string' ? detailArgs.model : '',
    attempt: Number.isFinite(attempt) ? attempt : 0,
  };
}

import type { TransientTurnPresentation } from '../domain/transient-turn';

export type LivePhaseText = (
  key: string, fallback: string, args?: Readonly<Record<string, string | number>>,
) => string;

/** One label policy for normal attempts, virtual users and Goal root summaries.
 * Caller owns DOM and Goal role context; storage warnings always stay local. */
export function presentLivePhaseLabel(value: TransientTurnPresentation, text: LivePhaseText): string {
  const phase = value.phase || 'working';
  if (phase === 'storage_wedged') {
    return text('stream.phase.storageWedged',
      'Execution records could not be saved. Status updates are paused while the server retries.');
  }
  const phaseFallback = phase === 'executor_preparing'
    ? "Task accepted; binding it to this server's task scheduler…"
    : phase === 'executor_queued'
      ? "Waiting in this server's AI task queue; syncing queue position and available slots (not model/API quota)…"
      : phase === 'worker_starting' ? 'Server execution slot acquired; starting the task…' : '';
  const localizedDetail = value.detailKey
    ? text(value.detailKey, value.detail || value.label || phaseFallback, value.detailArgs) : '';
  let label = localizedDetail || value.detail || value.label;
  if (phase === 'warming') label = text('autopilot.warming', 'Autopilot is starting…');
  else if (phase === 'thinking_active') label = text('stream.thinking.active', 'Thinking…');
  else if (phase === 'llm_thinking' && !label) label = text('stream.phase.generatingResponse', 'Generating response…');
  else if (phase === 'tool_exec' && value.tools?.length && !localizedDetail) label = value.detail || value.tools.join(', ');
  else if (phase === 'retrying' && !localizedDetail && !value.detail) label = text('stream.phase.retrying', 'Retrying…');
  else if (phase === 'waiting' && !localizedDetail && !value.detail) label = text('stream.phase.waitingWorkerStatus', 'Waiting for an execution status update…');
  else if (phase === 'responding' && !value.detail) label = text('stream.phase.generatingResponse', 'Generating response…');
  if (!label && phase === 'waiting_model') label = text('stream.phase.waitingModel', 'Sent to the model, waiting for it to start replying…');
  const route = value.modelRoute;
  if (route?.selectedModel && route.resolvedModel && route.selectedModel !== route.resolvedModel
      && value.detailKey !== 'stream.phase.modelRouted') {
    const routeLabel = text('stream.phase.modelRouted', 'Model routing: {from} → {to} ({role}, {tier})', {
      from: route.selectedModel, to: route.resolvedModel, role: route.role, tier: route.tier,
    });
    label = label ? `${routeLabel} · ${label}` : routeLabel;
  }
  return label;
}

/** Choose a joined Goal worker phase only for a waiting root, not warnings. */
export function liveStatusPhase(value: TransientTurnPresentation): TransientTurnPresentation {
  return value.goalProgress?.rootSummary && value.phase === 'waiting'
    ? value.goalProgress.currentPhase ?? value : value;
}
