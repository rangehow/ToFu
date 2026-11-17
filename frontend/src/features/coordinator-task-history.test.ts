// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { CoordinatorProgressHistoryPage, CoordinatorProgressHistoryTask } from '../api/project-coordination.generated';
import { createCoordinatorTaskHistory, type CoordinatorTaskHistoryQuery } from './coordinator-task-history';
import { appendPage, historyRows, rowOffsets, visibleRange } from './coordinator-task-history-model';

vi.mock('../i18n', () => ({ t: (key: string, args?: Record<string, unknown>) => (args ? `${key}:${JSON.stringify(args)}` : key) }));

const NOW = Date.UTC(2026, 10, 11, 12, 0);
const PAGE = 64;
const ROW = { group: 36, task: 84 };

function task(index: number, extra: Partial<CoordinatorProgressHistoryTask> = {}): CoordinatorProgressHistoryTask {
  return { key: index.toString(16).padStart(32, '0'), title: `Task ${index}`, status: 'completed', onBoard: false, dropped: false,
    firstSeenAt: NOW - 86_400_000, lastSeenAt: NOW - 3_600_000, changedAt: NOW - index * 1000, progress: `Note ${index}`, ...extra };
}

type Counts = CoordinatorProgressHistoryPage['counts'];
const countsOf = (tasks: CoordinatorProgressHistoryTask[]): Counts => {
  const counts: Counts = { in_progress: 0, pending: 0, dropped: 0, completed: 0, cancelled: 0 };
  for (const item of tasks) counts[item.dropped ? 'dropped' : item.status]++;
  return counts;
};

/** A server over a fixed history: 64-task pages with an offset cursor, filtered like the API. */
function server(all: () => CoordinatorProgressHistoryTask[]) {
  const reads: CoordinatorTaskHistoryQuery[] = [];
  const read = vi.fn(async (query: CoordinatorTaskHistoryQuery, _signal: AbortSignal): Promise<CoordinatorProgressHistoryPage> => {
    reads.push(query);
    const needle = (query.q ?? '').toLowerCase();
    const matching = all().filter(item => !needle || item.title.toLowerCase().includes(needle) || (item.progress ?? '').toLowerCase().includes(needle));
    const shown = matching.filter(item => !query.group || (item.dropped ? 'dropped' : item.status) === query.group);
    const start = query.cursor ? Number(query.cursor) : 0;
    const tasks = shown.slice(start, start + PAGE);
    const more = start + PAGE < shown.length;
    return { projectPath: '/project', conversationId: 'leader', bindingRevision: 1, recorded: true, counts: countsOf(matching),
      tasks, nextCursor: more ? String(start + PAGE) : null, coverage: more ? 'page' : 'complete' };
  });
  return { read, reads };
}

let observers: Array<() => void> = [];
beforeEach(() => {
  observers = [];
  vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { observers.push(callback); } observe() {} disconnect() {} });
});
afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); });

function mount(all: () => CoordinatorProgressHistoryTask[], height = 600) {
  const api = server(all);
  let changed = () => {};
  const unsubscribe = vi.fn();
  const view = createCoordinatorTaskHistory({ read: api.read, now: () => NOW,
    subscribe: listener => { changed = listener; return unsubscribe; } });
  document.body.append(view.el);
  const viewport = view.el.querySelector<HTMLElement>('.coordinator-history-viewport')!;
  Object.defineProperty(viewport, 'clientHeight', { configurable: true, get: () => height });
  const scroll = (top: number) => { viewport.scrollTop = top; viewport.dispatchEvent(new Event('scroll')); };
  const rows = () => [...view.el.querySelectorAll<HTMLElement>('.coordinator-history-task')];
  const titles = () => rows().map(row => row.querySelector('.coordinator-history-task-title')!.textContent);
  return { view, api, viewport, scroll, rows, titles, unsubscribe, changed: () => changed() };
}

it('groups loaded tasks under one heading per group and finds the visible window by binary search', () => {
  const tasks = [task(1, { status: 'in_progress', onBoard: true }), task(2, { status: 'in_progress', dropped: true }), task(3), task(4)];
  const counts = countsOf(tasks);
  const rows = historyRows(tasks, 'all', counts);
  expect(rows.map(row => row.kind === 'group' ? `#${row.group}:${row.count}` : row.task.title)).toEqual([
    '#in_progress:1', 'Task 1', '#dropped:1', 'Task 2', '#completed:2', 'Task 3', 'Task 4']);
  expect(historyRows(tasks, 'completed', counts).every(row => row.kind === 'task')).toBe(true);
  const offsets = rowOffsets(rows, ROW);
  expect([...offsets]).toEqual([0, 36, 120, 156, 240, 276, 360, 444]);
  expect(visibleRange(offsets, 0, 100)).toEqual([0, 2]);
  expect(visibleRange(offsets, 130, 100)).toEqual([2, 4]);
  expect(visibleRange(offsets, 440, 100)).toEqual([6, 7]);
  expect(visibleRange(offsets, 0, 0)).toEqual([0, 0]);
  // A task a board write moved between two page reads is shown once.
  expect(appendPage([task(1), task(2)], [task(2), task(3)]).map(item => item.title)).toEqual(['Task 1', 'Task 2', 'Task 3']);
});

it('renders only the rows in view, with each task\'s status, due outcome, dates and dropped marker', async () => {
  const tasks = [
    task(1, { status: 'in_progress', onBoard: true, due: '2026-11-12T20:00:00+08:00', dueAt: NOW + 86_400_000, dueOutcome: 'open' }),
    task(2, { status: 'in_progress', dropped: true, departedAt: NOW - 600_000, formerTitles: ['Task two (draft)'] }),
    ...Array.from({ length: 40 }, (_, index) => task(index + 3, index === 0
      ? { due: '2026-11-10T20:00:00+08:00', dueAt: NOW - 86_400_000, completedAt: NOW - 90_000_000, dueOutcome: 'met' } : {})),
  ];
  const m = mount(() => tasks, 300);
  await vi.waitFor(() => expect(m.rows().length).toBeGreaterThan(0));
  // 300px shows the in-progress heading, two task rows and the next heading: never all 42 tasks.
  expect(m.titles()).toEqual(['Task 1', 'Task 2', 'Task 3']);
  const [current, dropped, met] = m.rows();
  expect(current.dataset.status).toBe('in_progress');
  expect(current.querySelector('[data-kind="board"]')?.textContent).toBe('coordinatorHistory.onBoard');
  expect(current.querySelector('[data-kind="due-open"]')?.textContent).toMatch(/^coordinatorHistory\.due\.open:/);
  expect(dropped.dataset.dropped).toBe('true');
  expect(dropped.querySelector('[data-kind="dropped"]')?.textContent).toBe('coordinatorHistory.group.dropped');
  expect(dropped.querySelector('.coordinator-history-task-meta')?.textContent).toContain('coordinatorHistory.formerTitles');
  expect(dropped.title).toContain('Task two (draft)');
  expect(met.querySelector('[data-kind="due-met"]')).not.toBeNull();
  expect(met.querySelector('.coordinator-history-task-note')?.textContent).toBe('Note 3');
  expect(current.getAttribute('aria-posinset')).toBe('1');
  expect(current.getAttribute('aria-setsize')).toBe('42');
  const chips = [...m.view.el.querySelectorAll<HTMLButtonElement>('[data-history-filter]')];
  expect(chips.map(chip => chip.textContent)).toEqual([
    'coordinatorHistory.chip:{"label":"coordinatorHistory.group.all","n":42}',
    'coordinatorHistory.chip:{"label":"coordinatorHistory.group.in_progress","n":1}',
    'coordinatorHistory.chip:{"label":"coordinatorHistory.group.pending","n":0}',
    'coordinatorHistory.chip:{"label":"coordinatorHistory.group.dropped","n":1}',
    'coordinatorHistory.chip:{"label":"coordinatorHistory.group.completed","n":40}',
    'coordinatorHistory.chip:{"label":"coordinatorHistory.group.cancelled","n":0}']);
  expect(chips[0].getAttribute('aria-pressed')).toBe('true');
  m.view.destroy();
  expect(m.unsubscribe).toHaveBeenCalledOnce();
});

it('loads the next page only when the end of the loaded rows comes into view', async () => {
  const tasks = Array.from({ length: 150 }, (_, index) => task(index));
  const m = mount(() => tasks);
  await vi.waitFor(() => expect(m.rows().length).toBeGreaterThan(0));
  expect(m.api.read).toHaveBeenCalledOnce();
  m.scroll(36 + 30 * ROW.task);
  expect(m.api.read).toHaveBeenCalledOnce();
  m.scroll(36 + 64 * ROW.task - 600);
  await vi.waitFor(() => expect(m.api.read).toHaveBeenCalledTimes(2));
  expect(m.api.reads[1]).toEqual({ cursor: '64' });
  await vi.waitFor(() => expect(m.viewport.querySelector<HTMLElement>('.coordinator-history-rows')!.style.height).toBe(`${36 + 128 * ROW.task}px`));
  m.scroll(36 + 150 * ROW.task);
  await vi.waitFor(() => expect(m.api.read).toHaveBeenCalledTimes(3));
  await vi.waitFor(() => expect(m.titles()).toContain('Task 149'));
  m.scroll(36 + 150 * ROW.task);
  expect(m.api.read).toHaveBeenCalledTimes(3);
  m.view.destroy();
});

it('filters and searches through the server, from the first page', async () => {
  const tasks = [task(1, { status: 'in_progress', onBoard: true }), task(2, { title: '修复侧边栏' }), task(3, { status: 'cancelled' })];
  const m = mount(() => tasks);
  await vi.waitFor(() => expect(m.rows()).toHaveLength(3));
  m.view.el.querySelector<HTMLButtonElement>('[data-history-filter="cancelled"]')!.click();
  await vi.waitFor(() => expect(m.titles()).toEqual(['Task 3']));
  expect(m.api.reads.at(-1)).toEqual({ group: 'cancelled' });
  expect(m.view.el.querySelector('.coordinator-history-group')).toBeNull();
  const input = m.view.el.querySelector<HTMLInputElement>('input[type="search"]')!;
  input.value = '侧边栏'; input.dispatchEvent(new Event('input'));
  await vi.waitFor(() => expect(m.view.el.querySelector('.coordinator-history-status')?.textContent).toBe('coordinatorHistory.noMatch'));
  expect(m.api.reads.at(-1)).toEqual({ group: 'cancelled', q: '侧边栏' });
  m.view.el.querySelector<HTMLButtonElement>('[data-history-filter="all"]')!.click();
  await vi.waitFor(() => expect(m.titles()).toEqual(['修复侧边栏']));
  expect(m.api.reads.at(-1)).toEqual({ q: '侧边栏' });
  // An IME composition searches once, when the text is committed.
  input.value = 'Tas'; input.dispatchEvent(new InputEvent('input', { isComposing: true }));
  expect(m.api.reads.at(-1)).toEqual({ q: '侧边栏' });
  input.dispatchEvent(new Event('compositionend'));
  await vi.waitFor(() => expect(m.api.reads.at(-1)).toEqual({ q: 'Tas' }));
  m.view.destroy();
});

it('re-reads the loaded pages in place on a newer board, rebuilding only rows whose content changed', async () => {
  let tasks = Array.from({ length: 100 }, (_, index) => task(index, index === 1 ? { onBoard: true } : {}));
  const m = mount(() => tasks);
  await vi.waitFor(() => expect(m.rows().length).toBeGreaterThan(0));
  m.scroll(36 + 64 * ROW.task - 600);
  await vi.waitFor(() => expect(m.api.read).toHaveBeenCalledTimes(2));
  m.scroll(0);
  const before = new Map(m.rows().map(row => [row.querySelector('.coordinator-history-task-title')!.textContent, row]));
  // Every board write sees a task on the board again; only Task 2's shown content changed.
  tasks = tasks.map(item => item.title === 'Task 2' ? { ...item, progress: 'Verified in the browser.' }
    : item.onBoard ? { ...item, lastSeenAt: item.lastSeenAt + 60_000 } : item);
  m.changed();
  await vi.waitFor(() => expect(m.api.read).toHaveBeenCalledTimes(4));
  await vi.waitFor(() => expect(m.rows().find(row => row.textContent?.includes('Verified in the browser.'))).toBeDefined());
  // Both loaded pages were read again, so the list keeps its length and scroll range.
  expect(m.api.reads.slice(2)).toEqual([{}, { cursor: '64' }]);
  for (const row of m.rows()) {
    const title = row.querySelector('.coordinator-history-task-title')!.textContent!;
    if (title === 'Task 2') expect(row).not.toBe(before.get(title));
    else expect(row).toBe(before.get(title));
  }
  m.view.destroy();
});

it('keeps scrolling through 5,000 tasks cheap: bounded DOM and fast windows', async () => {
  // Server order: the completed group, then the cancelled one.
  const tasks = Array.from({ length: 5000 }, (_, index) => task(index, index >= 4300 ? { status: 'cancelled' } : {}));
  const m = mount(() => tasks);
  await vi.waitFor(() => expect(m.rows().length).toBeGreaterThan(0));
  const loadAll = performance.now();
  // jsdom never clamps scrollTop, so the end stays in view and every page loads in turn.
  m.scroll(Number.MAX_SAFE_INTEGER);
  await vi.waitFor(() => expect(m.api.read).toHaveBeenCalledTimes(Math.ceil(5000 / PAGE)), { timeout: 20_000 });
  await vi.waitFor(() => expect(m.view.el.querySelector('.coordinator-history-rows')!.getAttribute('aria-busy')).toBe('false'));
  const loaded = performance.now() - loadAll;
  expect(m.view.el.querySelector<HTMLElement>('.coordinator-history-rows')!.style.height).toBe(`${2 * 36 + 5000 * ROW.task}px`);
  const total = 2 * 36 + 5000 * ROW.task;
  const started = performance.now();
  let maxRows = 0;
  for (let step = 0; step < 500; step++) {
    m.scroll(((step * 7919) % 500) / 500 * (total - 600));
    maxRows = Math.max(maxRows, m.rows().length + m.view.el.querySelectorAll('.coordinator-history-group').length);
  }
  const scrolled = performance.now() - started;
  const model = performance.now();
  const rows = historyRows(tasks, 'all', countsOf(tasks));
  visibleRange(rowOffsets(rows, ROW), 200_000, 600);
  const projected = performance.now() - model;
  console.info(`task history, 5000 tasks: ${maxRows} rows mounted at most; 500 scroll windows ${scrolled.toFixed(0)} ms `
    + `(${(scrolled / 500).toFixed(2)} ms each); rows + offsets ${projected.toFixed(1)} ms; 79 page loads ${loaded.toFixed(0)} ms`);
  // A 600px viewport over 84px rows: at most 9 rows plus a heading are ever in the DOM.
  expect(maxRows).toBeLessThanOrEqual(10);
  expect(scrolled / 500).toBeLessThan(8);
  expect(projected).toBeLessThan(50);
  m.view.destroy();
});

it('shows tasks on the board in the board\'s display translation, with the original in the tooltip', async () => {
  const tasks = [task(1, { status: 'in_progress', onBoard: true, title: 'Book flights', progress: 'Comparing fares' }), task(2)];
  const read = vi.fn(async (): Promise<CoordinatorProgressHistoryPage> => ({
    projectPath: '/project', conversationId: 'leader', bindingRevision: 1, recorded: true, counts: countsOf(tasks), tasks,
    nextCursor: null, coverage: 'complete',
    translation: { target: 'Chinese', texts: [{ source: 'Book flights', text: '订机票' }, { source: 'Comparing fares', text: '比较票价' }] } }));
  const view = createCoordinatorTaskHistory({ read, now: () => NOW, subscribe: () => () => {} });
  document.body.append(view.el);
  const viewport = view.el.querySelector<HTMLElement>('.coordinator-history-viewport')!;
  Object.defineProperty(viewport, 'clientHeight', { configurable: true, get: () => 600 });
  await vi.waitFor(() => expect(view.el.querySelectorAll('.coordinator-history-task')).toHaveLength(2));
  const rows = [...view.el.querySelectorAll<HTMLElement>('.coordinator-history-task')];
  expect(rows.map(row => row.querySelector('.coordinator-history-task-title')!.textContent)).toEqual(['订机票', 'Task 2']);
  expect(rows[0].querySelector('.coordinator-history-task-note')!.textContent).toBe('比较票价');
  expect(rows[0].title).toBe('订机票\nBook flights\n比较票价');
  view.destroy();
});
