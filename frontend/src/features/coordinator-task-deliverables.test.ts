// @vitest-environment jsdom
/* Owner request 2026-11-17: the task board, not the sidebar, is where the owner finds a lead's
 * reports and files. Each task expands to the items that named it; tasks that left the board stay
 * reachable as earlier tasks; everything else reads under one "other" group. */
import { afterEach, expect, it, vi } from 'vitest';
import type { LeadDeliverable, LeadDeliverablePage } from '../api/project-coordination.generated';
import { createCoordinatorProgress, type CoordinatorProgressView } from './coordinator-progress';
import { createTaskDeliverables, OTHER_TASKS } from './coordinator-task-deliverables';

vi.mock('../i18n', () => ({
  t: (key: string, values?: Record<string, unknown>) => (values?.count !== undefined ? `${key}:${values.count}` : key),
}));

const KEY_A = 'a'.repeat(32), KEY_B = 'b'.repeat(32), KEY_OLD = 'c'.repeat(32);
const NOW = Date.UTC(2026, 10, 17, 12);

afterEach(() => { document.body.replaceChildren(); });

function item(id: string, task?: { key: string; title: string }, extra: Partial<LeadDeliverable> = {}): LeadDeliverable {
  return { id: `report:${id}`, kind: 'report', reportKind: 'progress', title: `Report ${id}`, projectPath: '/work/tofu',
    createdAt: NOW, source: { conversationId: 'lead', turnId: id }, summary: 'Body', ...(task ? { task } : {}), ...extra };
}

const SUMMARY = {
  tasks: [{ key: KEY_A, title: 'Resume', count: 2, latestAt: NOW }, { key: KEY_OLD, title: 'Old survey', count: 1, latestAt: NOW - 1 }],
  otherCount: 1,
};

function pages(): Record<string, LeadDeliverablePage> {
  const page = (items: LeadDeliverable[], nextCursor: string | null = null) => ({ conversationId: 'lead', items, nextCursor, ...SUMMARY });
  return {
    [KEY_A]: page([item('r2', { key: KEY_A, title: 'Resume' })], 'c1'),
    [`${KEY_A}:c1`]: page([item('r1', { key: KEY_A, title: 'Resume' })]),
    [KEY_OLD]: page([item('r0', { key: KEY_OLD, title: 'Old survey' })]),
    [OTHER_TASKS]: page([item('legacy', undefined, { projectPath: '/work/parallel' })]),
  };
}

function setup() {
  const reads: string[] = [];
  const handlers: Array<(frame: { kind?: unknown; conversationId?: unknown }) => void> = [];
  const opened: string[] = [];
  const store = pages();
  const card = createCoordinatorProgress({ open: true, finishedOpen: false, toggle() {}, toggleFinished() {}, now: () => NOW });
  document.body.append(card.el);
  const helper = createTaskDeliverables({
    conversationId: 'lead', projectPath: '/work/tofu', now: () => NOW,
    read: async (query) => {
      const key = query.cursor ? `${query.task}:${query.cursor}` : query.task!;
      reads.push(key);
      return store[key];
    },
    subscribe: handler => { handlers.push(handler); return () => {}; },
    opener: async () => ({ open: entry => { opened.push(entry.id); return true; }, openSource: () => true }),
  }, () => card.repaint());
  const view: CoordinatorProgressView = { kind: 'board', taskKeys: [KEY_A, KEY_B], board: {
    revision: 3, updatedAt: NOW, summary: 'Two tasks', tasks: [
      { title: 'Resume', status: 'in_progress', progress: 'Writing' }, { title: 'Deck', status: 'pending' }] } };
  card.render(view, { history() {}, retry() {} });
  card.attachDeliverables(helper);
  return { card, helper, reads, handlers, opened, store };
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
  expect(document.querySelector(`.coordinator-task-deliverables[data-task="${OTHER_TASKS}"] .lead-deliverable__meta`)!
    .textContent).toMatch(/^parallel · /);
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
