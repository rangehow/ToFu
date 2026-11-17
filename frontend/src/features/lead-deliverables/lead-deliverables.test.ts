// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { LeadDeliverable } from './model';
import { createDeliverableOpener, type DeliverableOpenPorts } from './open';
import { deliverableRow, type DeliverableRowOptions } from './rows';
import {
  appendDeliverables, deliverableDayLabel, deliverableDays, deliverablePreview, deliverableProject, deliverableVisual, reportDeliverable,
} from './model';

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

/* Rows of a board task (owner request 2026-11-17: each task expands to its reports and files). */
function rowsOf(items: LeadDeliverable[], options: Partial<DeliverableRowOptions> = {}) {
  const opened: string[] = [], sources: string[] = [];
  const opener = { open: (entry: LeadDeliverable) => { opened.push(entry.id); return true; },
    openSource: (entry: LeadDeliverable) => { sources.push(entry.id); return true; } };
  const list = document.createElement('ol');
  list.append(...items.map(entry => deliverableRow(entry, { opener: async () => opener, now: () => NOW, ...options })));
  document.body.append(list);
  return { rows: [...list.children] as HTMLElement[], opened, sources };
}
const flush = async () => { for (let index = 0; index < 5; index += 1) await Promise.resolve(); };

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

it('gives each row a type glyph and kind badge, the whole title, and a hover preview of the report', async () => {
  const { rows } = rowsOf([
    item(1, { reportKind: 'progress', summary: '**Outcome**: merged.\n- one\n\nSecond paragraph' }),
    item(2, { id: 'artifact:p', kind: 'file', title: 'resume.pdf', artifactId: 'p', summary: undefined }),
    item(3, { id: 'artifact:d', kind: 'deck', title: 'Q4 review.pptx', artifactId: 'd', summary: undefined }),
  ]);
  const [progress, pdf, deck] = rows;
  expect([progress, pdf, deck].map(row => row.dataset.visual)).toEqual(['progress', 'pdf', 'deck']);
  expect([progress, pdf, deck].every(row => row.querySelector('.lead-deliverable__icon svg'))).toBe(true);
  expect(pdf.querySelector('.lead-deliverable__kind')!.textContent).toBe('leadDeliverables.kind.pdf');
  expect(deck.querySelector('.lead-deliverable__kind')!.textContent).toBe('leadDeliverables.kind.deck');
  const preview = progress.querySelector<HTMLButtonElement>('.lead-deliverable__open')!.title;
  expect(preview.split('\n')[0]).toBe('Delivery 1');
  expect(preview).toContain('Outcome: merged.');
  expect(preview).not.toContain('Second paragraph');
  expect(progress.querySelector<HTMLButtonElement>('.lead-deliverable__source')!.getAttribute('aria-label')).toBe('leadDeliverables.source');
});

it('the presentation model: visual types, days, labels and previews', () => {
  expect(deliverableVisual(item(1))).toBe('report');
  expect(deliverableVisual(item(1, { reportKind: 'blocked' }))).toBe('blocked');
  expect(deliverableVisual(item(1, { kind: 'file', title: 'x', format: 'PDF' }))).toBe('pdf');
  expect(deliverableVisual(item(1, { kind: 'file', title: 'table.csv' }))).toBe('sheet');
  expect(deliverableVisual(item(1, { kind: 'file', title: 'notes.md' }))).toBe('file');
  expect(deliverableVisual(item(1, { kind: 'document' }))).toBe('document');
  expect(deliverableVisual(item(1, { kind: 'merge' }))).toBe('merge');
  const days = deliverableDays([item(1), item(2), item(3, { createdAt: NOW - 2 * 86_400_000 })]);
  expect(days.map(entry => entry.items.length)).toEqual([2, 1]);
  expect(deliverableDayLabel(days[0].start, NOW, 'zh-CN')).toBe('今天');
  expect(deliverableDayLabel(days[1].start, NOW, 'en')).toMatch(/Nov/);
  expect(deliverablePreview(item(4, { summary: '# Head\n\nrest', files: [{ artifactId: 'a', filename: 'a.md', kind: 'document' }] }), 'en'))
    .toMatch(/^Delivery 4\n.+\n\nHead\n\n↗ a\.md$/);
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
  const [first, second] = rowsOf([translated, item(2)]).rows;
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


it('a row opens the deliverable, each attached file and its source message', async () => {
  const files = [{ artifactId: 'pdf', filename: 'resume.pdf', kind: 'file' as const, path: 'out/resume.pdf' },
    { artifactId: 'deck', filename: 'talk.pptx', kind: 'deck' as const }];
  const { rows, opened, sources } = rowsOf([item(1, { reportKind: 'delivery', files }), item(2, { reportKind: 'decision' })]);
  const [delivered, decision] = rows;
  expect(delivered.querySelector('.lead-deliverable__kind')!.textContent).toBe('leadDeliverables.reportKind.delivery');
  expect(decision.dataset.reportKind).toBe('decision');
  expect(decision.querySelector('.lead-deliverable__files')).toBeNull();
  const chips = [...delivered.querySelectorAll<HTMLButtonElement>('.lead-deliverable__file')];
  expect(chips.map(chip => [chip.textContent, chip.title])).toEqual([['resume.pdf', 'out/resume.pdf'], ['talk.pptx', 'talk.pptx']]);
  delivered.querySelector<HTMLButtonElement>('.lead-deliverable__open')!.click();
  chips[1].click();
  delivered.querySelector<HTMLButtonElement>('.lead-deliverable__source')!.click();
  await flush();
  expect(opened).toEqual(['report:t1', 'artifact:deck']);
  expect(sources).toEqual(['report:t1']);
});

it("names a row's project only when asked (another project than the lead's)", () => {
  const plain = rowsOf([item(1, { projectPath: '/work/tofu' })]).rows[0];
  expect(plain.querySelector<HTMLElement>('.lead-deliverable__project')!.hidden).toBe(true);
  const other = rowsOf([item(2, { projectPath: '/work/parallel' })], { showProject: true }).rows[0];
  expect(other.querySelector<HTMLElement>('.lead-deliverable__project')!.hidden).toBe(false);
  expect(other.querySelector('.lead-deliverable__project')!.textContent).toBe('parallel');
});

it('a row reads in the document language when it is built', () => {
  locale.language = 'zh';
  document.documentElement.lang = 'zh-CN';
  const row = rowsOf([item(1, { reportKind: 'delivery', files: [{ artifactId: 'a', filename: 'notes.md', kind: 'document' }] })]).rows[0];
  expect(row.querySelector('.lead-deliverable__kind')!.textContent).toBe('zh:leadDeliverables.reportKind.delivery');
  expect(row.querySelector('.lead-deliverable__source')!.getAttribute('aria-label')).toBe('zh:leadDeliverables.source');
  expect(row.querySelector('.lead-deliverable__file')!.getAttribute('aria-label')).toBe('zh:leadDeliverables.openFile');
  expect(row.querySelector('.lead-deliverable__age')!.textContent).toMatch(/分钟前$/);
  document.documentElement.lang = 'en';
});
