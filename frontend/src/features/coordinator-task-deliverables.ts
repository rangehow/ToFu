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
  /** Project push frames and reconnects; returns the release. */
  subscribe(changed: (frame: { kind?: unknown; conversationId?: unknown }) => void): () => void;
  conversationId: string;
  /** The lead's own project: items of other projects show their project. */
  projectPath: string;
  opener?(): Promise<DeliverableOpener>;
  now?(): number;
}

export interface TaskDeliverableSummary { key: string; title: string; count: number; latestAt: number }

export interface TaskDeliverables {
  /** Items of a task key (or OTHER_TASKS) from the latest summary; 0 before it loads. */
  count(key: string): number;
  /** Tasks with deliverables, newest first. */
  tasks(): readonly TaskDeliverableSummary[];
  expanded(key: string): boolean;
  toggle(key: string): void;
  /** The retained list region of an expanded key. */
  region(key: string): HTMLElement;
  destroy(): void;
}

interface TaskList {
  items: LeadDeliverable[];
  cursor: string | null;
  state: 'idle' | 'loading' | 'ok' | 'error';
  flight: AbortController | null;
  again: boolean;
  region: HTMLElement;
}

async function defaultOpener(): Promise<DeliverableOpener> {
  const module = await import('./lead-deliverables/open');
  return module.createDeliverableOpener(module.runtimeOpenPorts());
}

export function createTaskDeliverables(ports: TaskDeliverablesPorts, changed: () => void): TaskDeliverables {
  const now = ports.now || (() => Date.now());
  let opener: Promise<DeliverableOpener> | null = null;
  const openerPort = () => (opener ??= (ports.opener || defaultOpener)());
  let summary: TaskDeliverableSummary[] = [];
  let otherCount = 0;
  let summaryFlight: AbortController | null = null;
  let alive = true;
  const lists = new Map<string, TaskList>();
  const open = new Set<string>();

  function adoptSummary(page: LeadDeliverablePage) {
    const next = JSON.stringify([page.tasks, page.otherCount]);
    if (next === JSON.stringify([summary, otherCount])) return;
    summary = page.tasks.map(task => ({ ...task }));
    otherCount = page.otherCount;
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
      entry = { items: [], cursor: null, state: 'idle', flight: null, again: false, region };
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
        if (!entry.items.length) entry.cursor = page.nextCursor;
        entry.items = appendDeliverables(page.items, entry.items);
      } else {
        entry.items = appendDeliverables(entry.items, page.items);
        entry.cursor = page.nextCursor;
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
      keep.set(node.dataset.deliverableId || '', node);
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

  const release = ports.subscribe(frame => {
    if (frame && frame.kind !== undefined && (frame.kind !== 'lead_deliverable'
        || frame.conversationId !== ports.conversationId)) return;
    void readSummary();
    for (const key of open) void load(key, true);
  });
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
    count: key => (key === OTHER_TASKS ? otherCount : summary.find(task => task.key === key)?.count ?? 0),
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
    region: key => list(key).region,
    destroy() {
      alive = false;
      release();
      window.removeEventListener('tofu:language-change', relabel);
      summaryFlight?.abort();
      for (const entry of lists.values()) entry.flight?.abort();
    },
  };
}
