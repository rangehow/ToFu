// @vitest-environment jsdom
/* Owner request 2026-11-17: the task board, not the sidebar, is where the owner finds a lead's
 * reports and files. Each task expands to the items that named it; tasks that left the board stay
 * reachable as earlier tasks; everything else reads under one "other" group. */
import { afterEach, expect, it, vi } from 'vitest';
import type { LeadDeliverable, LeadDeliverablePage } from '../api/project-coordination.generated';
import { createCoordinatorProgress, type CoordinatorProgressView } from './coordinator-progress';
import { createTaskDeliverables, OTHER_TASKS, type TaskDeliverablesPorts } from './coordinator-task-deliverables';

vi.mock('../i18n', () => ({
  t: (key: string, values?: Record<string, unknown>) => (values?.count !== undefined ? `${key}:${values.count}` : key),
}));

const KEY_A = 'a'.repeat(32), KEY_B = 'b'.repeat(32), KEY_OLD = 'c'.repeat(32);
const NOW = Date.UTC(2026, 10, 17, 12);

afterEach(() => { document.body.replaceChildren(); });

function item(id: string, task?: { key: string; title: string }, extra: Partial<LeadDeliverable> = {}): LeadDeliverable {
  return { id: `report:${id}`, kind: 'report', reportKind: 'progress', title: `Report ${id}`, projectPath: '/work/tofu',
    createdAt: NOW, source: { conversationId: 'lead', turnId: id }, summary: 'Body', ...(task ? { tasks: [task] } : {}), ...extra };
}

const SUMMARY = {
  tasks: [{ key: KEY_A, title: 'Resume', count: 2, latestAt: NOW }, { key: KEY_OLD, title: 'Old survey', count: 1, latestAt: NOW - 1 }],
  otherCount: 1,
};

function pages(summary: Pick<LeadDeliverablePage, 'tasks' | 'otherCount'> = SUMMARY): Record<string, LeadDeliverablePage> {
  const page = (items: LeadDeliverable[], nextCursor: string | null = null) => ({ conversationId: 'lead', items, nextCursor, ...summary });
  return {
    [KEY_A]: page([item('r2', { key: KEY_A, title: 'Resume' })], 'c1'),
    [`${KEY_A}:c1`]: page([item('r1', { key: KEY_A, title: 'Resume' })]),
    [KEY_OLD]: page([item('r0', { key: KEY_OLD, title: 'Old survey' })]),
    [OTHER_TASKS]: page([item('legacy', undefined, { projectPath: '/work/parallel' })]),
  };
}

function setup(summary?: Pick<LeadDeliverablePage, 'tasks' | 'otherCount'>, read?: TaskDeliverablesPorts['read']) {
  const reads: string[] = [];
  const handlers: Array<(frame: { kind?: unknown; conversationId?: unknown }) => void> = [];
  const opened: string[] = [];
  const boardChanges = new Set<() => void>();
  const store = pages(summary);
  const card = createCoordinatorProgress({ open: true, finishedOpen: false, toggle() {}, toggleFinished() {}, now: () => NOW });
  document.body.append(card.el);
  const helper = createTaskDeliverables({
    conversationId: 'lead', projectPath: '/work/tofu', now: () => NOW,
    read: async (query, signal) => {
      const key = query.cursor ? `${query.task}:${query.cursor}` : query.task!;
      reads.push(key);
      return read ? read(query, signal) : store[key];
    },
    subscribe: handler => { handlers.push(handler); return () => {}; },
    subscribeBoard: changed => { boardChanges.add(changed); return () => { boardChanges.delete(changed); }; },
    opener: async () => ({ open: entry => { opened.push(entry.id); return true; }, openSource: () => true }),
  }, () => card.repaint());
  const view: CoordinatorProgressView = { kind: 'board', taskKeys: [KEY_A, KEY_B], board: {
    revision: 3, updatedAt: NOW, summary: 'Two tasks', tasks: [
      { title: 'Resume', status: 'in_progress', progress: 'Writing' }, { title: 'Deck', status: 'pending' }] } };
  card.render(view, { history() {}, retry() {} });
  card.attachDeliverables(helper);
  return { card, helper, reads, handlers, opened, store, boardChanges };
}

const flush = async () => { for (let index = 0; index < 6; index += 1) await Promise.resolve(); };
const toggle = (key: string) => document.querySelector<HTMLButtonElement>(`.coordinator-progress-task-toggle[data-task="${key}"]`);

it('a task with reports is a disclosure with its count; a task without any stays a plain row', async () => {
  setup();
  await flush();
  const resume = toggle(KEY_A)!;
  expect(resume.getAttribute('aria-expanded')).toBe('false');
  expect(resume.querySelector('.coordinator-progress-task-count')!.textContent).toBe('2');
  expect(resume.textContent).toContain('Resume');
  expect(toggle(KEY_B)).toBeNull();
  expect(document.querySelector('.coordinator-progress-task[data-status="pending"]')!.textContent).toContain('Deck');
});

it("expanding a task lists its own reports, newest first, and loads older ones on demand", async () => {
  const { reads, opened } = setup();
  await flush();
  toggle(KEY_A)!.click();
  await flush();
  expect(toggle(KEY_A)!.getAttribute('aria-expanded')).toBe('true');
  const region = document.querySelector<HTMLElement>(`.coordinator-task-deliverables[data-task="${KEY_A}"]`)!;
  expect([...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId)).toEqual(['report:r2']);
  const more = [...region.querySelectorAll('button')].find(button => button.textContent === 'leadDeliverables.more')!;
  more.click();
  await flush();
  expect([...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId))
    .toEqual(['report:r2', 'report:r1']);
  region.querySelector<HTMLButtonElement>('.lead-deliverable__open')!.click();
  await flush();
  expect(opened).toEqual(['report:r2']);
  expect(reads).toEqual([OTHER_TASKS, KEY_A, `${KEY_A}:c1`]);
  // Folding keeps the list; reopening shows it without another read.
  toggle(KEY_A)!.click();
  toggle(KEY_A)!.click();
  await flush();
  expect(reads).toHaveLength(3);
});

it('tasks that left the board and items of no task stay reachable below the board', async () => {
  setup();
  await flush();
  const earlier = document.querySelector<HTMLDetailsElement>('.coordinator-progress-earlier')!;
  expect(earlier.querySelector('summary')!.textContent).toContain('coordinatorProgress.earlier:1');
  earlier.open = true;
  toggle(KEY_OLD)!.click();
  await flush();
  expect(document.querySelector(`.coordinator-task-deliverables[data-task="${KEY_OLD}"] .lead-deliverable`)!
    .getAttribute('data-deliverable-id')).toBe('report:r0');
  // The other group is one expandable row, not a fold around a row of the same name.
  expect(document.querySelector('.coordinator-progress-other')!.tagName).toBe('OL');
  toggle(OTHER_TASKS)!.click();
  await flush();
  // An item of another project names its project.
  const project = document.querySelector<HTMLElement>(`.coordinator-task-deliverables[data-task="${OTHER_TASKS}"] .lead-deliverable__project`)!;
  expect(project.hidden).toBe(false);
  expect(project.textContent).toBe('parallel');
});

it("a deliverable push for this lead re-reads the counts and every open task; other pushes read nothing", async () => {
  const { reads, handlers, store } = setup();
  await flush();
  toggle(KEY_A)!.click();
  await flush();
  handlers.forEach(handler => handler({ kind: 'lead_deliverable', conversationId: 'another-lead' }));
  handlers.forEach(handler => handler({ kind: 'coordinator_progress', conversationId: 'lead' }));
  await flush();
  expect(reads).toEqual([OTHER_TASKS, KEY_A]);
  const fresh = { ...SUMMARY, tasks: [{ ...SUMMARY.tasks[0], count: 3 }, SUMMARY.tasks[1]] };
  store[OTHER_TASKS] = { ...store[OTHER_TASKS], ...fresh };
  store[KEY_A] = { ...store[KEY_A], ...fresh, items: [item('r3', { key: KEY_A, title: 'Resume' }), ...store[KEY_A].items] };
  handlers.forEach(handler => handler({ kind: 'lead_deliverable', conversationId: 'lead' }));
  await flush();
  expect(reads).toEqual([OTHER_TASKS, KEY_A, OTHER_TASKS, KEY_A]);
  expect(toggle(KEY_A)!.querySelector('.coordinator-progress-task-count')!.textContent).toBe('3');
  expect([...document.querySelectorAll<HTMLElement>(`.coordinator-task-deliverables[data-task="${KEY_A}"] .lead-deliverable`)]
    .map(row => row.dataset.deliverableId)).toEqual(['report:r3', 'report:r2']);
  // A reconnect frame carries no kind and re-reads too.
  handlers.forEach(handler => handler({}));
  await flush();
  expect(reads).toHaveLength(6);
});

it('keyboard focus stays on a task toggle while its list loads and repaints the board', async () => {
  setup();
  await flush();
  toggle(KEY_A)!.focus();
  toggle(KEY_A)!.click();
  await flush();
  expect(document.activeElement).toBe(toggle(KEY_A));
});

it('without task keys (an older server) the board rows read as before and nothing is lost', async () => {
  const { card } = setup();
  await flush();
  card.render({ kind: 'board', board: { revision: 3, updatedAt: NOW, summary: 's',
    tasks: [{ title: 'Resume', status: 'in_progress' }] } }, { history() {}, retry() {} });
  // The board row cannot be matched to its reports, so it stays plain; they stay reachable below.
  const row = document.querySelector('.coordinator-progress-tasks .coordinator-progress-task')!;
  expect(row.textContent).toContain('Resume');
  expect(row.querySelector('.coordinator-progress-task-toggle')).toBeNull();
  expect(document.querySelector('.coordinator-progress-earlier')!.contains(toggle(KEY_A))).toBe(true);
});

it("an earlier task reads in its display translation, with the board's Original toggle back to its title", async () => {
  setup({ ...SUMMARY, tasks: [SUMMARY.tasks[0], { ...SUMMARY.tasks[1], translation: { title: '旧调研' } }] });
  await flush();
  const name = () => toggle(KEY_OLD)!.querySelector<HTMLElement>('.coordinator-progress-task-title')!;
  expect(name().textContent).toBe('旧调研');
  expect(name().title).toBe('Old survey');
  const original = [...document.querySelectorAll<HTMLButtonElement>('.coordinator-progress-commands button')]
    .find(button => button.textContent === 'chat.bilingualOriginal')!;
  expect(original.hidden).toBe(false);
  original.click();
  expect(name().textContent).toBe('Old survey');
  expect(original.getAttribute('aria-pressed')).toBe('true');
});

it('approaching a task reads its list ahead, so the click shows it without another read', async () => {
  const { reads } = setup();
  await flush();
  toggle(KEY_A)!.dispatchEvent(new Event('pointerenter'));
  toggle(KEY_A)!.dispatchEvent(new FocusEvent('focus'));
  await flush();
  expect(reads).toEqual([OTHER_TASKS, KEY_A]);
  toggle(KEY_A)!.click();
  expect([...document.querySelectorAll<HTMLElement>(`.coordinator-task-deliverables[data-task="${KEY_A}"] .lead-deliverable`)]
    .map(row => row.dataset.deliverableId)).toEqual(['report:r2']);
  await flush();
  expect(reads).toEqual([OTHER_TASKS, KEY_A]);
});

it('a list read ahead is read again on opening once a deliverable push made it stale', async () => {
  const { reads, handlers } = setup();
  await flush();
  toggle(KEY_A)!.dispatchEvent(new Event('pointerenter'));
  await flush();
  handlers.forEach(handler => handler({ kind: 'lead_deliverable', conversationId: 'lead' }));
  await flush();
  expect(reads).toEqual([OTHER_TASKS, KEY_A, OTHER_TASKS]);
  toggle(KEY_A)!.click();
  await flush();
  expect(reads).toEqual([OTHER_TASKS, KEY_A, OTHER_TASKS, KEY_A]);
});

it("a board mounted again for the same lead paints the last counts before its summary read answers", async () => {
  setup();
  await flush();
  document.body.replaceChildren();
  const card = createCoordinatorProgress({ open: true, finishedOpen: false, toggle() {}, toggleFinished() {}, now: () => NOW });
  document.body.append(card.el);
  const pending: Array<() => void> = [];
  const helper = createTaskDeliverables({
    conversationId: 'lead', projectPath: '/work/tofu', now: () => NOW,
    read: () => new Promise(() => { pending.push(() => {}); }),
    subscribe: () => () => {},
  }, () => card.repaint());
  card.render({ kind: 'board', taskKeys: [KEY_A], board: { revision: 3, updatedAt: NOW, summary: 's',
    tasks: [{ title: 'Resume', status: 'in_progress' }] } }, { history() {}, retry() {} });
  card.attachDeliverables(helper);
  expect(pending).toHaveLength(1);
  expect(toggle(KEY_A)!.querySelector('.coordinator-progress-task-count')!.textContent).toBe('2');
  // Another lead of the project (a replaced binding) starts without them.
  expect(createTaskDeliverables({ conversationId: 'new-lead', projectPath: '/work/tofu', read: () => new Promise(() => {}),
    subscribe: () => () => {} }, () => {}).count(KEY_A)).toBe(0);
});


it('keeps current work first and shows recorded off-board status separately from delivery evidence', async () => {
  const completed = 'd'.repeat(32), cancelled = 'e'.repeat(32), unknown = 'f'.repeat(32);
  const { card } = setup({ ...SUMMARY, tasks: [SUMMARY.tasks[0],
    { ...SUMMARY.tasks[1], archivedGroup: 'dropped', hasDeliveryReport: true },
    { key: completed, title: 'Finished survey', count: 1, latestAt: NOW, archivedGroup: 'completed', hasDeliveryReport: false },
    { key: cancelled, title: 'Cancelled survey', count: 1, latestAt: NOW, archivedGroup: 'cancelled' },
    { key: unknown, title: 'Unrecorded survey', count: 1, latestAt: NOW, hasDeliveryReport: true },
  ] });
  await flush();
  const tasks = [...card.el.querySelectorAll<HTMLElement>('.coordinator-progress-task')];
  expect(tasks.map(row => row.dataset.status)).toEqual(['in_progress', 'pending', 'dropped', 'completed', 'cancelled', 'unknown', 'other']);
  expect(toggle(KEY_OLD)!.textContent).toContain('coordinatorHistory.group.dropped');
  expect(toggle(completed)!.getAttribute('aria-label')).toContain('coordinatorProgress.noDeliveryReport');
  expect(toggle(unknown)!.textContent).toContain('coordinatorProgress.status.unknown');
  expect(toggle(KEY_OLD)!.textContent).not.toContain('coordinatorProgress.status.completed');
});

it('refreshes archived status on a board revision and replaces an automatic note with the delivery report', async () => {
  const note: LeadDeliverable = { ...item('note'), id: `completion:${KEY_OLD}`, kind: 'completion', reportKind: undefined, source: null };
  const auto = { ...SUMMARY, tasks: [SUMMARY.tasks[0], { ...SUMMARY.tasks[1], archivedGroup: 'completed' as const, hasDeliveryReport: false }] };
  const { card, helper, store, boardChanges, handlers } = setup(auto);
  store[KEY_OLD] = { ...store[KEY_OLD], items: [note] };
  await flush();
  card.el.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!.open = true;
  toggle(KEY_OLD)!.click();
  await flush();
  const ids = () => [...card.el.querySelectorAll<HTMLElement>(`.coordinator-task-deliverables[data-task="${KEY_OLD}"] .lead-deliverable`)]
    .map(row => row.dataset.deliverableId);
  expect(ids()).toEqual([note.id]);
  const fresh = { ...auto, tasks: [auto.tasks[0], { ...auto.tasks[1], hasDeliveryReport: true }] };
  store[OTHER_TASKS] = { ...store[OTHER_TASKS], ...fresh };
  store[KEY_OLD] = { ...store[KEY_OLD], ...fresh, items: [item('delivery', undefined, { reportKind: 'delivery' })] };
  handlers.forEach(changed => changed({ kind: 'lead_deliverable', conversationId: 'lead' }));
  await flush();
  expect(toggle(KEY_OLD)!.textContent).toContain('coordinatorProgress.deliveryLinked');
  expect(ids()).toEqual(['report:delivery']);
  const reopened = { ...fresh, tasks: [fresh.tasks[0], { ...fresh.tasks[1], archivedGroup: 'dropped' as const }] };
  store[OTHER_TASKS] = { ...store[OTHER_TASKS], ...reopened };
  store[KEY_OLD] = { ...store[KEY_OLD], ...reopened };
  boardChanges.forEach(changed => changed());
  await flush();
  expect(toggle(KEY_OLD)!.closest('li')!.dataset.status).toBe('dropped');
  helper.destroy();
  expect(boardChanges.size).toBe(0);
});

it.each(['before-board', 'after-board', 'after-open', 'after-fresh', 'failed-summary', 'failed-fresh'] as const)(
  'orders a closed prefetch against an authoritative board update: %s', async order => {
  const store = pages();
  const old = store[KEY_A];
  let answerOld!: (page: LeadDeliverablePage) => void;
  let oldSignal!: AbortSignal;
  const pending = new Promise<LeadDeliverablePage>(resolve => { answerOld = resolve; });
  let first = true, updated = false;
  const { helper, reads, boardChanges } = setup(undefined, (query, signal) => {
    if (query.task === KEY_A && first) {
      first = false;
      oldSignal = signal;
      return pending;
    }
    if (query.task === OTHER_TASKS && updated && order === 'failed-summary') return Promise.reject(new Error('offline'));
    if (query.task === KEY_A && order === 'failed-fresh') return Promise.reject(new Error('offline'));
    return Promise.resolve(store[query.task!]);
  });
  try {
    await flush();
    toggle(KEY_A)!.dispatchEvent(new FocusEvent('focus'));
    expect(reads).toEqual([OTHER_TASKS, KEY_A]);
    const fresh = { ...SUMMARY, tasks: [{ ...SUMMARY.tasks[0], count: 3 }, SUMMARY.tasks[1]] };
    store[OTHER_TASKS] = { ...store[OTHER_TASKS], ...fresh };
    store[KEY_A] = { ...store[KEY_A], ...fresh, items: [item('r3', { key: KEY_A, title: 'Resume' })] };
    if (order === 'before-board') { answerOld(old); await flush(); }
    updated = true;
    boardChanges.forEach(changed => changed());
    await flush();
    expect(helper.count(KEY_A)).toBe(order === 'failed-summary' ? 2 : 3);
    if (order === 'after-board' || order === 'failed-summary') { answerOld(old); await flush(); }
    expect(reads).toEqual([OTHER_TASKS, KEY_A, OTHER_TASKS]);
    toggle(KEY_A)!.click();
    toggle(KEY_A)!.focus();
    if (order === 'after-open') answerOld(old);
    await flush();
    if (order === 'after-fresh' || order === 'failed-fresh') { answerOld(old); await flush(); }
    expect(helper.count(KEY_A)).toBe(3);
    expect(oldSignal.aborted).toBe(order !== 'before-board');
    expect([...helper.region(KEY_A).querySelectorAll<HTMLElement>('.lead-deliverable')]
      .map(row => row.dataset.deliverableId)).toEqual(order === 'failed-fresh' ? []
        : order === 'before-board' ? ['report:r3', 'report:r2'] : ['report:r3']);
    if (order === 'failed-fresh') {
      expect(helper.region(KEY_A).textContent).toContain('leadDeliverables.error');
      expect(helper.region(KEY_A).textContent).toContain('leadDeliverables.retry');
    }
    expect(helper.region(KEY_A).dataset.task).toBe(KEY_A);
    expect(document.activeElement).toBe(toggle(KEY_A));
    expect(reads).toEqual([OTHER_TASKS, KEY_A, OTHER_TASKS, KEY_A]);
  } finally { helper.destroy(); }
});

it('shares unchanged keyboard read-ahead through opening, closing and viewing another task', async () => {
  const store = pages();
  let answer!: (page: LeadDeliverablePage) => void;
  let signal!: AbortSignal;
  const pending = new Promise<LeadDeliverablePage>(resolve => { answer = resolve; });
  const { helper, reads } = setup(undefined, (query, currentSignal) => {
    if (query.task === KEY_A) { signal = currentSignal; return pending; }
    return Promise.resolve(store[query.task!]);
  });
  try {
    await flush();
    toggle(KEY_A)!.focus();
    toggle(KEY_A)!.dispatchEvent(new Event('pointerenter'));
    toggle(KEY_A)!.click();
    expect(reads).toEqual([OTHER_TASKS, KEY_A]);
    expect(signal.aborted).toBe(false);
    toggle(KEY_A)!.click();
    toggle(KEY_OLD)!.focus();
    toggle(KEY_OLD)!.click();
    await flush();
    answer(store[KEY_A]);
    await flush();
    expect(helper.expanded(KEY_A)).toBe(false);
    expect(helper.expanded(KEY_OLD)).toBe(true);
    expect([...helper.region(KEY_OLD).querySelectorAll<HTMLElement>('.lead-deliverable')]
      .map(row => row.dataset.deliverableId)).toEqual(['report:r0']);
    expect(document.activeElement).toBe(toggle(KEY_OLD));
    expect(signal.aborted).toBe(false);
    toggle(KEY_A)!.click();
    await flush();
    expect([...helper.region(KEY_A).querySelectorAll<HTMLElement>('.lead-deliverable')]
      .map(row => row.dataset.deliverableId)).toEqual(['report:r2']);
    expect(reads).toEqual([OTHER_TASKS, KEY_A, KEY_OLD]);
  } finally { helper.destroy(); }
});

it.each([
  { exhausted: false, tied: false }, { exhausted: false, tied: true },
  { exhausted: true, tied: false }, { exhausted: true, tied: true },
])('resumes a changed first-page continuation without skipping reports: %j', async ({ exhausted, tied }) => {
  const task = { key: KEY_A, title: 'Resume' };
  // r5/r4 and r2/r1 also follow the API's ascending digest order when their timestamps tie.
  const report = (id: string, rank: number) => item(id, task,
    { createdAt: NOW + (tied && (rank === 4 || rank === 1) ? rank + 1 : rank) });
  const page = (items: LeadDeliverable[], nextCursor: string | null): LeadDeliverablePage =>
    ({ conversationId: 'lead', ...SUMMARY, items, nextCursor });
  const responses = [
    page([report('r2', 2)], 'after-r2'),
    page([report('r1', 1)], exhausted ? null : 'after-r1'),
    page([report('r6', 6), report('r5', 5)], 'after-r5'),
    page([report('r4', 4), report('r3', 3)], 'after-r3'),
    page([report('r2', 2), report('r1', 1)], null),
  ];
  const queries: Array<{ task?: string; cursor?: string }> = [];
  const { helper, boardChanges, store } = setup(undefined, query => {
    queries.push({ ...query });
    return Promise.resolve(query.task === KEY_A ? responses.shift()! : pages()[query.task!]);
  });
  try {
    await flush();
    toggle(KEY_A)!.click();
    toggle(KEY_OLD)!.click();
    await flush();
    const region = helper.region(KEY_A);
    const ids = () => [...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId);
    const more = () => [...region.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === 'leadDeliverables.more');
    more()!.click();
    await flush();
    expect(ids()).toEqual(['report:r2', 'report:r1']);
    const olderRow = region.querySelector('[data-deliverable-id="report:r1"]');
    boardChanges.forEach(changed => changed());
    await flush();
    expect(ids()).toEqual(['report:r6', 'report:r5', 'report:r2', 'report:r1']);
    expect(region.querySelector('[data-deliverable-id="report:r1"]')).toBe(olderRow);
    expect(more()).toBeDefined();
    more()!.click();
    await flush();
    expect(queries.filter(query => query.task === KEY_A)).toEqual([
      { task: KEY_A }, { task: KEY_A, cursor: 'after-r2' }, { task: KEY_A },
      { task: KEY_A, cursor: 'after-r5' },
    ]);
    expect(ids()).toEqual(['report:r6', 'report:r5', 'report:r4', 'report:r3', 'report:r2', 'report:r1']);
    more()!.click();
    await flush();
    expect(ids()).toEqual(['report:r6', 'report:r5', 'report:r4', 'report:r3', 'report:r2', 'report:r1']);
    expect(region.querySelector('[data-deliverable-id="report:r1"]')).toBe(olderRow);
    expect(more()).toBeUndefined();
    expect(queries).toEqual([
      { task: OTHER_TASKS }, { task: KEY_A }, { task: KEY_OLD },
      { task: KEY_A, cursor: 'after-r2' },
      { task: OTHER_TASKS }, { task: KEY_A }, { task: KEY_OLD },
      { task: KEY_A, cursor: 'after-r5' }, { task: KEY_A, cursor: 'after-r3' },
    ]);
    expect([...helper.region(KEY_OLD).querySelectorAll<HTMLElement>('.lead-deliverable')]
      .map(row => row.dataset.deliverableId)).toEqual(store[KEY_OLD].items.map(row => row.id));
    expect(responses).toHaveLength(0);
  } finally { helper.destroy(); }
});

it.each([false, true])('keeps the advanced continuation on an unchanged first page (exhausted=%s)', async exhausted => {
  const { helper, reads, boardChanges, store } = setup();
  try {
    store[`${KEY_A}:c1`].nextCursor = exhausted ? null : 'c2';
    await flush();
    toggle(KEY_A)!.click();
    await flush();
    const region = helper.region(KEY_A);
    const more = () => [...region.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === 'leadDeliverables.more');
    more()!.click();
    await flush();
    boardChanges.forEach(changed => changed());
    await flush();
    toggle(KEY_A)!.click();
    toggle(KEY_A)!.click();
    await flush();
    expect(reads).toEqual([OTHER_TASKS, KEY_A, `${KEY_A}:c1`, OTHER_TASKS, KEY_A]);
    expect([...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId))
      .toEqual(['report:r2', 'report:r1']);
    if (exhausted) expect(more()).toBeUndefined();
    else {
      store[`${KEY_A}:c2`] = { ...store[`${KEY_A}:c1`], items: [item('r0')], nextCursor: null };
      more()!.click();
      await flush();
      expect(reads.at(-1)).toBe(`${KEY_A}:c2`);
    }
  } finally { helper.destroy(); }
});

it('adopts a changed continuation when reopening a stale exhausted list, including a terminal head', async () => {
  const { helper, reads, boardChanges, store } = setup();
  try {
    await flush();
    toggle(KEY_A)!.click();
    await flush();
    const region = helper.region(KEY_A);
    const more = () => [...region.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === 'leadDeliverables.more');
    more()!.click();
    await flush();
    toggle(KEY_A)!.click();
    store[KEY_A] = { ...store[KEY_A], items: [item('fresh')], nextCursor: 'fresh-tail' };
    store[`${KEY_A}:fresh-tail`] = { ...store[KEY_A], items: [item('gap'), item('r2')], nextCursor: 'remaining' };
    boardChanges.forEach(changed => changed());
    await flush();
    expect(reads).toEqual([OTHER_TASKS, KEY_A, `${KEY_A}:c1`, OTHER_TASKS]);
    toggle(KEY_A)!.click();
    await flush();
    more()!.click();
    await flush();
    expect(reads.at(-1)).toBe(`${KEY_A}:fresh-tail`);
    expect([...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId))
      .toEqual(['report:fresh', 'report:gap', 'report:r2', 'report:r1']);
    store[KEY_A] = { ...store[KEY_A], nextCursor: null };
    boardChanges.forEach(changed => changed());
    await flush();
    expect(more()).toBeUndefined();
    expect([...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId))
      .toEqual(['report:fresh', 'report:gap', 'report:r2', 'report:r1']);
    expect(reads).toEqual([OTHER_TASKS, KEY_A, `${KEY_A}:c1`, OTHER_TASKS, KEY_A,
      `${KEY_A}:fresh-tail`, OTHER_TASKS, KEY_A]);
  } finally { helper.destroy(); }
});

it.each([false, true])('keeps the fetched boundary when a completion anchor disappears (retained older=%s)', async retainedOlder => {
  const report = (id: string, createdAt: number) => item(id, { key: KEY_A, title: 'Resume' }, { createdAt });
  const note: LeadDeliverable = { ...report('note', 15), id: `completion:${KEY_A}`, kind: 'completion', source: null };
  const page = (items: LeadDeliverable[], nextCursor: string | null, hasDeliveryReport = false): LeadDeliverablePage =>
    ({ conversationId: 'lead', items, nextCursor, ...SUMMARY,
      tasks: [{ ...SUMMARY.tasks[0], hasDeliveryReport }, SUMMARY.tasks[1]] });
  let refreshed = false;
  const queries: Array<{ task?: string; cursor?: string }> = [];
  const { helper, boardChanges } = setup(undefined, query => {
    queries.push({ ...query });
    if (query.task !== KEY_A) return Promise.resolve(pages()[query.task!]);
    if (query.cursor === 'c3') return Promise.resolve(page([report('r2', 20), note], 'c-note'));
    if (query.cursor === 'c-note') return Promise.resolve(page([report('r1', 10)], null, true));
    return Promise.resolve(refreshed ? page([report('r3', 30)], 'c3', true)
      : retainedOlder ? page([report('old', 5)], null) : page([report('r3', 30)], 'c3'));
  });
  try {
    await flush();
    toggle(KEY_A)!.click();
    await flush();
    if (retainedOlder) {
      refreshed = true;
      boardChanges.forEach(changed => changed());
      await flush();
    }
    const region = helper.region(KEY_A);
    const ids = () => [...region.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId);
    const more = () => [...region.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === 'leadDeliverables.more')!;
    more().click();
    await flush();
    const retainedRow = region.querySelector(`[data-deliverable-id="report:${retainedOlder ? 'old' : 'r2'}"]`);
    refreshed = true;
    boardChanges.forEach(changed => changed());
    await flush();
    expect(ids()).toEqual(['report:r3', 'report:r2', ...(retainedOlder ? ['report:old'] : [])]);
    more().click();
    await flush();
    expect(ids()).toEqual(['report:r3', 'report:r2', 'report:r1', ...(retainedOlder ? ['report:old'] : [])]);
    expect(region.querySelector(`[data-deliverable-id="report:${retainedOlder ? 'old' : 'r2'}"]`)).toBe(retainedRow);
    expect(queries.filter(query => query.task === KEY_A)).toEqual([
      { task: KEY_A }, ...(retainedOlder ? [{ task: KEY_A }] : []),
      { task: KEY_A, cursor: 'c3' }, { task: KEY_A }, { task: KEY_A, cursor: 'c-note' },
    ]);
  } finally { helper.destroy(); }
});
