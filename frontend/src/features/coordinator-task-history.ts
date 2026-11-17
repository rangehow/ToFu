/** Coordinator task history: every task the bound Coordinator's board ever showed.
 *
 * Opened from the task board's 历史任务 ("Task history") button, never implicitly.
 * The server pages the history (`progressHistoryPage`, docs/PROJECT_COORDINATION.md
 * "Task history"); this drawer shows it as a virtualized list: only the rows in view
 * exist in the DOM, each keyed by task and rebuilt only when its content changes. The
 * next page loads when the end of the loaded rows comes into view. While open, a newer
 * board revision re-reads the pages already loaded in place. Group filters and search
 * are server queries, so counts and pages always cover the whole history.
 *
 * Entry points: openCoordinatorTaskHistory (the drawer), createCoordinatorTaskHistory
 * (its content, for tests). Dependencies: the typed history read and board-change
 * subscription supplied by the caller; ui factories; the board's status glyph.
 */
import { createButton, createDrawer, createField } from '../ui';
import { t } from '../i18n';
import type {
  CoordinatorProgressHistoryGroup, CoordinatorProgressHistoryPage, CoordinatorProgressHistoryTask,
  CoordinatorProgressTranslation,
} from '../api/project-coordination.generated';
import { coordinatorProgressGlyph, coordinatorProgressText } from './coordinator-progress';
import {
  HISTORY_GROUPS, appendPage, historyRows, historyTotal, rowOffsets, rowSignature, visibleRange,
  type HistoryCounts, type HistoryFilter, type HistoryRow,
} from './coordinator-task-history-model';
import '../styles/application/34-coordinator-task-history.css';

export interface CoordinatorTaskHistoryQuery { group?: CoordinatorProgressHistoryGroup; q?: string; cursor?: string }

export interface CoordinatorTaskHistoryPorts {
  read(query: CoordinatorTaskHistoryQuery, signal: AbortSignal): Promise<CoordinatorProgressHistoryPage>;
  /** Calls `changed` for each newer board revision of this binding; returns the unsubscribe. */
  subscribe(changed: () => void): () => void;
  now?(): number;
}

export interface CoordinatorTaskHistoryView {
  el: HTMLElement;
  /** Re-lays out the visible window, e.g. once the drawer has a size. */
  paint(): void;
  destroy(): void;
}

/* Fixed row heights make the visible window a binary search; the stylesheet reads
 * them from these custom properties, so they are defined once, here. */
const ROW_HEIGHTS = { group: 36, task: 84 } as const;
const EMPTY_COUNTS: HistoryCounts = { in_progress: 0, pending: 0, dropped: 0, completed: 0, cancelled: 0 };

function node<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function timeFormatter() {
  const locale = document.documentElement.lang || undefined;
  const formats = {
    day: new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' }),
    year: new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
    older: new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'short', day: 'numeric' }),
  };
  return (at: number, now: number) => {
    const when = new Date(at), today = new Date(now);
    if (when.toDateString() === today.toDateString()) return formats.day.format(when);
    return (when.getFullYear() === today.getFullYear() ? formats.year : formats.older).format(when);
  };
}

const groupLabel = (filter: HistoryFilter) => t(`coordinatorHistory.group.${filter}`);

export function createCoordinatorTaskHistory(ports: CoordinatorTaskHistoryPorts): CoordinatorTaskHistoryView {
  const now = () => ports.now?.() ?? Date.now();
  const when = timeFormatter();
  const state = {
    filter: 'all' as HistoryFilter, query: '', tasks: [] as CoordinatorProgressHistoryTask[],
    counts: EMPTY_COUNTS, nextCursor: null as string | null, recorded: true,
    translation: undefined as CoordinatorProgressTranslation | undefined,
  };
  // A task on the board shows its board text, so the board's display translation covers it.
  const displayed = (text: string) => coordinatorProgressText(state.translation, text);
  let generation = 0, loading = false, loadingMore = false, failed = false, moreFailed = false, refreshQueued = false;
  let loaded = false, alive = true;
  let reading: AbortController | null = null, readingMore: AbortController | null = null;
  let rows: HistoryRow[] = [], offsets = rowOffsets([], ROW_HEIGHTS), taskPositions: number[] = [];
  const mounted = new Map<string, { el: HTMLElement; signature: string }>();

  const root = node('div', 'coordinator-history');
  root.style.setProperty('--coordinator-history-group-height', `${ROW_HEIGHTS.group}px`);
  root.style.setProperty('--coordinator-history-task-height', `${ROW_HEIGHTS.task}px`);
  const search = createField({ label: t('coordinatorHistory.search'), size: 'sm' });
  search.el.classList.add('coordinator-history-search');
  const input = search.control as HTMLInputElement;
  input.type = 'search';
  input.placeholder = t('coordinatorHistory.searchPlaceholder');
  input.autocomplete = 'off';
  const filters = node('div', 'coordinator-history-filters');
  filters.setAttribute('role', 'group');
  filters.setAttribute('aria-label', t('coordinatorHistory.filterLabel'));
  const chips = new Map<HistoryFilter, HTMLButtonElement>();
  for (const filter of ['all', ...HISTORY_GROUPS] as HistoryFilter[]) {
    const chip = createButton({ label: groupLabel(filter), size: 'sm', variant: 'ghost', onClick: () => choose(filter) });
    chip.dataset.historyFilter = filter;
    chips.set(filter, chip);
    filters.append(chip);
  }
  const status = node('div', 'coordinator-history-status');
  status.setAttribute('role', 'status');
  const viewport = node('div', 'coordinator-history-viewport');
  viewport.tabIndex = 0;
  viewport.setAttribute('aria-label', t('coordinatorHistory.title'));
  const content = node('div', 'coordinator-history-rows');
  content.setAttribute('role', 'feed');
  viewport.append(content);
  root.append(search.el, filters, status, viewport);

  function badge(kind: string, text: string) {
    const element = node('span', 'coordinator-history-badge', text);
    element.dataset.kind = kind;
    return element;
  }

  function taskRow(task: CoordinatorProgressHistoryTask): HTMLElement {
    const at = now();
    const row = node('article', 'coordinator-history-task');
    row.dataset.status = task.status;
    if (task.dropped) row.dataset.dropped = 'true';
    const line = node('div', 'coordinator-history-task-line');
    const glyph = coordinatorProgressGlyph(task.status);
    const title = node('span', 'coordinator-history-task-title', displayed(task.title));
    title.id = `coordinator-history-${task.key}`;
    row.setAttribute('aria-labelledby', title.id);
    line.append(glyph, title);
    if (task.onBoard) line.append(badge('board', t('coordinatorHistory.onBoard')));
    if (task.dropped) line.append(badge('dropped', groupLabel('dropped')));
    const note = node('div', 'coordinator-history-task-note', displayed(task.progress ?? ''));
    const facts = [t('coordinatorHistory.firstSeen', { time: when(task.firstSeenAt, at) }),
      t(task.onBoard ? 'coordinatorHistory.updated' : 'coordinatorHistory.lastSeen', { time: when(task.onBoard ? task.changedAt : task.lastSeenAt, at) })];
    if (task.formerTitles?.length) facts.push(t('coordinatorHistory.formerTitles', { titles: task.formerTitles.map(displayed).join(' / ') }));
    // The promise leads the time facts, so it stays visible however narrow the row.
    const meta = node('div', 'coordinator-history-task-meta');
    if (task.dueOutcome && task.dueAt !== undefined) {
      meta.append(badge(`due-${task.dueOutcome}`, t(`coordinatorHistory.due.${task.dueOutcome}`, { time: when(task.dueAt, at) })));
    }
    meta.append(node('span', 'coordinator-history-task-facts', facts.join(' · ')));
    // Each line is cut to fit its fixed row; the tooltip keeps every text.
    row.title = [...new Set([displayed(task.title), task.title]),
      ...(task.formerTitles ?? []).map(former => t('coordinatorHistory.formerTitles', { titles: displayed(former) })),
      displayed(task.progress ?? '')].filter(Boolean).join('\n');
    row.append(line, note, meta);
    return row;
  }

  function groupRow(row: Extract<HistoryRow, { kind: 'group' }>): HTMLElement {
    const heading = node('div', 'coordinator-history-group');
    heading.dataset.group = row.group;
    // Each task names its own status; the heading is the visual grouping only.
    heading.setAttribute('aria-hidden', 'true');
    heading.append(node('span', 'coordinator-history-group-label', groupLabel(row.group)),
      node('span', 'coordinator-history-group-count', String(row.count)));
    return heading;
  }

  function layout() {
    rows = historyRows(state.tasks, state.filter, state.counts);
    offsets = rowOffsets(rows, ROW_HEIGHTS);
    taskPositions = [];
    let position = 0;
    for (const row of rows) taskPositions.push(row.kind === 'task' ? ++position : 0);
    content.style.height = `${offsets[rows.length]}px`;
    content.setAttribute('aria-busy', String(loading || loadingMore));
    paint();
  }

  function paint() {
    if (!alive) return;
    const [first, end] = visibleRange(offsets, viewport.scrollTop, viewport.clientHeight);
    const shown = new Set<string>();
    const total = String(historyTotal(state.counts, state.filter));
    let previous: Element | null = null;
    for (let index = first; index < end; index++) {
      const row = rows[index];
      const signature = row.kind === 'task'
        ? rowSignature(row) + JSON.stringify([row.task.title, row.task.progress ?? '', ...(row.task.formerTitles ?? [])].map(displayed))
        : rowSignature(row);
      let entry = mounted.get(row.id);
      if (!entry || entry.signature !== signature) {
        const element = row.kind === 'group' ? groupRow(row) : taskRow(row.task);
        entry?.el.replaceWith(element);
        entry = { el: element, signature };
        mounted.set(row.id, entry);
      }
      shown.add(row.id);
      const element = entry.el;
      const offset = `translateY(${offsets[index]}px)`;
      if (element.style.transform !== offset) element.style.transform = offset;
      if (row.kind === 'task') {
        const position = String(taskPositions[index]);
        if (element.getAttribute('aria-posinset') !== position) element.setAttribute('aria-posinset', position);
        if (element.getAttribute('aria-setsize') !== total) element.setAttribute('aria-setsize', total);
      }
      const anchor: Element | null = previous ? previous.nextElementSibling : content.firstElementChild;
      if (anchor !== element) content.insertBefore(element, anchor);
      previous = element;
    }
    for (const [id, entry] of mounted) {
      if (!shown.has(id)) { entry.el.remove(); mounted.delete(id); }
    }
    if (end === rows.length && state.nextCursor && !loading && !loadingMore && !moreFailed) void loadMore();
  }

  function showStatus() {
    for (const [filter, chip] of chips) {
      chip.textContent = t('coordinatorHistory.chip', { label: groupLabel(filter), n: historyTotal(state.counts, filter) });
      chip.setAttribute('aria-pressed', String(filter === state.filter));
    }
    const parts: Node[] = [];
    const retry = (action: () => void) => createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: action });
    if (failed) parts.push(node('span', 'coordinator-history-status-text', t('coordinatorHistory.error')), retry(() => { void load('reset'); }));
    else if (loading && !loaded) parts.push(node('span', 'coordinator-history-status-text', t('coordinatorHistory.loading')));
    else if (moreFailed) parts.push(node('span', 'coordinator-history-status-text', t('coordinatorHistory.error')),
      retry(() => { moreFailed = false; void loadMore(); }));
    else if (loadingMore) parts.push(node('span', 'coordinator-history-status-text', t('coordinatorHistory.loadingMore')));
    else if (loaded && !state.tasks.length) {
      parts.push(node('span', 'coordinator-history-status-text',
        t(state.query || state.filter !== 'all' ? 'coordinatorHistory.noMatch' : 'coordinatorHistory.empty')));
    }
    if (loaded && !state.recorded && !failed) parts.push(node('span', 'coordinator-history-status-note', t('coordinatorHistory.unrecorded')));
    status.replaceChildren(...parts);
    status.hidden = parts.length === 0;
  }

  const query = (cursor?: string): CoordinatorTaskHistoryQuery => ({
    ...(state.filter === 'all' ? {} : { group: state.filter }),
    ...(state.query ? { q: state.query } : {}),
    ...(cursor ? { cursor } : {}),
  });

  function settle(page: CoordinatorProgressHistoryPage, tasks: CoordinatorProgressHistoryTask[]) {
    state.tasks = tasks;
    state.counts = page.counts;
    state.nextCursor = page.nextCursor;
    state.recorded = page.recorded;
    state.translation = page.translation;
  }

  /** reset: the first page of a new query. refresh: re-read as many tasks as are loaded. */
  async function load(kind: 'reset' | 'refresh') {
    reading?.abort(); readingMore?.abort();
    const own = ++generation, controller = new AbortController(), wanted = Math.max(state.tasks.length, 1);
    reading = controller;
    loading = true; loadingMore = false; moreFailed = false;
    showStatus();
    try {
      let tasks: CoordinatorProgressHistoryTask[] = [], cursor: string | undefined, page: CoordinatorProgressHistoryPage;
      do {
        page = await ports.read(query(cursor), controller.signal);
        if (own !== generation) return;
        tasks = appendPage(tasks, page.tasks);
        cursor = page.nextCursor ?? undefined;
      } while (kind === 'refresh' && cursor && tasks.length < wanted);
      settle(page, tasks);
      failed = false; loaded = true;
      if (kind === 'reset') viewport.scrollTop = 0;
    } catch {
      if (own !== generation) return;
      failed = true;
    } finally {
      if (own === generation) {
        reading = null; loading = false;
        showStatus(); layout();
        if (refreshQueued && alive) { refreshQueued = false; void load('refresh'); }
      }
    }
  }

  async function loadMore() {
    const cursor = state.nextCursor;
    if (!cursor || loading || loadingMore) return;
    const own = generation, controller = new AbortController();
    readingMore = controller; loadingMore = true;
    showStatus();
    try {
      const page = await ports.read(query(cursor), controller.signal);
      if (own !== generation) return;
      settle(page, appendPage(state.tasks, page.tasks));
    } catch {
      if (own !== generation) return;
      moreFailed = true;
    } finally {
      if (readingMore === controller) { readingMore = null; loadingMore = false; }
      if (own === generation) { showStatus(); layout(); }
    }
  }

  function choose(filter: HistoryFilter) {
    if (filter === state.filter) return;
    state.filter = filter;
    void load('reset');
  }

  input.addEventListener('input', event => {
    if ((event as InputEvent).isComposing) return;
    const next = input.value.trim();
    if (next === state.query) return;
    state.query = next;
    void load('reset');
  });
  input.addEventListener('compositionend', () => input.dispatchEvent(new Event('input')));
  viewport.addEventListener('scroll', paint, { passive: true });
  const resize = new ResizeObserver(paint);
  resize.observe(viewport);
  const unsubscribe = ports.subscribe(() => {
    if (!alive) return;
    if (loading) refreshQueued = true; else void load('refresh');
  });
  void load('reset');

  return {
    el: root,
    paint,
    destroy() {
      alive = false; generation++;
      reading?.abort(); readingMore?.abort();
      resize.disconnect(); unsubscribe();
      mounted.clear();
    },
  };
}

let openDrawer: ReturnType<typeof createDrawer> | null = null;

export async function openCoordinatorTaskHistory(ports: CoordinatorTaskHistoryPorts): Promise<void> {
  openDrawer?.close();
  const view = createCoordinatorTaskHistory(ports);
  const drawer = createDrawer({ title: t('coordinatorHistory.title'), body: view.el });
  drawer.el.classList.add('coordinator-history-drawer');
  openDrawer = drawer;
  document.body.append(drawer.el);
  const closed = drawer.open();
  view.paint();
  await closed;
  view.destroy();
  drawer.el.remove();
  if (openDrawer === drawer) openDrawer = null;
}
