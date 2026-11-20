/** Owns transient Stop requests, never Turn settlement or queue state. */
import type { AbortAttemptResponse } from '../api/conversation-sync.generated';
import type { RuntimeConversation } from './turn-runtime-contracts';
import { activeTurnAttemptId, activeTurnAttempts, type TurnState } from './turn-state';

const TURN_STOP_CONFIRMATION_MS = 4000;
export type AttemptStopState = 'stopping' | 'refresh' | null;

interface StopTarget {
  attemptId: string;
  turnId: string;
  acknowledged?: boolean;
  refreshable?: boolean;
  scheduled?: boolean;
  discard(): void;
}

interface StopFeedbackPorts {
  readState(conversation: RuntimeConversation): TurnState | null;
  abortAttempt(attemptId: string): Promise<AbortAttemptResponse | null>;
  refresh(conversation: RuntimeConversation): void;
  changed(conversation: RuntimeConversation, unconfirmed?: true): void;
}

function isLive(state: TurnState | null, target: StopTarget): boolean {
  return activeTurnAttemptId(state?.turnsById[target.turnId]) === target.attemptId;
}

function projectStopState(targets: ReadonlyMap<string, StopTarget>, active: ReadonlyMap<string, string>): AttemptStopState {
  let disposition: AttemptStopState = active.size ? 'refresh' : 'stopping';
  for (const attemptId of active.keys()) {
    const target = targets.get(attemptId);
    if (!target) return null; // New active work must still have a Stop button.
    if (!target.refreshable) disposition = 'stopping';
  }
  return disposition;
}

export function createTurnStopFeedback(ports: StopFeedbackPorts) {
  const owned = new WeakMap<RuntimeConversation, Map<string, StopTarget>>();
  return {
    state(conversation: RuntimeConversation): AttemptStopState {
      const targets = owned.get(conversation);
      return targets?.size ? projectStopState(targets, activeTurnAttempts(ports.readState(conversation))) : null;
    },
    request(conversation: RuntimeConversation): void {
      const read = () => ports.readState(conversation);
      const targets = owned.get(conversation) ?? new Map<string, StopTarget>();
      const active = activeTurnAttempts(read());
      const requested = [...active].filter(([attemptId]) => !targets.has(attemptId));
      if (!requested.length) {
        if (!targets.size || projectStopState(targets, active) === 'refresh') {
          ports.refresh(conversation);
        }
        return;
      }
      let remaining = requested.length;
      let warned = false;
      const changed = (unconfirmed: boolean) => {
        const firstWarning = unconfirmed && !warned;
        warned ||= unconfirmed;
        ports.changed(conversation, firstWarning || undefined);
      };
      const ownsLive = (target: StopTarget) => targets.get(target.attemptId) === target && isLive(read(), target);
      const deadline = setTimeout(() => {
        const live = entries.filter(ownsLive);
        if (!live.length) return;
        let uncertain = false;
        for (const target of live) {
          if (target.acknowledged) target.refreshable = true;
          else { target.discard(); uncertain = true; }
        }
        changed(uncertain);
        if (read()) ports.refresh(conversation);
      }, TURN_STOP_CONFIRMATION_MS);
      const entries = requested.map(([attemptId, turnId]): StopTarget => {
        const target = { attemptId, turnId, discard() {
          if (targets.get(attemptId) !== target) return;
          targets.delete(attemptId);
          if (!--remaining) clearTimeout(deadline);
        } };
        targets.set(attemptId, target);
        return target;
      });
      owned.set(conversation, targets);
      changed(false);
      for (const target of entries) {
        if (!ownsLive(target)) continue;
        const fail = () => {
          if (!ownsLive(target)) return;
          target.discard();
          changed(true);
        };
        void ports.abortAttempt(target.attemptId).then(result => {
          // Queue cancellation can lose a race to dispatch despite an OK envelope.
          if (result?.ok && (result.status !== 'cancelled' || result.cancelled)) target.acknowledged = true;
          else fail();
        }, fail);
      }
    },
    ended(conversation: RuntimeConversation, state: TurnState): StopTarget[] {
      const targets = owned.get(conversation);
      return targets?.size ? [...targets.values()].filter(target => !isLive(state, target)) : [];
    },
    discardEnded(state: TurnState, targets: StopTarget[]): void {
      for (const target of targets) if (!isLive(state, target)) target.discard();
    },
    dispose(conversation: RuntimeConversation): void {
      for (const target of owned.get(conversation)?.values() ?? []) target.discard();
      owned.delete(conversation);
    },
  };
}
