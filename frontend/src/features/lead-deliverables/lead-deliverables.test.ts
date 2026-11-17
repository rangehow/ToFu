// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { LeadDeliverable } from './model';
import { createDeliverableOpener, type DeliverableOpenPorts } from './open';
import { deliverableRow } from './rows';
import { appendDeliverables, deliverableProject, reportDeliverable } from './model';

const locale = vi.hoisted(() => ({ language: 'en' }));
vi.mock('../../i18n', () => ({ t: (key: string) => (locale.language === 'en' ? key : `${locale.language}:${key}`) }));

const NOW = Date.UTC(2026, 10, 16, 12);

function item(index: number, extra: Partial<LeadDeliverable> = {}): LeadDeliverable {
  return { id: `report:t${index}`, kind: 'report', title: `Delivery ${index}`, projectPath: '/work/tofu', createdAt: NOW - index * 60_000,
    source: { conversationId: 'lead', turnId: `t${index}`, ordinal: index }, summary: `Body ${index}`, ...extra };
}

function ports() {
  const calls = { artifact: [] as string[], turn: [] as Array<[string, unknown]> };
  const open: DeliverableOpenPorts = {
    openArtifact: id => { calls.artifact.push(id); },
    openTurn: (conversation, turn) => { calls.turn.push([conversation, turn]); },
    markdown: source => `<p>${source}</p>`,
  };
  return { calls, open };
}

beforeEach(() => {
  // jsdom has no modal dialog implementation.
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement, value?: string) {
    this.returnValue = value ?? ''; this.removeAttribute('open'); this.dispatchEvent(new Event('close'));
  };
});
afterEach(() => { document.body.replaceChildren(); locale.language = 'en'; });

it('opens each deliverable itself: artifact panel, report card, Worker result Turn', () => {
  const { calls, open } = ports();
  const opener = createDeliverableOpener(open);
  expect(opener.open(item(1, { id: 'artifact:a', kind: 'deck', artifactId: 'a', summary: undefined }))).toBe(true);
  expect(calls.artifact).toEqual(['a']);
  expect(opener.open(item(2, { id: 'merge:x:w', kind: 'merge', summary: undefined,
    result: { conversationId: 'worker', turnId: 'wt', ordinal: 4 } }))).toBe(true);
  expect(calls.turn).toEqual([['worker', { turnId: 'wt', ordinal: 4 }]]);

  opener.open(item(3));
  const dialog = document.querySelector<HTMLDialogElement>('dialog.lead-deliverable-dialog')!;
  expect(dialog.hasAttribute('open')).toBe(true);
  // The conversation's own report_to_user card, opened, with the report message.
  const card = dialog.querySelector<HTMLDetailsElement>('details.ptool-coord-card[data-tool-card="report_to_user"]')!;
  expect(card.open).toBe(true);
  expect(card.textContent).toContain('Body 3');
});

it('the report reader and the secondary action go to the exact source Turn', async () => {
  const { calls, open } = ports();
  const opener = createDeliverableOpener(open);
  expect(opener.openSource(item(5))).toBe(true);
  expect(calls.turn).toEqual([['lead', { turnId: 't5', ordinal: 5 }]]);
  expect(opener.openSource(item(6, { source: null }))).toBe(false);

  opener.open(item(7));
  const source = [...document.querySelectorAll<HTMLButtonElement>('dialog .ui-dialog__footer button')]
    .find(button => button.textContent === 'leadDeliverables.source')!;
  source.click();
  await Promise.resolve();
  expect(calls.turn.at(-1)).toEqual(['lead', { turnId: 't7', ordinal: 7 }]);
});

it('the item model matches the server projection identities', () => {
  expect(reportDeliverable({ conversationId: 'lead', turnId: 't', ordinal: 3, title: 'Done', content: 'Done\n\nAll merged.',
    createdAt: 9, projectPath: '/p' })).toEqual({ id: 'report:t', kind: 'report', title: 'Done', projectPath: '/p', createdAt: 9,
    source: { conversationId: 'lead', turnId: 't', ordinal: 3 }, summary: 'All merged.' });
  expect(appendDeliverables([item(1)], [item(1), item(2)]).map(entry => entry.id)).toEqual(['report:t1', 'report:t2']);
  expect(deliverableProject('/a/b/tofu/')).toBe('tofu');
});

it('the report dialog shows the report kind and opens attached files', () => {
  const { calls, open } = ports();
  createDeliverableOpener(open).open(item(4, { reportKind: 'progress',
    files: [{ artifactId: 'md', filename: 'notes.md', kind: 'document' }] }));
  const dialog = document.querySelector<HTMLDialogElement>('dialog.lead-deliverable-dialog')!;
  const chip = dialog.querySelector<HTMLButtonElement>('.lead-deliverable-report__files button')!;
  expect(chip.textContent).toBe('notes.md');
  chip.click();
  expect(calls.artifact).toEqual(['md']);
  expect(dialog.hasAttribute('open')).toBe(false);
});

it("shows a report in its display translation, as the conversation does, keeping the original in the tooltip", async () => {
  const translated = item(1, { title: 'Shipped', summary: 'All merged.', translation: { title: '已交付', summary: '全部已合并。' } });
  const options = { opener: async () => createDeliverableOpener(ports().open), now: () => NOW };
  const first = deliverableRow(translated, options), second = deliverableRow(item(2), options);
  expect(first.querySelector('.lead-deliverable__title')!.textContent).toBe('已交付');
  expect(first.querySelector<HTMLButtonElement>('.lead-deliverable__open')!.title.split('\n').slice(0, 2)).toEqual(['已交付', 'Shipped']);
  expect(second.querySelector('.lead-deliverable__title')!.textContent).toBe('Delivery 2');
  const { open } = ports();
  createDeliverableOpener(open).open(translated);
  const dialog = document.querySelector<HTMLDialogElement>('dialog.lead-deliverable-dialog')!;
  expect(dialog.textContent).toContain('已交付');
  expect(dialog.textContent).toContain('全部已合并。');
  expect(dialog.textContent).not.toContain('All merged.');
});

/* Owner request 2026-11-17: a task on the board expands to these rows. */
it('a row names a report by its kind, opens it, its attached files and its source message', async () => {
  const opened: string[] = [], sources: string[] = [];
  const opener = { open: (entry: LeadDeliverable) => { opened.push(entry.id); return true; },
    openSource: (entry: LeadDeliverable) => { sources.push(entry.id); return true; } };
  const files = [{ artifactId: 'pdf', filename: 'resume.pdf', kind: 'file' as const, path: 'out/resume.pdf' },
    { artifactId: 'deck', filename: 'talk.pptx', kind: 'deck' as const }];
  const row = deliverableRow(item(1, { reportKind: 'delivery', files }), { opener: async () => opener, now: () => NOW,
    showProject: true });
  expect(row.dataset.reportKind).toBe('delivery');
  expect(row.querySelector('.lead-deliverable__kind')!.textContent).toBe('leadDeliverables.reportKind.delivery');
  expect(row.querySelector('.lead-deliverable__meta')!.textContent).toMatch(/^tofu · /);
  expect(deliverableRow(item(1), { opener: async () => opener, now: () => NOW })
    .querySelector('.lead-deliverable__meta')!.textContent).not.toMatch(/tofu/);
  const chips = [...row.querySelectorAll<HTMLButtonElement>('.lead-deliverable__file')];
  expect(chips.map(chip => [chip.textContent, chip.title])).toEqual([['resume.pdf', 'out/resume.pdf'], ['talk.pptx', 'talk.pptx']]);
  row.querySelector<HTMLButtonElement>('.lead-deliverable__open')!.click();
  chips[1].click();
  row.querySelector<HTMLButtonElement>('.lead-deliverable__source')!.click();
  await Promise.resolve(); await Promise.resolve();
  expect(opened).toEqual(['report:t1', 'artifact:deck']);
  expect(sources).toEqual(['report:t1']);
});
