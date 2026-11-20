/** Each task's reports and files on the Coordinator's task board (owner request 2026-11-17).
 *
 * A task row expands to the reports that named it (report_to_user's task) and the files they
 * attached; tasks that left the board but have deliverables stay reachable as earlier tasks, and
 * everything of no task of this project (older unlinked reports, files published on their own,
 * reports the Global Router relayed from other projects) reads under one "other" group. All reads
 * are the lead's deliverables projection (GET /api/v4/project/coordination/deliverables): every page
 * summarises the items per task, and a task's list is that task's pages, newest first, older pages
 * on demand. A `lead_deliverable` push for this conversation and a reconnect re-read the summary and
 * each open list; nothing polls. Opening an item goes through the shared opener, loaded on first use.
 *
 * Entry point: createTaskDeliverables. Dependencies: the read and push ports, i18n, ui and the
 * lead-deliverables row and opener.
 */
import { t } from '../i18n';
import { createButton } from '../ui';
import { apiFailure } from '../api/errors';
import type { LeadDeliverable, LeadDeliverablePage } from '../api/project-coordination.generated';
import { appendDeliverables } from './lead-deliverables/model';
import { deliverableRow } from './lead-deliverables/rows';
import type { DeliverableOpener } from './lead-deliverables/open';
import '../styles/application/36-lead-deliverables.css';

export const OTHER_TASKS = 'other';

export interface TaskDeliverablesPorts {
  read(query: { task?: string; cursor?: string }, signal: AbortSignal): Promise<LeadDeliverablePage>;
  /** A newer board revision can move a task or create an automatic board note. */
  subscribeBoard?(changed: () => void): () => void;
  /** Project push frames and reconnects; returns the release. */
  subscribe(changed: (frame: { kind?: unknown; conversationId?: unknown }) => void): () => void;
  conversationId: string;
  /** The lead's own project: items of other projects show their project. */
  projectPath: string;
  opener?(): Promise<DeliverableOpener>;
  now?(): number;
}

export type TaskDeliverableSummary = LeadDeliverablePage['tasks'][number];

export interface TaskDeliverables {
  /** Items of a task key (or OTHER_TASKS) from the latest summary; 0 before it loads. */
  count(key: string): number;
  /** Tasks with deliverables, newest first. */
  tasks(): readonly TaskDeliverableSummary[];
  expanded(key: string): boolean;
  toggle(key: string): void;
  /** Start reading a key's list before it is opened (pointer or keyboard approach). */
  prefetch(key: string): void;
  /** The retained list region of an expanded key. */
  region(key: string): HTMLElement;
  destroy(): void;
}

interface TaskList {
  items: LeadDeliverable[];
  cursor: string | null;
  firstCursor: string | null | undefined;
  cursorEnd: string | null;
  state: 'idle' | 'loading' | 'ok' | 'error';
  flight: AbortController | null;
  again: boolean;
  region: HTMLElement;
}

/* The latest summary of each project's lead, so a board mounted again (a project switch back)
 * paints its counts at once while its fresh summary reads. One entry per project. */
const lastSummaries = new Map<string, { conversationId: string; tasks: TaskDeliverableSummary[]; otherCount: number }>();

async function defaultOpener(): Promise<DeliverableOpener> {
  const module = await import('./lead-deliverables/open');
  return module.createDeliverableOpener(module.runtimeOpenPorts());
}

export function createTaskDeliverables(ports: TaskDeliverablesPorts, changed: () => void): TaskDeliverables {
  const now = ports.now || (() => Date.now());
  let opener: Promise<DeliverableOpener> | null = null;
  const openerPort = () => (opener ??= (ports.opener || defaultOpener)());
  const remembered = lastSummaries.get(ports.projectPath);
  const resumed = remembered?.conversationId === ports.conversationId ? remembered : undefined;
  let summary: TaskDeliverableSummary[] = resumed ? resumed.tasks : [];
  let byKey = new Map(summary.map(task => [task.key, task]));
  let otherCount = resumed ? resumed.otherCount : 0;
  let summaryFlight: AbortController | null = null;
  let alive = true;
  const lists = new Map<string, TaskList>();
  const open = new Set<string>();

  function adoptSummary(page: LeadDeliverablePage) {
    const next = JSON.stringify([page.tasks, page.otherCount]);
    if (next === JSON.stringify([summary, otherCount])) return;
    summary = page.tasks.map(task => ({ ...task }));
    byKey = new Map(summary.map(task => [task.key, task]));
    otherCount = page.otherCount;
    lastSummaries.set(ports.projectPath, { conversationId: ports.conversationId, tasks: summary, otherCount });
    changed();
  }

  async function readSummary() {
    summaryFlight?.abort();
    const controller = new AbortController();
    summaryFlight = controller;
    try {
      const page = await ports.read({ task: OTHER_TASKS }, controller.signal);
      if (alive && summaryFlight === controller) adoptSummary(page);
    } catch (error) {
      // The board stays usable without counts; the next push or reconnect reads again.
      if (!controller.signal.aborted) console.warn('[TaskDeliverables] summary read failed', apiFailure(error).message);
    } finally {
      if (summaryFlight === controller) summaryFlight = null;
    }
  }

  function list(key: string): TaskList {
    let entry = lists.get(key);
    if (!entry) {
      const region = document.createElement('div');
      region.className = 'coordinator-task-deliverables';
      region.dataset.task = key;
      region.setAttribute('role', 'region');
      entry = { items: [], cursor: null, firstCursor: undefined, cursorEnd: null,
        state: 'idle', flight: null, again: false, region };
      lists.set(key, entry);
    }
    return entry;
  }

  async function load(key: string, first: boolean) {
    const entry = list(key);
    if (entry.flight) {
      if (first) entry.again = true;
      return;
    }
    const controller = new AbortController();
    entry.flight = controller;
    entry.state = 'loading';
    paint(key);
    try {
      const page = await ports.read({ task: key, ...(first ? {} : entry.cursor ? { cursor: entry.cursor } : {}) },
        controller.signal);
      if (!alive || entry.flight !== controller) return;
      if (first) {
        // A re-read first page keeps the older pages already shown; item keys never move.
        if (entry.firstCursor === undefined || entry.firstCursor !== page.nextCursor) {
          entry.cursor = page.nextCursor;
          entry.cursorEnd = page.items.at(-1)?.id ?? null;
        }
        entry.firstCursor = page.nextCursor;
        const summary = page.tasks.find(task => task.key === key);
        const pageTail = page.nextCursor ? page.items.at(-1)?.createdAt : undefined;
        // A synthetic note can disappear on reopen or be replaced by a linked delivery.
        // Retain one outside the refreshed interval only if the summary doesn't supersede it.
        const older = entry.items.filter(item => item.kind !== 'completion' || (summary && !summary.hasDeliveryReport
          && pageTail !== undefined && item.createdAt <= pageTail));
        const retainedIds = new Set([...page.items, ...older].map(item => item.id));
        if (entry.cursorEnd && !retainedIds.has(entry.cursorEnd)) {
          // Removing a completion anchor must not move its continuation beyond retained older rows.
          const end = entry.items.findIndex(item => item.id === entry.cursorEnd);
          entry.cursorEnd = entry.items.slice(0, end).reverse().find(item => retainedIds.has(item.id))?.id ?? null;
        }
        entry.items = appendDeliverables(page.items, older);
      } else {
        // A refreshed head can leave retained older rows beyond this continuation's interval.
        const end = entry.items.findIndex(item => item.id === entry.cursorEnd) + 1;
        const throughPage = appendDeliverables(entry.items.slice(0, end), page.items);
        entry.items = appendDeliverables(throughPage, entry.items.slice(end));
        entry.cursor = page.nextCursor;
        entry.cursorEnd = page.items.at(-1)?.id ?? entry.cursorEnd;
      }
      entry.state = 'ok';
      adoptSummary(page);
    } catch (error) {
      if (controller.signal.aborted || entry.flight !== controller) return;
      entry.state = 'error';
      entry.region.dataset.error = apiFailure(error).message;
    } finally {
      if (entry.flight === controller) {
        entry.flight = null;
        paint(key);
        if (entry.again) { entry.again = false; void load(key, true); }
      }
    }
  }

  function paint(key: string) {
    const entry = list(key);
    const other = key === OTHER_TASKS;
    const keep = new Map<string, HTMLLIElement>();
    const rows = entry.region.querySelector<HTMLOListElement>(':scope > .coordinator-task-deliverables__list')
      ?? Object.assign(document.createElement('ol'), { className: 'coordinator-task-deliverables__list' });
    for (const node of rows.querySelectorAll<HTMLLIElement>(':scope > .lead-deliverable')) {
      if (!node.dataset.deliverableId?.startsWith('completion:')) keep.set(node.dataset.deliverableId || '', node);
    }
    rows.replaceChildren(...entry.items.map(item => keep.get(item.id) || deliverableRow(item, {
      opener: openerPort, now,
      showProject: other && !!item.projectPath && item.projectPath !== ports.projectPath,
    })));
    const status = document.createElement('p');
    status.className = 'coordinator-task-deliverables__status';
    status.setAttribute('role', 'status');
    status.textContent = entry.state === 'loading' && !entry.items.length ? t('leadDeliverables.loading')
      : entry.state === 'error' ? t('leadDeliverables.error')
        : entry.state === 'ok' && !entry.items.length ? t('leadDeliverables.empty') : '';
    const controls: Node[] = [];
    if (entry.cursor && entry.state === 'ok') {
      controls.push(createButton({ label: t('leadDeliverables.more'), variant: 'ghost', size: 'sm',
        onClick: () => void load(key, false) }));
    }
    if (entry.state === 'error') {
      controls.push(createButton({ label: t('leadDeliverables.retry'), variant: 'ghost', size: 'sm',
        onClick: () => void load(key, !entry.items.length) }));
    }
    entry.region.replaceChildren(rows, ...(status.textContent ? [status] : []), ...controls);
  }

  function refresh() {
    void readSummary();
    for (const key of open) void load(key, true);
    // A list read ahead but not open is stale now: its opening reads it again.
    for (const [key, entry] of lists) if (!open.has(key) && (entry.flight || entry.state === 'ok')) {
      entry.flight?.abort();
      entry.flight = null;
      entry.again = false;
      entry.state = 'idle';
    }
  }
  const release = ports.subscribe(frame => {
    if (frame && frame.kind !== undefined && (frame.kind !== 'lead_deliverable'
        || frame.conversationId !== ports.conversationId)) return;
    refresh();
  });
  const releaseBoard = ports.subscribeBoard?.(refresh);
  // Rows are kept across paints, so a language switch rebuilds the open lists in the new language.
  const relabel = () => {
    for (const key of open) {
      list(key).region.querySelector(':scope > .coordinator-task-deliverables__list')?.replaceChildren();
      paint(key);
    }
  };
  window.addEventListener('tofu:language-change', relabel);
  void readSummary();

  return {
    count: key => (key === OTHER_TASKS ? otherCount : byKey.get(key)?.count ?? 0),
    tasks: () => summary,
    expanded: key => open.has(key),
    toggle(key) {
      if (open.has(key)) open.delete(key);
      else {
        open.add(key);
        const entry = list(key);
        if (entry.state === 'idle' || entry.state === 'error') void load(key, true);
        else paint(key);
      }
      changed();
    },
    prefetch(key) {
      const entry = list(key);
      if (entry.state === 'idle') void load(key, true);
    },
    region: key => list(key).region,
    destroy() {
      alive = false;
      release();
      releaseBoard?.();
      window.removeEventListener('tofu:language-change', relabel);
      summaryFlight?.abort();
      for (const entry of lists.values()) entry.flight?.abort();
    },
  };
}
