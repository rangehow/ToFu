// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import {
  coordinatorTaskLimit, coordinatorTaskStatus, createCoordinatorProgress,
  type CoordinatorProgressActions, type CoordinatorProgressPorts,
} from './coordinator-progress';
import { coordinatorProgressHeadline, coordinatorProgressTally, coordinatorProgressText } from './coordinator-progress-summary';
import type { CoordinatorProgressBoard, CoordinatorProgressTranslation } from '../api/project-coordination.generated';
vi.mock('../i18n', () => ({ t: (key: string, args?: Record<string, unknown>) => (args ? `${key}:${JSON.stringify(args)}` : key) }));
const { readFileSync } = await vi.importActual<{ readFileSync(path: string, encoding: 'utf8'): string }>('node:fs');

const board: CoordinatorProgressBoard = {
  revision: 2, updatedAt: Date.UTC(2026, 10, 2, 9, 5), summary: 'Two tasks moving; one finished.',
  tasks: [
    { title: 'Summarize the article', status: 'pending', reason: 'Link to the article.' },
    { title: 'Fix the folder dialog', status: 'completed', progress: 'Merged and verified in the browser.' },
    { title: 'Research homestays', status: 'in_progress', progress: 'Four platforms compared.' },
    { title: 'Old spike', status: 'cancelled', reason: 'Superseded by the new installer.' },
    { title: 'Stop the page flicker', status: 'in_progress' },
  ],
};

const translation: CoordinatorProgressTranslation = { target: 'Chinese', texts: [
  { source: 'Two tasks moving; one finished.', text: '两项进行中，一项已完成。' },
  { source: 'Research homestays', text: '调研民宿' },
  { source: 'Four platforms compared.', text: '已比较四个平台。' },
  { source: 'Link to the article.', text: '文章链接' },
] };

function ports(overrides: Partial<CoordinatorProgressPorts> = {}): CoordinatorProgressPorts {
  return { open: true, finishedOpen: false, toggle: vi.fn(), toggleFinished: vi.fn(),
    now: () => Date.UTC(2026, 10, 2, 12, 0), ...overrides };
}
function actions(): CoordinatorProgressActions { return { history: vi.fn(), retry: vi.fn() }; }
const titles = (root: ParentNode) => [...root.querySelectorAll('.coordinator-progress-task-title')].map(node => node.textContent);
const visibleButtons = (root: ParentNode) => [...root.querySelectorAll('button')].filter(button => !button.hidden);

it('lists one line per task behind a status glyph: in-progress work first, finished work folded', () => {
  const { el: card, render } = createCoordinatorProgress(ports());
  render({ kind: 'board', board }, actions());
  expect(card.dataset.state).toBe('board');
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe(board.summary);
  expect(card.querySelector('.coordinator-progress-tally')?.textContent).toBe('coordinatorProgress.sidebar.active:{"count":2} · coordinatorProgress.sidebar.waiting:{"count":1}');
  const open = [...card.querySelectorAll(':scope > .coordinator-progress-body > .coordinator-progress-content > .coordinator-progress-tasks > li')];
  expect(open.map(row => [row.getAttribute('data-status'), row.querySelector('.coordinator-progress-task-title')?.textContent])).toEqual([
    ['in_progress', 'Research homestays'], ['in_progress', 'Stop the page flicker'], ['waiting', 'Summarize the article']]);
  const status = (row: Element) => row.querySelector('.coordinator-progress-task-status')?.textContent;
  expect(open.map(status)).toEqual(['coordinatorProgress.status.in_progress', 'coordinatorProgress.status.in_progress',
    'coordinatorProgress.status.waiting']);
  const fold = card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  expect(fold.open).toBe(false);
  expect(fold.querySelector('summary')?.textContent).toBe('coordinatorProgress.finished:{"count":2}');
  expect(titles(fold)).toEqual(['Fix the folder dialog', 'Old spike']);
  expect([...fold.querySelectorAll('li')].map(status))
    .toEqual(['coordinatorProgress.status.completed', 'coordinatorProgress.status.cancelled']);
  expect(card.querySelector('time')?.getAttribute('datetime')).toBe(new Date(board.updatedAt).toISOString());
  // The head is the line a folded card shows: title, state, summary, then the update time.
  expect([...card.querySelector('summary')!.children].map(node => node.className)).toEqual(['coordinator-progress-title',
    'coordinator-progress-tally', 'coordinator-progress-summary', 'coordinator-progress-updated']);
  // No session, worker, reference, usage or question vocabulary ever reaches the board.
  expect(card.textContent).not.toMatch(/projectCoordination\.(workList|dependencyGraph|report)|coordinatorBudget|coordinatorPlan/);
});

it('makes a recorded wait, an unexplained pending row and a cancellation readable without hover', () => {
  const { el: card, render } = createCoordinatorProgress(ports({ finishedOpen: true }));
  render({ kind: 'board', board: { ...board, tasks: [...board.tasks, { title: 'Not dispatched yet', status: 'pending' }] } }, actions());
  const note = (status: string) => card.querySelector(`[data-status="${status}"] .coordinator-progress-task-note`)?.textContent;
  expect(note('waiting')).toBe('coordinatorProgress.waitReason:{"reason":"Link to the article."}');
  expect(note('pending')).toBe('coordinatorProgress.startUnknown');
  expect(note('cancelled')).toBe('coordinatorProgress.cancelReason:{"reason":"Superseded by the new installer."}');
  expect(note('in_progress')).toBe('Four platforms compared.');
});

it('reads a typed blocker as the wait, keeping a bare assignment wait to its status badge', () => {
  const { el: card, render } = createCoordinatorProgress(ports());
  render({ kind: 'board', board: { ...board, tasks: [
    { title: 'Recover the backup', status: 'pending',
      blocker: { kind: 'needs_external_resource', resource: 'the read-only snapshot',
        check: 'a restore rehearsal passes', ownerAction: 'confirm receipt on the phone' } },
    { title: 'Ship the folder fix', status: 'pending',
      blocker: { kind: 'active_dispatch', assignmentId: 'assignment-1' } },
  ] } }, actions());
  const rows = [...card.querySelectorAll('.coordinator-progress-tasks > li')];
  expect(rows.map(row => row.getAttribute('data-status'))).toEqual(['waiting', 'waiting']);
  const note = (index: number) => rows[index].querySelector('.coordinator-progress-task-note')?.textContent;
  // The model's own resource and its proving check read as one wait; an id-only wait stays in the badge.
  expect(note(0)).toBe('coordinatorProgress.waitReason:{"reason":"the read-only snapshot — a restore rehearsal passes · confirm receipt on the phone"}');
  expect(note(1)).toBeUndefined();
});

it('renders an unavailable resource as a limit, never a waiting badge, in both languages', () => {
  type Blocker = NonNullable<CoordinatorProgressBoard['tasks'][number]['blocker']>;
  const limit: Blocker = { kind: 'needs_external_resource', resource: 'the deleted archive',
    check: 'the archive lists the missing month', unavailable: true,
    limit: 'replay before March stays unverified' };
  const actionable: Blocker = { kind: 'needs_external_resource', resource: 'the read-only snapshot',
    check: 'a restore rehearsal passes', ownerAction: 'confirm receipt on the phone' };
  expect(coordinatorTaskLimit(limit)).toBe(true);
  expect(coordinatorTaskLimit(actionable)).toBe(false);
  expect(coordinatorTaskStatus('pending', 'note', limit)).toBe('limited');
  expect(coordinatorTaskStatus('pending', undefined, actionable)).toBe('waiting');
  const { el: card, render } = createCoordinatorProgress(ports());
  render({ kind: 'board', board: { ...board, tasks: [
    { title: 'Replay the lost month', status: 'pending', blocker: limit },
  ] } }, actions());
  const row = card.querySelector('.coordinator-progress-tasks > li')!;
  expect(row.getAttribute('data-status')).toBe('limited');
  expect(row.querySelector('.coordinator-progress-task-status')?.textContent).toBe('coordinatorProgress.status.limited');
  expect(row.querySelector('.coordinator-progress-task-note')?.textContent)
    .toBe('coordinatorProgress.limitReason:{"reason":"the deleted archive — replay before March stays unverified"}');
  // Both authored languages say limit, not waiting.
  for (const code of ['en', 'zh']) {
    const catalog = JSON.parse(readFileSync(`frontend/src/i18n/locales/${code}.json`, 'utf8'));
    expect(catalog['coordinatorProgress.status.limited']).toBeTruthy();
    expect(catalog['coordinatorProgress.limitReason']).toContain('{reason}');
    expect(catalog['coordinatorProgress.limitReason']).not.toMatch(/wait|等待/i);
  }
});

it('shows the board\'s display translation and toggles to the original as written', () => {
  const { el: card, render } = createCoordinatorProgress(ports());
  document.body.replaceChildren(card);
  render({ kind: 'board', board, translation }, actions());
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe('两项进行中，一项已完成。');
  expect(titles(card)).toEqual(['调研民宿', 'Stop the page flicker', 'Summarize the article', 'Fix the folder dialog', 'Old spike']);
  expect(card.querySelector('.coordinator-progress-task-note')?.textContent).toBe('已比较四个平台。');
  const pending = [...card.querySelectorAll<HTMLElement>('.coordinator-progress-task')].find(item => item.dataset.status === 'waiting')!;
  expect(pending.querySelector('.coordinator-progress-task-note')?.textContent).toContain('文章链接');
  const toggle = visibleButtons(card).find(button => button.textContent === 'chat.bilingualOriginal')!;
  expect(toggle.getAttribute('aria-pressed')).toBe('false');
  toggle.focus();
  toggle.click();
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe(board.summary);
  expect(titles(card)[0]).toBe('Research homestays');
  expect(toggle.getAttribute('aria-pressed')).toBe('true');
  // The toggle is one stable control: its focus survives its own repaint.
  expect(document.activeElement).toBe(toggle);
  toggle.click();
  expect(titles(card)[0]).toBe('调研民宿');
  // Nothing to switch when the translation changes no text, or there is none.
  render({ kind: 'board', board, translation: { target: 'Chinese', texts: [{ source: 'Old spike', text: 'Old spike' }] } }, actions());
  expect(toggle.hidden).toBe(true);
  render({ kind: 'board', board }, actions());
  expect(toggle.hidden).toBe(true);
  card.remove();
  expect(coordinatorProgressText(translation, 'Research homestays')).toBe('调研民宿');
  expect(coordinatorProgressText(translation, 'Old spike')).toBe('Old spike');
  expect(coordinatorProgressText(undefined, 'Old spike')).toBe('Old spike');
});

it('keeps one card and its footer control attached across renders, binding the latest actions', () => {
  const footer = document.createElement('div');
  footer.className = 'router-worker-model';
  const p = ports({ footer });
  const { el: card, render } = createCoordinatorProgress(p);
  document.body.replaceChildren(card);
  const first = actions();
  render({ kind: 'board', board }, first);
  const commands = card.querySelector('.coordinator-progress-actions')!;
  expect(commands.firstElementChild).toBe(footer);
  const observer = new MutationObserver(() => undefined);
  observer.observe(card, { childList: true, subtree: true });
  const latest = actions();
  render({ kind: 'board', board: { ...board, revision: 3, summary: 'Research done.' } }, latest);
  render({ kind: 'unavailable' }, latest);
  const moved = observer.takeRecords().flatMap(record => [...record.removedNodes, ...record.addedNodes]);
  observer.disconnect();
  expect(moved.some(node => node === footer || node.contains(footer))).toBe(false);
  expect(commands.firstElementChild).toBe(footer);
  visibleButtons(card).find(button => button.textContent === 'coordinatorProgress.history')!.click();
  expect(first.history).not.toHaveBeenCalled();
  expect(latest.history).toHaveBeenCalledOnce();
  card.remove();
});

it('reports fold changes and offers only the explicit task history', () => {
  const p = ports();
  const a = actions();
  const { el: card, render } = createCoordinatorProgress(p);
  render({ kind: 'board', board }, a);
  card.open = false; card.dispatchEvent(new Event('toggle'));
  expect(p.toggle).toHaveBeenCalledWith(false);
  const fold = card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  fold.open = true; fold.dispatchEvent(new Event('toggle'));
  expect(p.toggleFinished).toHaveBeenCalledWith(true);
  // The finished fold stays as the reader left it across live updates.
  render({ kind: 'board', board: { ...board, revision: 3 } }, a);
  expect(card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')?.open).toBe(true);
  const buttons = visibleButtons(card);
  expect(buttons.map(button => button.textContent)).toEqual(['coordinatorProgress.history']);
  buttons[0].click();
  expect(a.history).toHaveBeenCalledOnce();
});

it('folds to a head that still states the board, an empty board or an unavailable one', () => {
  const { el: folded, render } = createCoordinatorProgress(ports({ open: false }));
  render({ kind: 'board', board }, actions());
  expect(folded.open).toBe(false);
  const head = folded.querySelector('summary')!;
  expect(head.querySelector('.coordinator-progress-tally')?.textContent).toBe('coordinatorProgress.sidebar.active:{"count":2} · coordinatorProgress.sidebar.waiting:{"count":1}');
  expect(head.querySelector('.coordinator-progress-summary')?.textContent).toBe(board.summary);
  for (const kind of ['empty', 'unavailable'] as const) {
    render({ kind }, actions());
    expect(folded.open).toBe(false);
    expect([...head.children].map(node => node.textContent)).toEqual(['coordinatorProgress.title', `coordinatorProgress.${kind}`]);
  }
});

it('distinguishes an empty board from an unavailable one and retries only on request', () => {
  const empty = createCoordinatorProgress(ports());
  empty.render({ kind: 'empty' }, actions());
  expect(empty.el.dataset.state).toBe('empty');
  expect(empty.el.textContent).toContain('coordinatorProgress.empty');
  const a = actions();
  const unavailable = createCoordinatorProgress(ports());
  unavailable.render({ kind: 'unavailable' }, a);
  expect(unavailable.el.querySelector('[role="status"]')?.textContent).toBe('coordinatorProgress.unavailable');
  expect(a.retry).not.toHaveBeenCalled();
  visibleButtons(unavailable.el).find(button => button.textContent === 'projectCoordination.retry')!.click();
  expect(a.retry).toHaveBeenCalledOnce();
});

it('summarizes the board for the sidebar without claiming progress the board does not state', () => {
  expect(coordinatorProgressTally(board)).toEqual({ active: 2, pending: 0, waiting: 1, finished: 2, total: 5 });
  expect(coordinatorProgressHeadline(board)).toBe('coordinatorProgress.sidebar.active:{"count":2}');
  const done = { ...board, tasks: board.tasks.filter(task => task.status === 'completed' || task.status === 'cancelled') };
  expect(coordinatorProgressHeadline(done)).toBe('coordinatorProgress.sidebar.allDone');
  expect(coordinatorProgressHeadline({ ...board, tasks: [{ title: 'Next', status: 'pending' }] })).toBe('coordinatorProgress.sidebar.pending:{"count":1}');
  expect(coordinatorProgressHeadline({ ...board, tasks: [] })).toBeNull();
  expect(coordinatorProgressHeadline(null)).toBeNull();
});

it('labels only the board report age and never derives waiting or execution from age or a note', () => {
  document.documentElement.lang = 'en';
  const card = createCoordinatorProgress(ports({ now: () => board.updatedAt + 3 * 3_600_000 }));
  const snapshot: CoordinatorProgressBoard = { ...board, tasks: [
    { title: 'Await source', status: 'pending', reason: 'Upload the source article.' },
    { title: 'Prepare release', status: 'pending', progress: 'Release checklist drafted.' },
    { title: 'Compare platforms', status: 'in_progress', progress: 'Four platforms compared.' },
  ] };
  card.render({ kind: 'board', board: snapshot }, actions());
  expect(card.el.querySelector('.coordinator-progress-updated')?.textContent).toContain('3 hours ago');
  expect(card.el.querySelector('[data-status="pending"] .coordinator-progress-task-note')?.textContent)
    .toBe('coordinatorProgress.startUnknown Release checklist drafted.');
  expect(card.el.querySelector('[data-status="waiting"]')?.textContent).toContain('Upload the source article.');
  expect(card.el.querySelectorAll('.coordinator-progress-task time')).toHaveLength(0);
  card.render({ kind: 'board', board: { ...snapshot, updatedAt: board.updatedAt - 365 * 86_400_000 } }, actions());
  expect([...card.el.querySelectorAll('.coordinator-progress-task')].map(row => row.getAttribute('data-status')))
    .toEqual(['in_progress', 'waiting', 'pending']);
  expect(card.el.querySelector('.coordinator-progress-updated')?.textContent).toContain('2025');
  card.render({ kind: 'unavailable' }, actions());
  expect(card.el.querySelector('time')).toBeNull();
  expect(card.el.querySelectorAll('.coordinator-progress-task')).toHaveLength(0);
});

const KEY_A = 'a'.repeat(32), KEY_B = 'b'.repeat(32);
it('distinguishes exact positive receipts from authored progress without claiming overlap coverage or completion', () => {
  const card = createCoordinatorProgress(ports());
  const openWorker = vi.fn();
  const current = { ...board, tasks: [{ title: 'Linked', status: 'in_progress' as const },
    { title: 'Manual or older request', status: 'in_progress' as const }] };
  for (const phase of ['queued', 'active', 'awaiting_review', 'awaiting_delivery', 'delivered', 'held', 'ended', 'unknown'] as const) {
    card.render({ kind: 'board', board: current, taskKeys: [KEY_A, KEY_B], workEvidence: {
      scope: 'latest_request', coverage: 'unknown', requestId: 'request-b', sourceTurnId: 'owner-turn-b',
      rows: [{ assignmentId: 'assignment-b', conversationId: 'worker-b', taskKeys: [KEY_A], phase }],
    } }, { ...actions(), openWorker });
    const rows = card.el.querySelectorAll('.coordinator-progress-task');
    expect(rows[0].textContent).toContain('projectCoordination.graph.assignmentCoverage');
    expect(rows[0].textContent).toContain(JSON.stringify({ requestId: 'request-b', sourceTurnId: 'owner-turn-b',
      assignmentId: 'assignment-b', phase }));
    expect(card.el.querySelector('.coordinator-progress-evidence-scope')?.textContent).toContain('"coverage":"unknown"');
    expect(card.el.querySelector('.coordinator-progress-evidence-scope')?.textContent).toContain('"requestId":"request-b"');
    expect(rows[0].getAttribute('data-status')).toBe('in_progress');
    expect(rows[1].textContent).toContain('projectCoordination.graph.progressUnknown');
    expect(rows[1].querySelector('[data-assignment-id]')).toBeNull();
    expect(card.el.textContent).not.toMatch(/no owner|unowned/i);
  }
  card.el.querySelector<HTMLButtonElement>('.coordinator-progress-live-receipt button')!.click();
  expect(openWorker).toHaveBeenCalledWith('worker-b');
  card.invalidateEvidence();
  expect(card.el.querySelector('[data-assignment-id]')).toBeNull();
  expect(card.el.querySelector('.coordinator-progress-live-receipt')).toBeNull();
  expect(card.el.querySelector('.coordinator-progress-evidence-scope')?.textContent)
    .toBe('projectCoordination.graph.assignmentsUnavailable');
  card.render({ kind: 'board', board: current, taskKeys: [KEY_A, KEY_B] }, actions());
  expect(card.el.querySelector('[data-assignment-id]')).toBeNull();
});

it('shows two exact request receipts for one task without counting them or replacing authored status', () => {
  const card = createCoordinatorProgress(ports());
  const openWorker = vi.fn();
  const current = { ...board, tasks: [{ title: 'Same title', status: 'pending' as const },
    { title: 'Same title', status: 'pending' as const }] };
  card.render({ kind: 'board', board: current, taskKeys: [KEY_A, KEY_B], workEvidence: {
    scope: 'latest_request', coverage: 'unknown', requestId: 'request-b', sourceTurnId: 'turn-b',
    rows: [{ assignmentId: 'assignment-b', conversationId: 'worker-b', taskKeys: [KEY_A], phase: 'active' }],
    additionalRequests: [{ requestId: 'request-a', sourceTurnId: 'turn-a', rows: [
      { assignmentId: 'assignment-a', conversationId: 'worker-a', taskKeys: [KEY_A], phase: 'unknown' },
      { assignmentId: 'assignment-b', conversationId: 'duplicate-worker', taskKeys: [KEY_A], phase: 'delivered' },
      { assignmentId: 'assignment-a', conversationId: 'duplicate-worker-a', taskKeys: [KEY_A], phase: 'active' },
    ] }],
  } }, { ...actions(), openWorker });
  const rows = card.el.querySelectorAll('.coordinator-progress-task');
  const receipts = [...rows[0].querySelectorAll<HTMLElement>('[data-assignment-id]')];
  expect(receipts.map(receipt => [receipt.dataset.assignmentId, receipt.dataset.requestId])).toEqual([
    ['assignment-b', 'request-b'], ['assignment-a', 'request-a'],
  ]);
  expect(receipts.map(receipt => JSON.parse(receipt.querySelector('code')!.textContent!))).toEqual([
    { requestId: 'request-b', sourceTurnId: 'turn-b', assignmentId: 'assignment-b', phase: 'active' },
    { requestId: 'request-a', sourceTurnId: 'turn-a', assignmentId: 'assignment-a', phase: 'unknown' },
  ]);
  expect(receipts.map(receipt => receipt.querySelector('code')!.dataset.requestId)).toEqual(['request-b', 'request-a']);
  expect(receipts[1].textContent).toContain('projectCoordination.graph.status.unknown');
  expect(rows[0].getAttribute('data-status')).toBe('pending');
  expect(card.el.querySelector('.coordinator-progress-tally')?.textContent).toBe('coordinatorProgress.sidebar.pending:{"count":2}');
  expect(rows[0].querySelector('.coordinator-progress-task-count')).toBeNull();
  expect(rows[1].querySelector('[data-assignment-id]')).toBeNull();
  expect(rows[1].textContent).toContain('projectCoordination.graph.progressUnknown');
  expect(card.el.textContent).not.toMatch(/no owner|unowned/i);
  const workerButtons = rows[0].querySelectorAll<HTMLButtonElement>('.coordinator-progress-live-receipt button');
  workerButtons[0].click(); workerButtons[1].click();
  expect(openWorker.mock.calls).toEqual([['worker-b'], ['worker-a']]);
  expect(card.invalidateEvidence()).toBe(true);
  expect(card.el.querySelector('[data-assignment-id], [data-request-id]')).toBeNull();
  expect(card.el.querySelectorAll('.coordinator-progress-live-receipt button, .coordinator-progress-live-receipt code')).toHaveLength(0);
  expect(card.el.querySelector('.coordinator-progress-evidence-scope')?.textContent)
    .toBe('projectCoordination.graph.assignmentsUnavailable');
  expect(card.el.querySelectorAll('.coordinator-progress-live-receipt')).toHaveLength(0);
  card.repaint();
  expect(card.el.querySelector('[data-assignment-id], [data-request-id]')).toBeNull();
  expect(card.invalidateEvidence()).toBe(false);
});

it('states unavailable assignment data once when the board observed no receipts at all', () => {
  const card = createCoordinatorProgress(ports());
  const current = { ...board, tasks: [
    { title: 'Await the source', status: 'pending' as const, reason: 'Upload the article.' },
    { title: 'Compare platforms', status: 'in_progress' as const },
  ] };
  card.render({ kind: 'board', board: current, taskKeys: [KEY_A, KEY_B], workEvidence: {
    scope: 'latest_request', coverage: 'unknown', requestId: null, sourceTurnId: null, rows: [],
  } }, actions());
  expect(card.el.querySelectorAll('.coordinator-progress-task')).toHaveLength(2);
  expect(card.el.querySelectorAll('.coordinator-progress-live-receipt')).toHaveLength(0);
  expect(card.el.textContent).not.toContain('projectCoordination.graph.progressUnknown');
  const coverage = card.el.querySelector('.coordinator-progress-evidence-scope');
  expect(coverage?.textContent).toContain('projectCoordination.graph.assignmentsUnavailable');
  expect(coverage?.textContent).toContain('"requestId":null');
  expect([...card.el.querySelectorAll('.coordinator-progress-task')].map(row => row.getAttribute('data-status')))
    .toEqual(['in_progress', 'waiting']);
  card.render({ kind: 'board', board: current, taskKeys: [KEY_A, KEY_B] }, actions());
  expect(card.el.querySelectorAll('.coordinator-progress-live-receipt')).toHaveLength(0);
  expect(card.el.textContent).toContain('projectCoordination.graph.assignmentsUnavailable');
});

it('does not claim assignment data is unavailable when the request itself was read', () => {
  const card = createCoordinatorProgress(ports());
  const current = { ...board, tasks: [{ title: 'Unlinked worker', status: 'in_progress' as const }] };
  card.render({ kind: 'board', board: current, taskKeys: [KEY_A], workEvidence: {
    scope: 'latest_request', coverage: 'unknown', requestId: 'request-b', sourceTurnId: 'turn-b', rows: [],
  } }, actions());
  const coverage = card.el.querySelector('.coordinator-progress-evidence-scope');
  expect(coverage?.textContent).toContain('projectCoordination.graph.progressUnknown');
  expect(coverage?.textContent).not.toContain('projectCoordination.graph.assignmentsUnavailable');
  expect(coverage?.textContent).toContain('"requestId":"request-b"');
});

it('never links either request by task title or by a missing task key', () => {
  const card = createCoordinatorProgress(ports());
  const current = { ...board, tasks: [{ title: KEY_A, status: 'in_progress' as const }] };
  const workEvidence = {
    scope: 'latest_request' as const, coverage: 'unknown' as const, requestId: 'request-b', sourceTurnId: 'turn-b',
    rows: [{ assignmentId: 'assignment-b', conversationId: 'worker-b', taskKeys: [KEY_A], phase: 'active' as const }],
    additionalRequests: [{ requestId: 'request-a', sourceTurnId: 'turn-a', rows: [
      { assignmentId: 'assignment-a', conversationId: 'worker-a', taskKeys: [KEY_A], phase: 'active' as const },
    ] }],
  };
  for (const taskKeys of [undefined, [KEY_B], []]) {
    card.render({ kind: 'board', board: current, taskKeys, workEvidence }, actions());
    expect(card.el.querySelector('[data-assignment-id]')).toBeNull();
    expect(card.el.querySelector('.coordinator-progress-task-title')?.textContent).toBe(KEY_A);
    if (taskKeys?.length) expect(card.el.querySelector('.coordinator-progress-live-receipt')?.textContent)
      .toBe('projectCoordination.graph.progressUnknown');
  }
});
