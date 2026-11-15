/** Office facts (GAME-LINK-1; docs/PROJECT_COORDINATION.md "Office team facts"): one owner-scoped snapshot,
 * GET /api/v4/router/office, then ordered `office.fact` deltas on the push channel `office`. A delta applies
 * only with the snapshot's streamId and seq == last + 1. A frame from another stream (the server restarted)
 * or one that skips a number (a frame was lost) asks for a fresh snapshot, as does every push reconnect; a
 * frame at or below the current seq on the same stream is already reflected and is ignored. Frames that
 * arrive while a snapshot is being read are held and replayed on it. Facts carry identifiers, never content:
 * titles, results and reports are read from their own endpoints by these ids.
 * Pure. Entry points: createFactStore, changedFacts, factsReplaceHints, factQuestions, busyConversations,
 * openAlarms. */
import type { RouterOfficeFact, RouterOfficeFacts } from '../../api/router.generated';
import type { OpenQuestion } from './attention';

export type Fact = RouterOfficeFact;
export type FactCoverage = RouterOfficeFacts['coverage'];
export interface FactFrame extends Fact { streamId: string; seq: number; state: string }
export type FrameOutcome =
  | { kind: 'applied'; fact: Fact; state: 'set' | 'cleared'; previous: Fact | null }
  | { kind: 'stale' }
  | { kind: 'resnapshot' };
/** What the scene reads: the current facts and how completely each kind is covered. */
export interface FactsView { facts: ReadonlyMap<string, Fact>; coverage: FactCoverage }

function factOf(frame: FactFrame): Fact {
  const { streamId: _stream, seq: _seq, state: _state, ...rest } = frame as FactFrame & { type?: unknown; userId?: unknown };
  const { type: _type, userId: _user, ...fact } = rest as typeof rest & { type?: unknown; userId?: unknown };
  return fact as Fact;
}

export function createFactStore() {
  let streamId = '';
  let seq = 0;
  let coverage: FactCoverage | null = null;
  let facts = new Map<string, Fact>();
  return {
    reset(snapshot: RouterOfficeFacts) {
      streamId = snapshot.streamId;
      seq = snapshot.seq;
      coverage = snapshot.coverage;
      facts = new Map(snapshot.facts.map(fact => [fact.key, fact as Fact]));
    },
    apply(frame: FactFrame): FrameOutcome {
      if (!coverage || frame.streamId !== streamId || frame.seq > seq + 1) return { kind: 'resnapshot' };
      if (frame.seq <= seq) return { kind: 'stale' };
      seq = frame.seq;
      const previous = facts.get(frame.key) ?? null;
      const fact = factOf(frame);
      if (frame.state === 'cleared') facts.delete(frame.key);
      else facts.set(frame.key, fact);
      return { kind: 'applied', fact, state: frame.state === 'cleared' ? 'cleared' : 'set', previous };
    },
    view(): FactsView | null { return coverage ? { facts, coverage } : null; },
    clear() { coverage = null; facts = new Map(); streamId = ''; seq = 0; },
  };
}
export type FactStore = ReturnType<typeof createFactStore>;

/** What a fresh snapshot changed against the facts held before it (a lost frame or a server restart):
 * each new or different fact as set, each vanished one as cleared, so its reads still happen. */
export function changedFacts(before: ReadonlyMap<string, Fact>, after: ReadonlyMap<string, Fact>): Array<{ fact: Fact; state: 'set' | 'cleared' }> {
  const changes: Array<{ fact: Fact; state: 'set' | 'cleared' }> = [];
  for (const [key, fact] of after) {
    const old = before.get(key);
    if (!old || JSON.stringify(old) !== JSON.stringify(fact)) changes.push({ fact, state: 'set' });
  }
  for (const [key, fact] of before) if (!after.has(key)) changes.push({ fact, state: 'cleared' });
  return changes;
}

/** Facts replace push hints only when they carry every change a hint would read: busy edges completely,
 * and reports, owner questions and boards as frames. */
export function factsReplaceHints(view: FactsView | null): boolean {
  if (!view || view.coverage.busy !== 'complete') return false;
  return (['reports', 'decisions', 'boards'] as const)
    .every(kind => view.coverage[kind] === 'complete' || view.coverage[kind] === 'newest_page');
}

/** Conversations with a running attempt, when busy facts are complete. */
export function busyConversations(view: FactsView | null): ReadonlySet<string> | null {
  if (!view || view.coverage.busy !== 'complete') return null;
  const busy = new Set<string>();
  for (const fact of view.facts.values()) if (fact.fact === 'busy') busy.add(fact.conversationId);
  return busy;
}

/** A lead's open owner questions from decision facts (the whole owner-question index, no Turn window). Text
 * comes from the lead's tail read when it holds the same card; otherwise it stays empty for the panel to
 * fill, and the identifiers still open the exact Turn. */
export function factQuestions(view: FactsView | null, leadId: string, known: ReadonlyArray<OpenQuestion> | null): OpenQuestion[] | null {
  if (!view || view.coverage.decisions !== 'complete') return null;
  const text = new Map((known || []).map(question => [question.guidanceId, question]));
  const open: OpenQuestion[] = [];
  for (const fact of view.facts.values()) {
    if (fact.fact !== 'decision' || fact.conversationId !== leadId || !fact.guidanceId || !fact.turnId) continue;
    const card = text.get(fact.guidanceId);
    open.push({
      conversationId: leadId, turnId: fact.turnId, ordinal: fact.ordinal ?? card?.ordinal ?? 0, guidanceId: fact.guidanceId,
      blocking: fact.blocking ?? card?.blocking ?? true, question: card?.question || '', defaultText: card?.defaultText || '',
      createdAt: card?.createdAt || 0,
    });
  }
  return open;
}

/** Open Watchdog incidents, when alarm facts are complete. */
export function openAlarms(view: FactsView | null): number | null {
  if (!view || view.coverage.alarms !== 'complete') return null;
  let count = 0;
  for (const fact of view.facts.values()) if (fact.fact === 'alarm') count += 1;
  return count;
}
