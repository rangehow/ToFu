// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { LeadDeliverable, LeadDeliverablePage } from './model';
import { createDeliverableOpener, type DeliverableOpenPorts } from './open';
import { createLeadDeliverables, type LeadDeliverablesPushFrame, type LeadDeliverablesTarget } from './list';
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

function list(pages: Map<string, LeadDeliverablePage>) {
  const reads: Array<[LeadDeliverablesTarget, string | undefined]> = [];
  const handlers: Array<(frame: LeadDeliverablesPushFrame) => void> = [];
  const opened: string[] = [], sources: string[] = [];
  const view = createLeadDeliverables({
    read: async (target, cursor) => {
      reads.push([target, cursor]);
      return pages.get(cursor || '')!;
    },
    subscribe: handler => { handlers.push(handler); return () => {}; },
    onReconnect: () => () => {},
    opener: { open: entry => { opened.push(entry.id); return true; }, openSource: entry => { sources.push(entry.id); return true; } },
    now: () => NOW,
    readExpanded: () => true,
  });
  document.body.append(view.root);
  return { view, reads, handlers, opened, sources };
}

const flush = async () => { for (let index = 0; index < 5; index += 1) await Promise.resolve(); };
const rows = () => [...document.querySelectorAll<HTMLElement>('.lead-deliverable')].map(row => row.dataset.deliverableId);

it('pages the whole list newest first without truncation and shows title, kind, project and age', async () => {
  const first = Array.from({ length: 64 }, (_, index) => item(index));
  const second = Array.from({ length: 10 }, (_, index) => item(64 + index));
  const { view, reads } = list(new Map([
    ['', { conversationId: 'lead', items: first, nextCursor: 'c1' }],
    ['c1', { conversationId: 'lead', items: second, nextCursor: null }],
  ]));
  view.show({ leadId: 'lead', path: '/work/tofu' });
  await flush();
  expect(rows()).toEqual(first.map(entry => entry.id));
  expect(document.querySelector('.lead-deliverables__count')!.textContent).toBe('64+');
  const row = document.querySelector<HTMLElement>('.lead-deliverable')!;
  expect(row.querySelector('.lead-deliverable__kind')!.textContent).toBe('leadDeliverables.kind.report');
  expect(row.querySelector('.lead-deliverable__title')!.textContent).toBe('Delivery 0');
  // Every item is in one project: the heading names it once and the rows show their age.
  expect(document.querySelector('.lead-deliverables__scope')!.textContent).toBe('tofu');
  expect(document.querySelector('.lead-deliverables')!.getAttribute('data-shared-project')).toBe('true');
  expect(row.querySelector('.lead-deliverable__age')!.textContent).not.toBe('');
  const more = document.querySelector<HTMLButtonElement>('.lead-deliverables__more')!;
  expect(more.hidden).toBe(false);
  more.click();
  await flush();
  expect(reads.map(([, cursor]) => cursor)).toEqual([undefined, 'c1']);
  expect(rows()).toEqual([...first, ...second].map(entry => entry.id));
  expect(more.hidden).toBe(true);
  expect(document.querySelector('.lead-deliverables__count')!.textContent).toBe('74');
});

it('a row opens the deliverable; its secondary control opens the source message', async () => {
  const { view, opened, sources } = list(new Map([['', { conversationId: 'lead', items: [item(1)], nextCursor: null }]]));
  view.show({ leadId: 'lead', path: '' });
  await flush();
  document.querySelector<HTMLButtonElement>('.lead-deliverable__open')!.click();
  document.querySelector<HTMLButtonElement>('.lead-deliverable__source')!.click();
  expect(opened).toEqual(['report:t1']);
  expect(sources).toEqual(['report:t1']);
});

it('a delivery push for the shown lead re-reads the first page; other leads and kinds read nothing', async () => {
  const pages = new Map([['', { conversationId: 'lead', items: [item(1)], nextCursor: null }]]);
  const { view, reads, handlers } = list(pages);
  view.show({ leadId: 'lead', path: '/work/tofu' });
  await flush();
  handlers.forEach(handler => handler({ kind: 'lead_deliverable', conversationId: 'other-lead' }));
  handlers.forEach(handler => handler({ kind: 'coordinator_progress', conversationId: 'lead' }));
  await flush();
  expect(reads).toHaveLength(1);
  pages.set('', { conversationId: 'lead', items: [item(0), item(1)], nextCursor: null });
  handlers.forEach(handler => handler({ kind: 'lead_deliverable', conversationId: 'lead' }));
  await flush();
  expect(reads).toHaveLength(2);
  expect(rows()).toEqual(['report:t0', 'report:t1']);
});

it('the item model matches the server projection identities', () => {
  expect(reportDeliverable({ conversationId: 'lead', turnId: 't', ordinal: 3, title: 'Done', content: 'Done\n\nAll merged.',
    createdAt: 9, projectPath: '/p' })).toEqual({ id: 'report:t', kind: 'report', title: 'Done', projectPath: '/p', createdAt: 9,
    source: { conversationId: 'lead', turnId: 't', ordinal: 3 }, summary: 'All merged.' });
  expect(appendDeliverables([item(1)], [item(1), item(2)]).map(entry => entry.id)).toEqual(['report:t1', 'report:t2']);
  expect(deliverableProject('/a/b/tofu/')).toBe('tofu');
});

/* Owner request 2026-11-17: the section lists what the lead sent the owner,
 * every report with the files it attached, open until the owner folds it. */
it('names each report by its kind and opens its attached files from chips', async () => {
  const files = [{ artifactId: 'pdf', filename: 'resume.pdf', kind: 'file' as const, path: 'out/resume.pdf' },
    { artifactId: 'deck', filename: 'talk.pptx', kind: 'deck' as const }];
  const { view, opened } = list(new Map([['', { conversationId: 'lead', nextCursor: null, items: [
    item(1, { reportKind: 'delivery', files }), item(2, { reportKind: 'decision' })] }]]));
  view.show({ leadId: 'lead', path: '/work/tofu' });
  await flush();
  const [delivered, decision] = [...document.querySelectorAll<HTMLElement>('.lead-deliverable')];
  expect(delivered.querySelector('.lead-deliverable__kind')!.textContent).toBe('leadDeliverables.reportKind.delivery');
  expect(decision.dataset.reportKind).toBe('decision');
  expect(decision.querySelector('.lead-deliverable__files')).toBeNull();
  const chips = [...delivered.querySelectorAll<HTMLButtonElement>('.lead-deliverable__file')];
  expect(chips.map(chip => [chip.textContent, chip.title])).toEqual([['resume.pdf', 'out/resume.pdf'], ['talk.pptx', 'talk.pptx']]);
  chips[1].click();
  expect(opened).toEqual(['artifact:deck']);
});

it('is open by default in the rail, folded by default in the narrow fold, and an owner choice wins over both', async () => {
  const choices = new Map<string, boolean>();
  const view = createLeadDeliverables({
    read: async () => ({ conversationId: 'lead', items: [item(1)], nextCursor: null }),
    subscribe: () => () => {}, onReconnect: () => () => {},
    opener: { open: () => true, openSource: () => true },
    readExpanded: id => choices.get(id) ?? null, writeExpanded: (id, value) => { choices.set(id, value); },
  });
  document.body.append(view.root);
  const toggle = view.root.querySelector<HTMLButtonElement>('.lead-deliverables__toggle')!;
  view.place('fold');
  view.show({ leadId: 'lead', path: '' });
  await flush();
  expect(view.root.dataset.placement).toBe('fold');
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  view.place('rail');
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  toggle.click();
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  view.place('fold');
  view.place('rail');
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
});

it('gives each row a type glyph and kind badge, the whole title, and a hover preview of the report', async () => {
  const { view } = list(new Map([['', { conversationId: 'lead', nextCursor: null, items: [
    item(1, { reportKind: 'progress', summary: '**Outcome**: merged.\n- one\n\nSecond paragraph' }),
    item(2, { id: 'artifact:p', kind: 'file', title: 'resume.pdf', artifactId: 'p', summary: undefined }),
    item(3, { id: 'artifact:d', kind: 'deck', title: 'Q4 review.pptx', artifactId: 'd', summary: undefined }),
  ] }]]));
  view.show({ leadId: 'lead', path: '/work/tofu' });
  await flush();
  const [progress, pdf, deck] = [...document.querySelectorAll<HTMLElement>('.lead-deliverable')];
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

it('groups rows under day headings once the list spans more than one day', async () => {
  const day = 86_400_000;
  const pages = new Map([['', { conversationId: 'lead', nextCursor: null, items: [item(1), item(2)] }]]);
  const { view } = list(pages);
  view.show({ leadId: 'lead', path: '' });
  await flush();
  expect(document.querySelectorAll('.lead-deliverables__day-label')).toHaveLength(0);
  view.hide();
  const spread = list(new Map([['', { conversationId: 'lead', nextCursor: null, items: [
    item(1), item(2, { createdAt: NOW - day }), item(3, { createdAt: NOW - 3 * day })] }]]));
  document.body.replaceChildren(spread.view.root);
  spread.view.show({ leadId: 'other', path: '' });
  await flush();
  const labels = [...document.querySelectorAll('.lead-deliverables__day-label')].map(node => node.textContent);
  expect(labels).toHaveLength(3);
  expect(labels.slice(0, 2)).toEqual(['Today', 'Yesterday']);
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

it('is open by default and remembers a fold', async () => {
  const view = createLeadDeliverables({
    read: async () => ({ conversationId: 'lead', items: [], nextCursor: null }),
    subscribe: () => () => {}, onReconnect: () => () => {},
    opener: { open: () => true, openSource: () => true }, readExpanded: () => null,
  });
  document.body.append(view.root);
  view.show({ leadId: 'lead', path: '' });
  await flush();
  expect(view.root.querySelector('.lead-deliverables__toggle')!.getAttribute('aria-expanded')).toBe('true');
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

it('a language switch repaints every label, row and control in the new language', async () => {
  const { view } = list(new Map([['', { conversationId: 'lead', items: [item(1, { reportKind: 'delivery', files: [
    { artifactId: 'a', filename: 'notes.md', kind: 'document' }] })], nextCursor: 'c1' }]]));
  view.show({ leadId: 'lead', path: '/work/tofu' });
  await flush();
  locale.language = 'zh';
  document.documentElement.lang = 'zh-CN';
  window.dispatchEvent(new CustomEvent('tofu:language-change', { detail: { language: 'zh' } }));
  const root = view.root;
  expect(root.querySelector('.lead-deliverables__label')!.textContent).toBe('zh:leadDeliverables.title');
  expect(root.querySelector('.lead-deliverable__kind')!.textContent).toBe('zh:leadDeliverables.reportKind.delivery');
  expect(root.querySelector('.lead-deliverable__source')!.getAttribute('aria-label')).toBe('zh:leadDeliverables.source');
  expect(root.querySelector('.lead-deliverable__files')!.getAttribute('aria-label')).toBe('zh:leadDeliverables.files');
  expect(root.querySelector('.lead-deliverable__file')!.getAttribute('aria-label')).toBe('zh:leadDeliverables.openFile');
  expect(root.querySelector('.lead-deliverables__more')!.textContent).toBe('zh:leadDeliverables.more');
  expect(root.querySelector('.lead-deliverables__retry')!.textContent).toBe('zh:leadDeliverables.retry');
  // The age follows the document language: Chinese relative time, no English left.
  expect(root.querySelector('.lead-deliverable__age')!.textContent).toMatch(/分钟前$/);
  view.destroy();
  document.documentElement.lang = 'en';
  locale.language = 'en';
  window.dispatchEvent(new CustomEvent('tofu:language-change', { detail: { language: 'en' } }));
  expect(root.querySelector('.lead-deliverables__label')!.textContent).toBe('zh:leadDeliverables.title');
});

it("shows a report in its display translation, as the conversation does, keeping the original in the tooltip", async () => {
  const translated = item(1, { title: 'Shipped', summary: 'All merged.', translation: { title: '已交付', summary: '全部已合并。' } });
  const { view } = list(new Map([['', { conversationId: 'lead', items: [translated, item(2)], nextCursor: null }]]));
  view.show({ leadId: 'lead', path: '/work/tofu' });
  await flush();
  const [first, second] = [...document.querySelectorAll<HTMLElement>('.lead-deliverable')];
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

it('names each row\'s project only while the list spans more than one project', async () => {
  const { view } = list(new Map([['', { conversationId: 'lead', nextCursor: null, items: [
    item(1, { projectPath: '/work/tofu' }), item(2, { projectPath: '/work/parallel' })] }]]));
  view.show({ leadId: 'router', path: '' });
  await flush();
  const root = view.root;
  expect(root.dataset.sharedProject).toBe('false');
  expect(root.querySelector<HTMLElement>('.lead-deliverables__scope')!.hidden).toBe(true);
  expect([...root.querySelectorAll('.lead-deliverable__project')].map(node => node.textContent)).toEqual(['tofu', 'parallel']);
});
