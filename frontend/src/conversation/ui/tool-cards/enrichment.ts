/**
 * Facts a tool card shows after its first paint: which Worker conversation an
 * assignment belongs to, that conversation's title when the loaded catalog
 * does not hold it (the title names the task) and display translations
 * of model-authored free text. Each fact is requested on first demand,
 * settles once per page, and tells subscribed cards to repaint.
 *
 * Entry point: createToolCardEnrichment. Dependencies: the async lookup and
 * translation ports only; no DOM, transport or durable state.
 */

export interface ToolCardEnrichmentPorts {
  /** The Worker conversation of an assignment, '' when the record names none. */
  readonly assignmentConversation?: (assignmentId: string) => Promise<string>;
  /** A conversation's title when the loaded catalog does not hold it (an older Worker page), '' when untitled. */
  readonly lookupConversationTitle?: (conversationId: string) => Promise<string>;
  /** Display language of the conversation's translation setting, '' when it is off. */
  readonly translationTarget?: () => string;
  /** The text in `target`; returning the source unchanged means it needs no translation. */
  readonly translateText?: (source: string, target: string) => Promise<string>;
}

/** A lookup that has not settled yet, or its settled value ('' when there is none). */
export type EnrichedFact = { readonly settled: false } | { readonly settled: true; readonly value: string };

export interface ToolCardEnrichment {
  /** null when no lookup port exists, so callers keep their own fallback. */
  assignmentConversation(assignmentId: string): EnrichedFact | null;
  /** null when no title lookup port exists. */
  conversationTitle(conversationId: string): EnrichedFact | null;
  /** null when translation is off for the conversation. */
  translation(source: string): EnrichedFact | null;
  subscribe(listener: () => void): () => void;
  /** A setting the facts depend on changed (translation toggle, debug mode). */
  refresh(): void;
}

const PENDING: EnrichedFact = Object.freeze({ settled: false });

export function createToolCardEnrichment(ports: ToolCardEnrichmentPorts): ToolCardEnrichment {
  const listeners = new Set<() => void>();
  const facts = new Map<string, EnrichedFact>();
  const notify = (): void => { for (const listener of [...listeners]) listener(); };

  function fact(key: string, load: () => Promise<string>): EnrichedFact {
    const known = facts.get(key);
    if (known) return known;
    facts.set(key, PENDING);
    void load().catch(() => '').then((value) => {
      facts.set(key, Object.freeze({ settled: true, value }));
      notify();
    });
    return PENDING;
  }

  return {
    assignmentConversation(assignmentId) {
      const lookup = ports.assignmentConversation;
      if (!lookup || !assignmentId) return null;
      return fact(`assignment\u0000${assignmentId}`, () => lookup(assignmentId));
    },
    conversationTitle(conversationId) {
      const lookup = ports.lookupConversationTitle;
      if (!lookup || !conversationId) return null;
      return fact(`title\u0000${conversationId}`, async () => (await lookup(conversationId)).trim());
    },
    translation(source) {
      const target = ports.translationTarget?.() ?? '';
      const translate = ports.translateText;
      const original = source.trim();
      if (!target || !translate || !original) return null;
      return fact(`translation\u0000${target}\u0000${original}`, async () => {
        const translated = (await translate(original, target)).trim();
        return translated === original ? '' : translated;
      });
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    refresh: notify,
  };
}
