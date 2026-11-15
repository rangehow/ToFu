/** One explicit user gesture: stop an exact 429-waiting Attempt, then use the
 * configured model through the ordinary settled-Turn operation port. No durable
 * browser intent or second execution state machine survives a reload. */
import type {
  TurnRecord,
  TurnResumeOption,
} from '../../api/conversation-sync.generated';
import type { TurnState } from '../domain/turn-store';
import type { ExplicitFallbackPhaseOffer } from '../presentation/live-phase';

export type ExplicitFallbackOffer = ExplicitFallbackPhaseOffer;

export interface ExplicitFallbackPreparation {
  config: Record<string, unknown>;
}

export interface ExplicitFallbackOperation {
  conversationId: string;
  turnId: string;
  operation: TurnResumeOption['operation'];
  commandId: string;
  config: Record<string, unknown>;
  resumeAnchor?: Record<string, unknown>;
  inputUpdate?: Record<string, unknown>;
  expectedInputProjectionRevision?: number;
}

export interface ExplicitFallbackRecoveryPorts {
  hydrate(conversationId: string): Promise<unknown>;
  subscribe(conversationId: string, changed: () => void): () => void;
  readState(conversationId: string): TurnState | null;
  /** The existing route picker owns both model and provider routing pins. */
  prepareTarget(
    offer: ExplicitFallbackOffer,
    state: TurnState,
    signal: AbortSignal,
  ): Promise<ExplicitFallbackPreparation>;
  /** Existing resume path: contextSnapshot-only rebind at current parent head. */
  contextInputUpdate(
    turn: TurnRecord,
    state: TurnState,
    signal: AbortSignal,
  ): Pick<ExplicitFallbackOperation, 'inputUpdate' | 'expectedInputProjectionRevision'>;
  /** Regenerate uses the same existing parent-projection rebind as a normal click. */
  regenerateInputUpdate(
    turn: TurnRecord,
    state: TurnState,
    signal: AbortSignal,
  ): Pick<ExplicitFallbackOperation, 'inputUpdate' | 'expectedInputProjectionRevision'>;
  abortAttempt(attemptId: string): Promise<unknown>;
  /** Existing createAttempt owner reads projectionRevision at each dispatch. */
  operateAttempt(operation: ExplicitFallbackOperation): Promise<unknown>;
  hasAuthoritativeCommand(conversationId: string, commandId: string): boolean;
  reportError(error: unknown): void;
  commandId(): string;
}

const LIVE = new Set(['pending', 'running']);
const RECOVERABLE = new Set(['interrupted', 'truncated', 'failed']);

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function attemptSettled(attempt: TurnState['attemptsById'][string]): boolean {
  return Boolean(attempt && !LIVE.has(attempt.status || ''));
}

function sameTurn(state: TurnState, offer: ExplicitFallbackOffer): TurnRecord {
  const turn = state.turnsById[offer.turnId];
  if (!turn || turn.laneId !== 'main') {
    throw new Error('The running Turn changed; review it before using a fallback.');
  }
  return turn;
}

/** Read only the exact old Attempt plus its authoritative current Turn head. */
function settlement(
  state: TurnState,
  offer: ExplicitFallbackOffer,
): 'pending' | 'completed' | TurnRecord {
  const turn = sameTurn(state, offer);
  const attempt = state.attemptsById[offer.attemptId];
  if (!attemptSettled(attempt)
      || (turn.currentAttemptId === offer.attemptId && LIVE.has(turn.status))) {
    return 'pending';
  }
  /* Snapshot/terminal events may briefly expose only one half of the join.
   * Never mistake a different successor for the exact stopped Attempt. */
  if (turn.currentAttemptId !== offer.attemptId) return 'pending';
  if (attempt!.status === 'completed' || turn.status === 'completed') return 'completed';
  if (!RECOVERABLE.has(turn.status)) {
    throw new Error('The original Attempt did not settle into a recoverable state.');
  }
  return turn;
}

function firstOfferedOperation(turn: TurnRecord): TurnResumeOption {
  const options = turn.settlement?.resumeOptions;
  if (!Array.isArray(options) || !options.length) {
    throw new Error('The settled Turn has no server-offered recovery operation.');
  }
  const first = options[0];
  if (!first || !['continue', 'checkpoint_resume', 'regenerate'].includes(first.operation)) {
    throw new Error('The first recovery operation is not an ordinary resume or regenerate.');
  }
  if (first.operation !== 'regenerate'
      && (!first.anchor || typeof first.anchor !== 'object' || Array.isArray(first.anchor))) {
    throw new Error('The resume offer is missing its exact server anchor.');
  }
  return first;
}

export interface ExplicitFallbackRetryOffer {
  conversationId: string;
  turnId: string;
  model: string;
}

export function createExplicitFallbackRecovery(
  ports: ExplicitFallbackRecoveryPorts,
): {
  use(offer: ExplicitFallbackOffer): Promise<void>;
  retry(): Promise<void>;
  retryOffer(): ExplicitFallbackRetryOffer | null;
  readonly pending: boolean;
} {
  let pending = false;
  let uncertain: ExplicitFallbackOperation | null = null;

  async function replayUncertain(offer: ExplicitFallbackOffer): Promise<boolean> {
    if (!uncertain) return false;
    const submitted = uncertain;
    const state = ports.readState(submitted.conversationId);
    if (!state || !state.turnsById[submitted.turnId]) return true;
    try {
      await ports.hydrate(offer.conversationId);
      if (ports.hasAuthoritativeCommand(offer.conversationId, submitted.commandId)) {
        uncertain = null;
        return true;
      }
      await ports.operateAttempt(submitted);
      uncertain = null;
    } catch (error) {
      ports.reportError(error);
    }
    return true;
  }

  async function run(offer: ExplicitFallbackOffer): Promise<void> {
    const controller = new AbortController();
    const { signal } = controller;
    let unsubscribe: (() => void) | undefined;
    let hydrated: Promise<unknown> | null = null;
    const hydrate = async (): Promise<void> => {
      hydrated ??= Promise.resolve(ports.hydrate(offer.conversationId));
      await hydrated;
      hydrated = null;
    };
    const state = (): TurnState => {
      const value = ports.readState(offer.conversationId);
      if (!value) throw new Error('The authoritative Turn state is unavailable.');
      return value;
    };
    try {
      const target = await ports.prepareTarget(offer, state(), signal);
      if (signal.aborted) return;
      try {
        await ports.abortAttempt(offer.attemptId);
      } catch (error) {
        throw new Error(`Stop was not confirmed; the original Attempt was left alone: ${
          errorMessage(error, 'request failed')}`);
      }
      await new Promise<void>((resolve, reject) => {
        const inspect = (): void => {
          if (signal.aborted) return;
          try {
            if (settlement(state(), offer) === 'pending') return;
            resolve();
          } catch (error) {
            reject(error);
          }
        };
        unsubscribe = ports.subscribe(offer.conversationId, inspect);
        inspect();
        void hydrate().then(inspect).catch(reject);
      });
      if (signal.aborted) return;
      if (settlement(state(), offer) === 'completed') return;

      const turn = sameTurn(state(), offer);
      const operation = firstOfferedOperation(turn).operation;
      const initial = operation === 'regenerate'
        ? ports.regenerateInputUpdate(turn, state(), signal)
        : ports.contextInputUpdate(turn, state(), signal);
      const submitted: ExplicitFallbackOperation = {
        conversationId: offer.conversationId,
        turnId: offer.turnId,
        operation,
        commandId: ports.commandId(),
        config: target.config,
        ...initial,
        ...(operation === 'regenerate' ? {} : {
          resumeAnchor: { ...firstOfferedOperation(turn).anchor },
        }),
      };

      // A lost ACK is the one ambiguous command boundary. Hydrate first, then
      // either adopt the committed command or replay its unchanged envelope.
      if (ports.hasAuthoritativeCommand(offer.conversationId, submitted.commandId)) return;
      uncertain = submitted;
      try {
        await ports.operateAttempt(submitted);
        uncertain = null;
        return;
      } catch (error) {
        // Hydration separates a committed ACK from an uncertain one. Replay
        // the identical command; never mint a new command or successor here.
        await hydrate();
        if (ports.hasAuthoritativeCommand(offer.conversationId, submitted.commandId)) {
          uncertain = null;
          return;
        }
        await ports.operateAttempt(submitted);
        uncertain = null;
      }
    } finally {
      const dispose = unsubscribe as (() => void) | undefined;
      dispose?.();
    }
  }

  return {
    get pending() { return pending; },
    retryOffer() {
      return uncertain ? {
        conversationId: uncertain.conversationId,
        turnId: uncertain.turnId,
        model: String(uncertain.config.model || ''),
      } : null;
    },
    async use(offer) {
      if (pending) return;
      pending = true;
      try {
        await run(offer);
      } catch (error) {
        ports.reportError(error);
      } finally {
        pending = false;
      }
    },
    async retry() {
      if (pending || !uncertain) return;
      pending = true;
      try {
        await replayUncertain({
          conversationId: uncertain.conversationId,
          turnId: uncertain.turnId,
          attemptId: '', projectionRevision: 0, currentModel: '', model: '',
        });
      } finally {
        pending = false;
      }
    },
  };
}
