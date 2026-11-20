/** The office's per-conversation journal: the pages of one person's deliverables projection
 * (GET /api/v4/project/coordination/deliverables; docs/PROJECT_COORDINATION.md "Lead deliverables") that the
 * brief's progress timeline and files are projected from. Reads start when a panel asks for a person, older
 * pages append on request, and the project channel's `lead_deliverable` hint re-reads the first page of the
 * conversations already read (a re-read first page replaces the items it covers, older pages stay). Each
 * conversation has at most one read in flight plus one pending re-read: hints coalesce, never queue.
 * Entry point: createJournals. Dependencies: the live deliverables source, the shared item merge. */
import { appendDeliverables, type LeadDeliverable, type LeadDeliverablePage } from '../lead-deliverables/model';
import type { DeliverablesSource } from './live';

export type JournalStatus = 'loading' | 'ok' | 'error' | 'unavailable';
export interface Journal {
  status: JournalStatus;
  items: ReadonlyArray<LeadDeliverable>;
  /** Board task titles as the lead's board displays them (its translation), by task key. */
  taskTitles: ReadonlyMap<string, string>;
  nextCursor: string | null;
  loadingMore: boolean;
}

interface Entry { path: string; journal: Journal; pages: number; inFlight: boolean; again: boolean }

const UNAVAILABLE: Journal = { status: 'unavailable', items: [], taskTitles: new Map(), nextCursor: null, loadingMore: false };

function titlesOf(page: LeadDeliverablePage, previous: ReadonlyMap<string, string>): Map<string, string> {
  const titles = new Map(previous);
  for (const task of page.tasks) titles.set(task.key, task.translation?.title || task.title);
  return titles;
}

export function createJournals(source: DeliverablesSource | undefined, onChange: () => void) {
  const entries = new Map<string, Entry>();
  const lifetime = new AbortController();

  function readFirst(id: string, entry: Entry) {
    if (!source) return;
    if (entry.inFlight) { entry.again = true; return; }
    entry.inFlight = true;
    void source.read(id, entry.path, null, lifetime.signal).then(page => {
      // The re-read first page covers everything down to its oldest item; older pages already read stay.
      const extended = entry.pages > 1;
      const oldest = page.items.length ? page.items[page.items.length - 1].createdAt : Infinity;
      entry.journal = {
        status: 'ok',
        items: appendDeliverables(page.items, extended ? entry.journal.items.filter(item => item.createdAt <= oldest) : []),
        taskTitles: titlesOf(page, entry.journal.taskTitles),
        nextCursor: extended ? entry.journal.nextCursor : page.nextCursor,
        loadingMore: entry.journal.loadingMore,
      };
      entry.pages = Math.max(entry.pages, 1);
    }, () => {
      if (!lifetime.signal.aborted && entry.journal.status !== 'ok') entry.journal = { ...entry.journal, status: 'error' };
    }).finally(() => {
      entry.inFlight = false;
      if (lifetime.signal.aborted) return;
      onChange();
      if (entry.again) { entry.again = false; readFirst(id, entry); }
    });
  }

  const release = source?.subscribe(conversationId => {
    for (const [id, entry] of entries) if (!conversationId || id === conversationId) readFirst(id, entry);
  });

  return {
    /** The journal of one conversation, read on first ask. */
    get(conversationId: string, projectPath: string): Journal {
      if (!source) return UNAVAILABLE;
      let entry = entries.get(conversationId);
      if (!entry) {
        entry = { path: projectPath, pages: 0, inFlight: false, again: false,
          journal: { status: 'loading', items: [], taskTitles: new Map(), nextCursor: null, loadingMore: false } };
        entries.set(conversationId, entry);
        readFirst(conversationId, entry);
      } else if (entry.journal.status === 'error' && !entry.inFlight) {
        readFirst(conversationId, entry);
      }
      return entry.journal;
    },
    /** Append the next older page. */
    more(conversationId: string) {
      const entry = entries.get(conversationId);
      const cursor = entry?.journal.nextCursor;
      if (!source || !entry || !cursor || entry.journal.loadingMore) return;
      entry.journal = { ...entry.journal, loadingMore: true };
      onChange();
      void source.read(conversationId, entry.path, cursor, lifetime.signal).then(page => {
        entry.journal = { ...entry.journal, items: appendDeliverables(entry.journal.items, page.items),
          taskTitles: titlesOf(page, entry.journal.taskTitles), nextCursor: page.nextCursor };
        entry.pages += 1;
      }, () => undefined).finally(() => {
        if (lifetime.signal.aborted) return;
        entry.journal = { ...entry.journal, loadingMore: false };
        onChange();
      });
    },
    dispose() {
      lifetime.abort();
      release?.();
      entries.clear();
    },
  };
}
export type Journals = ReturnType<typeof createJournals>;
