// @vitest-environment jsdom
/**
 * Executable specification of the tool-card presentation layer, rendered
 * from real Coordinator transcript rounds (fixtures/coordinator-transcript.json,
 * captured 2026-11-15 and scrubbed of hosts/paths) plus schema-shaped rounds
 * for tools that transcript did not call (fixtures/schema-shaped.json).
 */
import { describe, expect, it, vi } from 'vitest';

import type { Translator } from '../../../i18n';
import en from '../../../i18n/locales/en.json';
import zh from '../../../i18n/locales/zh.json';

import contractsSource from './contracts.ts?raw';
import coordinationSource from './coordination.ts?raw';
import enrichmentSource from './enrichment.ts?raw';
import genericSource from './generic.ts?raw';
import indexSource from './index.ts?raw';
import markupSource from './markup.ts?raw';
import valuesSource from './values.ts?raw';
import transcriptFixture from './fixtures/coordinator-transcript.json';
import schemaShapedFixture from './fixtures/schema-shaped.json';

import { readableKey } from './generic';
import { createToolCardPresentation, handleToolCardClick } from './index';
import type { ToolPresentationPorts } from './contracts';

type Round = Record<string, unknown> & { toolName: string; toolArgs: string; toolContent: string; roundNum: number };

const transcript = transcriptFixture as unknown as Round[];
const schemaShaped = schemaShapedFixture as unknown as Round[];
const allRounds = [...transcript, ...schemaShaped];
const locales = { zh: zh as Record<string, string>, en: en as Record<string, string> };
/** Every renderer module of this owner; a new module joins the copy scan here. */
const RENDERER_SOURCES: Readonly<Record<string, string>> = {
  'contracts.ts': contractsSource, 'coordination.ts': coordinationSource, 'enrichment.ts': enrichmentSource,
  'generic.ts': genericSource,
  'index.ts': indexSource, 'markup.ts': markupSource, 'values.ts': valuesSource,
};

function localeTranslator(language: 'zh' | 'en'): Translator {
  return ((key: string, params?: Record<string, unknown>) => (locales[language][key] ?? key)
    .replace(/\{(\w+)\}/g, (match, name: string) => (params && params[name] !== undefined ? String(params[name]) : match))) as Translator;
}

/** Every translated string becomes a marker, so leftover text must be data. */
const markerTranslator = ((key: string, params?: Record<string, unknown>) =>
  `⟦${key}${params ? JSON.stringify(params) : ''}⟧`) as Translator;

const escapeText = (value: string): string => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const markdown = (source: string): string => `<p>${escapeText(source)}</p>`;

type ExtraPorts = Partial<Omit<ToolPresentationPorts, 'translate' | 'markdown' | 'conversationTitle'>>;

function presentation(language: 'zh' | 'en' | 'marker' = 'zh', titles: Record<string, string> = {}, extra: ExtraPorts = {}) {
  const cards = createToolCardPresentation({
    translate: language === 'marker' ? markerTranslator : localeTranslator(language),
    markdown,
    conversationTitle: (id) => titles[id],
    debugEnabled: () => true,
    ...extra,
  });
  // A page renders a transcript top-down: delegations name their assignments first.
  for (const round of transcript.filter((item) => item.toolName === 'delegate_project_work')) cards.headerText(round);
  return cards;
}

const chrome = { iconHtml: '<svg></svg>' };

function render(round: Round, language: 'zh' | 'en' | 'marker' = 'zh', titles: Record<string, string> = {}): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = presentation(language, titles).renderCardHtml(round, chrome);
  return host;
}

function first(name: string, rounds: readonly Round[] = allRounds): Round {
  const found = rounds.find((round) => round.toolName === name);
  if (!found) throw new Error(`fixture missing: ${name}`);
  return found;
}

function args(round: Round): Record<string, unknown> {
  return JSON.parse(round.toolArgs) as Record<string, unknown>;
}

/** Text a reader sees before opening technical details. */
function primaryText(host: HTMLElement): string {
  const copy = host.cloneNode(true) as HTMLElement;
  copy.querySelectorAll('.ptool-coord-debug').forEach((node) => node.remove());
  return copy.textContent ?? '';
}

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

describe('delegate_project_work', () => {
  const round = first('delegate_project_work');

  it('shows the task title, start status, acceptance checklist, collapsible brief and Worker link', () => {
    const host = render(round);
    const header = host.querySelector('summary .ptool-coord-header')!;
    expect(header.querySelector('.ptool-coord-title')!.textContent).toBe('分派任务');
    expect(header.querySelector('.ptool-coord-summary')!.textContent).toBe(args(round).title);
    expect(header.querySelector('.ptool-coord-chip')!.textContent).toBe('正在启动');
    const checks = host.querySelectorAll('.ptool-coord-check');
    expect(checks).toHaveLength((args(round).acceptance as unknown[]).length);
    expect(checks[0].textContent).toContain((args(round).acceptance as string[])[0]);
    const brief = host.querySelector<HTMLDetailsElement>('details.ptool-coord-more')!;
    expect(brief.open).toBe(false);
    expect(brief.querySelector('summary')!.textContent).toBe('完整任务说明');
    expect(brief.textContent).toContain('Owner request (2026-11-15)');
    const link = host.querySelector<HTMLElement>('[data-tool-open-conversation]')!;
    expect(link.dataset.toolOpenConversation).toBe(JSON.parse(round.toolContent).conversation_id);
    expect(link.textContent).toBe('打开工作者对话');
  });

  it('keeps identifiers and argument names out of the readable card', () => {
    const host = render(round);
    const readable = primaryText(host);
    expect(readable).not.toMatch(UUID);
    expect(readable).not.toContain('idempotency_key');
    expect(readable).not.toContain('brief=');
    expect(host.querySelector('.ptool-coord-debug')!.textContent).toContain(JSON.parse(round.toolContent).assignment_id);
  });

  it('opens the Worker conversation through the delegated click handler', () => {
    const host = render(round);
    document.body.append(host);
    const open = vi.fn();
    const button = host.querySelector<HTMLElement>('[data-tool-open-conversation]')!;
    const event = new MouseEvent('click', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'target', { value: button.querySelector('span') });
    handleToolCardClick(event, open);
    expect(open).toHaveBeenCalledWith(JSON.parse(round.toolContent).conversation_id);
    expect(event.defaultPrevented).toBe(true);
    host.remove();
  });
});

describe('update_task_progress', () => {
  const round = first('update_task_progress');
  const tasks = args(round).tasks as { title: string; status: string; progress?: string }[];

  it('renders the board as a todo list with icons, progress lines and the summary sentence on top', () => {
    const host = render(round);
    const rows = host.querySelectorAll('.ptool-todo-item');
    expect(rows).toHaveLength(tasks.length);
    expect(rows[0].querySelector('.ptool-coord-task-title')!.textContent).toBe(tasks[0].title);
    expect(rows[0].querySelector('.ptool-todo-mark svg')).not.toBeNull();
    const done = tasks.filter((task) => task.status === 'completed').length;
    const completedRow = [...rows].find((row) => row.classList.contains('ptool-todo-completed'))!;
    expect(completedRow.querySelector('.ptool-todo-mark')!.getAttribute('aria-label')).toBe('已完成');
    const withProgress = tasks.findIndex((task) => task.progress);
    if (withProgress >= 0) {
      expect(rows[withProgress].querySelector('.ptool-coord-progress')!.textContent).toBe(tasks[withProgress].progress);
    }
    const body = host.querySelector('.ptool-coord-body')!;
    expect(body.firstElementChild!.className).toBe('ptool-coord-lead');
    expect(body.firstElementChild!.textContent).toBe(args(round).summary);
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent).toBe(`已完成 ${done}/${tasks.length}`);
    expect(primaryText(host)).not.toContain('"plan"');
    expect(primaryText(host)).not.toContain('\\n- [ ]');
  });

  it('shows only the selected read result and never treats a missing result as an empty board', () => {
    const read = { ...round, toolArgs: JSON.stringify({ read: { title: 'A selected task' } }),
      toolContent: JSON.stringify({ revision: 5, tasks: [{ title: 'A selected task', status: 'pending' }] }) };
    const host = render(read, 'en');
    expect(host.querySelector('.ptool-coord-task-title')!.textContent).toBe('A selected task');
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent).toBe('A selected task');
    expect(render({ ...read, toolContent: '{}' }, 'en').querySelector('.ptool-todo-empty')).toBeNull();
  });

  it('does not repeat the whole board inside a change-only receipt', () => {
    const patch = { ...round, toolArgs: JSON.stringify({ changes: [{ title: 'A change', status: 'pending' }] }),
      toolContent: JSON.stringify({ revision: 5, changed: true, task_count: 200 }),
      todoState: { todos: [{ content: 'An unrelated task', status: 'pending' }] } };
    const host = render(patch, 'en');
    expect(primaryText(host)).not.toContain('An unrelated task');
    expect(host.querySelector('.ptool-todo-empty')).toBeNull();
  });

  it('shows a due date and a dropped task', () => {
    const board = { ...round, toolArgs: JSON.stringify({ summary: 'One due.', tasks: [
      { title: 'Ship it', status: 'in_progress', progress: 'Half done', due: '2026-11-20' },
      { title: 'Old idea', status: 'cancelled' },
    ] }) };
    const host = render(board, 'en');
    expect(host.querySelector('.ptool-coord-due')!.textContent).toBe('Due 2026-11-20');
    expect(host.querySelector('.ptool-coord-task-cancelled .ptool-todo-mark')!.getAttribute('aria-label')).toBe('Dropped');
    expect(host.querySelector('summary .ptool-coord-chip')!.textContent).toBe('1 in progress');
  });
});

describe('review_project_work', () => {
  const round = first('review_project_work');
  const reviewArgs = args(round);
  const content = JSON.parse(round.toolContent) as Record<string, unknown>;
  const delegation = transcript.find((item) => item.toolName === 'delegate_project_work'
    && JSON.parse(item.toolContent).assignment_id === reviewArgs.assignment_id)!;

  /** The same review with another decision and delivery state (the receipt's own fields). */
  function reviewed(decision: string, receipt: Record<string, unknown>, extraArgs: Record<string, unknown> = {}): Round {
    return { ...round,
      toolArgs: JSON.stringify({ ...reviewArgs, decision, ...extraArgs }),
      toolContent: JSON.stringify({ ...content, decision, ...receipt }) };
  }

  const verdict = (host: HTMLElement): string => host.querySelector('.ptool-coord-verdict')!.textContent ?? '';
  const headerChips = (host: HTMLElement): (string | null)[] =>
    [...host.querySelectorAll('summary .ptool-coord-chip')].map((chip) => chip.textContent);

  it('names the decision and task in a natural header, every check with evidence and a plain explanation', () => {
    const host = render(round);
    const header = host.querySelector('summary .ptool-coord-header')!;
    expect(header.querySelector('.ptool-coord-title')!.textContent).toBe('审核结果：采纳');
    expect(header.querySelector('.ptool-coord-summary')!.textContent).toBe(args(delegation).title);
    // Title and task never run together: the header reads "审核结果：采纳 · <task>".
    expect(header.textContent).toContain(`审核结果：采纳 · ${args(delegation).title as string}`);
    expect(headerChips(host)).toEqual(['5/5 项达标', '合并中']);
    const checks = host.querySelectorAll('.ptool-coord-check');
    expect(checks).toHaveLength(5);
    expect(checks[0].classList.contains('ptool-coord-check--met')).toBe(true);
    expect(checks[0].querySelector('.ptool-coord-mark')!.getAttribute('aria-label')).toBe('达标');
    expect(checks[0].textContent).toContain((args(delegation).acceptance as string[])[0]);
    expect(checks[0].querySelector('.ptool-coord-check-detail')!.textContent)
      .toBe((reviewArgs.acceptance_results as { evidence: string }[])[0].evidence);
    expect(verdict(host)).toBe('合并中');
    expect(host.querySelector('.ptool-coord-verdict + .ptool-coord-lead')!.textContent)
      .toBe('结果已通过审核，正在合并到主分支；合并完成后才算交付。');
    expect(primaryText(host)).not.toContain('canonical_delivery');
    expect(primaryText(host)).not.toContain(String(reviewArgs.checkpoint_sha));
    expect(host.querySelector('.ptool-coord-debug')!.textContent).toContain(String(reviewArgs.checkpoint_sha));
  });

  it.each([
    ['accept', { canonical_delivery: true, delivery: { status: 'delivered' } }, {},
      '审核结果：采纳', '已合并', ['已合并'], '结果已通过审核，并已合并到主分支。'],
    ['accept', { canonical_delivery: false, delivery: { status: 'pending', reason: '' } }, {},
      '审核结果：采纳', '合并中', ['合并中'], '结果已通过审核，正在合并到主分支；合并完成后才算交付。'],
    ['accept', { canonical_delivery: false, delivery: { status: 'held', reason: 'conflicts with main in lib/x.py' } }, {},
      '审核结果：采纳', '合并被挡住：conflicts with main in lib/x.py', ['合并被挡住'],
      '结果已通过审核，但合并没有完成；解决上面的原因后才会合并。'],
    ['accept', { canonical_delivery: false, delivery: null, checkpoint_sha: null }, { checkpoint_sha: '' },
      '审核结果：采纳', '已采纳 · 未指定版本，不合并代码', [], '结果已通过负责人审核，但没有指定要合并的版本，所以暂不合并代码。'],
    ['reject', { canonical_delivery: true, delivery: { status: 'delivered' } }, {},
      '审核结果：不采纳', '已合并（此前）· 本次审核记为不采纳', ['已合并（此前）'],
      '这个结果此前已经合并到主分支；本次审核只记录负责人的判断，不会撤销已合并的内容。'],
    ['rework', { canonical_delivery: true, delivery: { status: 'delivered' } }, {},
      '审核结果：返工', '已合并（此前）· 本次审核记为返工', ['已合并（此前）'],
      '这个结果此前已经合并到主分支；本次审核只记录负责人的判断，不会撤销已合并的内容。'],
    ['reject', { canonical_delivery: false, delivery: null }, {},
      '审核结果：不采纳', '不采纳 · 不会合并', [], '结果被拒绝，不会被合并。'],
    ['rework', { canonical_delivery: false, delivery: null }, {},
      '审核结果：返工', '返工 · 当前结果不会合并', [], '结果需要修改，工作者会按审核意见返工。'],
  ] as const)('%s with %j reads as its delivery state', (decision, receipt, extraArgs, title, line, mergeChips, explanation) => {
    const host = render(reviewed(decision, receipt, extraArgs));
    expect(host.querySelector('.ptool-coord-title')!.textContent).toBe(title);
    expect(verdict(host)).toBe(line);
    expect(headerChips(host)).toEqual(['5/5 项达标', ...mergeChips]);
    expect(host.querySelector('.ptool-coord-verdict + .ptool-coord-lead')!.textContent).toBe(explanation);
    // A result that is already merged is never described as one that will not be merged.
    if (receipt.canonical_delivery) expect(primaryText(host)).not.toContain('不会被合并');
  });

  it('reports unmet/unknown checks and rework in plain language', () => {
    const mixed = reviewed('rework', { acceptance_counts: { met: 1, unmet: 1, unknown: 1 } }, { checkpoint_sha: '', acceptance_results: [
      { check: 1, verdict: 'met', evidence: 'ok' }, { check: 2, verdict: 'unmet', evidence: 'missing test' },
      { check: 3, verdict: 'unknown', evidence: '' },
    ] });
    const host = render(mixed, 'en');
    expect([...host.querySelectorAll('.ptool-coord-check')].map((row) => row.className.split('--')[1]))
      .toEqual(['met', 'unmet', 'unknown']);
    expect(host.querySelector('.ptool-coord-title')!.textContent).toBe('Review: needs rework');
    expect(verdict(host)).toBe('Needs rework · this result will not be merged');
    expect(headerChips(host)).toEqual(['1/3 checks met']);
  });

  it('never presents a refused review as accepted, and names a changed result plainly', () => {
    const refused = { ...round, status: 'error', toolContent: JSON.stringify({
      error: 'Review requires the exact current completed worker result: result_revision 2814 is stale, the current result_revision is 2815',
      code: 'result_revision_changed', execution_started: false,
      details: { requested_result_revision: 2814, current_result_revision: 2815, checkpoint_sha: null } }) };
    const cards = presentation('en');
    const header = document.createElement('div');
    header.innerHTML = cards.headerHtml(refused);
    expect(header.querySelector('.ptool-coord-summary')!.textContent).toBe(args(delegation).title);
    expect(header.querySelectorAll('.ptool-coord-chip')).toHaveLength(0);
    expect(cards.failureText(refused)).toBe("The Worker's result changed while it was being reviewed, so this review was not recorded. "
      + 'The lead reads the current result and reviews it again.');
    const other = { ...refused, toolContent: JSON.stringify({ error: 'Worker review is immutable', execution_started: false }) };
    expect(cards.failureText(other)).toBe('');
    expect(cards.failureText(round)).toBe('');
  });
});

/* Rounds of a Coordinator whose delegations this page never rendered (older Turns, another conversation). */
describe('task titles of assignments this page never delegated', () => {
  const round = first('review_project_work');
  const content = JSON.parse(round.toolContent) as Record<string, unknown>;
  const assignment = 'eec5f732-878b-5f78-abef-e3fd62d8bc48';
  const worker = 'worker-0a1b2c3d4e5f60718293a4b5';
  const title = 'Back up repositories to the private remote';
  const unseen = (receipt: Record<string, unknown>): Round => ({ ...round,
    toolArgs: JSON.stringify({ assignment_id: assignment, result_revision: 490, decision: 'reject', note: 'Superseded.' }),
    toolContent: JSON.stringify({ ...content, assignment_id: assignment, decision: 'reject', grant_revision: 1,
      result_revision: 490, canonical_delivery: true, delivery: { status: 'delivered' }, acceptance_counts: undefined,
      ...receipt }) });

  it('looks up an older receipt\'s assignment once and repaints with its conversation title', async () => {
    const lookup = vi.fn(async () => worker);
    const cards = presentation('zh', { [worker]: title }, { assignmentConversation: lookup });
    const repaint = vi.fn();
    cards.subscribe(repaint);
    const host = document.createElement('div');
    host.innerHTML = cards.renderCardHtml(unseen({}), chrome);
    // While the lookup runs the header names the decision only, never a placeholder title.
    expect(host.querySelector('summary .ptool-coord-summary')).toBeNull();
    expect(host.textContent).not.toContain('未命名任务');
    await vi.waitFor(() => expect(repaint).toHaveBeenCalled());
    host.innerHTML = cards.renderCardHtml(unseen({}), chrome);
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent).toBe(title);
    expect(host.querySelector('.ptool-coord-field .ptool-coord-value')!.textContent).toBe(title);
    expect(host.textContent).not.toContain('未命名任务');
    expect(lookup).toHaveBeenCalledTimes(1);
    expect(lookup).toHaveBeenCalledWith(assignment);
  });

  it('titles an assignment whose Worker conversation the loaded catalog does not hold, never painting a placeholder', async () => {
    const assignmentConversation = vi.fn(async () => worker);
    const lookupConversationTitle = vi.fn(async () => title);
    // No catalog titles: the Worker conversation sits on a Worker page the sidebar never loaded.
    const cards = presentation('zh', {}, { assignmentConversation, lookupConversationTitle });
    const host = document.createElement('div');
    const paints: string[] = [];
    const paint = (): void => {
      host.innerHTML = cards.renderCardHtml(unseen({}), chrome);
      paints.push(host.textContent ?? '');
    };
    cards.subscribe(paint);
    paint();
    await vi.waitFor(() => expect(host.querySelector('summary .ptool-coord-summary')?.textContent).toBe(title));
    for (const painted of paints) expect(painted).not.toContain('未命名任务');
    expect(lookupConversationTitle).toHaveBeenCalledTimes(1);
    expect(lookupConversationTitle).toHaveBeenCalledWith(worker);
  });

  it('names a follow-up assignment by the Worker conversation it continues, without a lookup', () => {
    const lookup = vi.fn(async () => '');
    // A follow-up is a successor assignment in the same Worker conversation; its receipt names that conversation.
    const host = render(unseen({ conversation_id: worker }), 'zh', { [worker]: title });
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent).toBe(title);
    const cards = presentation('zh', { [worker]: title }, { assignmentConversation: lookup });
    cards.headerText(unseen({ conversation_id: worker }));
    expect(lookup).not.toHaveBeenCalled();
  });

  it('learns the conversation from Worker rows, so a later card of the same assignment is titled at once', () => {
    const lookup = vi.fn(async () => '');
    const cards = presentation('zh', { [worker]: title }, { assignmentConversation: lookup });
    cards.headerText({ ...first('get_project_workers'), toolContent: JSON.stringify({ workers: [
      { assignment_id: assignment, conversation_id: worker, status: 'completed' }] }) });
    const host = document.createElement('div');
    host.innerHTML = cards.renderCardHtml(unseen({}), chrome);
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent).toBe(title);
    expect(lookup).not.toHaveBeenCalled();
  });
});

describe('free text follows the conversation translation setting', () => {
  const round = first('review_project_work');
  const reviewArgs = args(round);
  const evidence = (reviewArgs.acceptance_results as { evidence: string }[])[0].evidence;

  it('shows translations with the original behind a toggle once they settle', async () => {
    const translateText = vi.fn(async (source: string) => `译：${source}`);
    const cards = presentation('zh', {}, { translationTarget: () => 'Chinese', translateText });
    const repaint = vi.fn();
    cards.subscribe(repaint);
    const host = document.createElement('div');
    host.innerHTML = cards.renderCardHtml(round, chrome);
    const note = host.querySelector('details.ptool-coord-more[open]')!;
    expect(note.querySelector('.ptool-coord-prose')!.textContent).toBe(reviewArgs.note);
    expect(note.querySelector('.ptool-coord-translating')!.textContent).toBe('正在翻译…');
    await vi.waitFor(() => expect(repaint).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.innerHTML = cards.renderCardHtml(round, chrome);
    const translated = host.querySelector('details.ptool-coord-more[open]')!;
    expect(translated.querySelector(':scope > .ptool-coord-prose')!.textContent).toBe(`译：${reviewArgs.note as string}`);
    const original = translated.querySelector<HTMLDetailsElement>('details.ptool-coord-original')!;
    expect(original.open).toBe(false);
    expect(original.querySelector('summary')!.textContent).toBe('原文');
    expect(original.querySelector('.ptool-coord-prose')!.textContent).toBe(reviewArgs.note);
    const detail = host.querySelector('.ptool-coord-check-detail')!;
    expect(detail.textContent).toContain(`译：${evidence}`);
    expect(detail.querySelector('details.ptool-coord-original')!.textContent).toContain(evidence);
    expect(translateText).toHaveBeenCalledWith(reviewArgs.note, 'Chinese');
    expect(host.querySelector('.ptool-coord-translating')).toBeNull();
  });

  it('shows the authored text alone when translation is off or the text needs none', async () => {
    const translateText = vi.fn(async (source: string) => source);
    const off = document.createElement('div');
    off.innerHTML = presentation('zh', {}, { translationTarget: () => '', translateText }).renderCardHtml(round, chrome);
    expect(translateText).not.toHaveBeenCalled();
    expect(off.querySelector('.ptool-coord-original, .ptool-coord-translating')).toBeNull();
    const cards = presentation('zh', {}, { translationTarget: () => 'English', translateText });
    cards.renderCardHtml(round, chrome);
    await vi.waitFor(() => expect(translateText).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    const same = document.createElement('div');
    same.innerHTML = cards.renderCardHtml(round, chrome);
    expect(same.querySelector('.ptool-coord-original, .ptool-coord-translating')).toBeNull();
  });
});

describe('technical details', () => {
  it('appear only in debug mode', () => {
    const round = first('review_project_work');
    let debug = false;
    const cards = presentation('zh', {}, { debugEnabled: () => debug });
    const host = document.createElement('div');
    host.innerHTML = cards.renderCardHtml(round, chrome);
    expect(host.querySelector('.ptool-coord-debug')).toBeNull();
    expect(host.textContent).not.toMatch(UUID);
    expect(host.textContent).not.toContain('结果版本');
    debug = true;
    host.innerHTML = cards.renderCardHtml(round, chrome);
    const details = host.querySelector<HTMLDetailsElement>('.ptool-coord-debug')!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain(String(args(round).assignment_id));
    expect(details.textContent).toContain('结果版本');
  });

  function card(cards: ReturnType<typeof presentation>, round: Round): HTMLElement {
    const host = document.createElement('div');
    host.innerHTML = cards.renderCardHtml(round, chrome)
      || cards.renderGenericCardHtml(round, { ...chrome, titleHtml: round.toolName });
    expect(host.querySelector('[data-tool-card]')).not.toBeNull();
    return host;
  }

  it.each(allRounds.map((round) => [round.toolName, round] as const))(
    '%s shows no technical details when debug mode is off', (_name, round) => {
      const host = card(presentation('zh', {}, { debugEnabled: () => false }), round);
      expect(host.querySelector('.ptool-coord-debug, .ptool-coord-raw')).toBeNull();
      expect(host.textContent).not.toContain('技术详情');
      expect(host.textContent).not.toContain('原始结果');
    });

  it('the poll card shows them only in debug mode', () => {
    let debug = false;
    const cards = presentation('zh', {}, { debugEnabled: () => debug });
    expect(card(cards, first('poll')).textContent).not.toContain('技术详情');
    debug = true;
    expect(card(cards, first('poll')).querySelector('.ptool-coord-debug > summary')!.textContent).toBe('技术详情');
  });
});

describe('card chrome', () => {
  it('parts the title from its subject with a separator the reader does not hear', () => {
    const header = render(first('poll')).querySelector('summary .ptool-coord-header')!;
    expect([...header.children].map((node) => node.className))
      .toEqual(['ptool-coord-title', 'ptool-coord-sep', 'ptool-coord-summary']);
    expect(header.querySelector('.ptool-coord-sep')!.getAttribute('aria-hidden')).toBe('true');
  });

  it('opens every fold inside every card from the same chevron toggle', () => {
    let folds = 0;
    for (const round of allRounds) {
      const cards = presentation('zh');
      const host = document.createElement('div');
      host.innerHTML = cards.renderCardHtml(round, chrome)
        || cards.renderGenericCardHtml(round, { ...chrome, titleHtml: round.toolName });
      for (const fold of host.querySelectorAll('[data-tool-card] details')) {
        const summary = fold.querySelector(':scope > summary')!;
        expect(summary.classList.contains('ptool-coord-toggle')).toBe(true);
        expect(summary.firstElementChild!.tagName.toLowerCase()).toBe('svg');
        expect(summary.firstElementChild!.getAttribute('aria-hidden')).toBe('true');
        expect(summary.textContent!.trim()).not.toBe('');
        folds += 1;
      }
    }
    expect(folds).toBeGreaterThan(allRounds.length);
  });
});

describe('poll', () => {
  const round = first('poll');
  const workers = JSON.parse(round.toolContent).workers as { status: string; assignment_id: string }[];

  it('lists finished and running work by task title', () => {
    const host = render(round);
    const running = workers.filter((worker) => worker.status === 'running').length;
    expect(host.querySelector('.ptool-coord-title')!.textContent).toBe('等待工作者结果');
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent)
      .toBe(`${workers.length - running} 个已完成 · ${running} 个进行中`);
    const headings = [...host.querySelectorAll('.ptool-coord-heading')].map((node) => node.textContent);
    expect(headings).toEqual([`已完成（${workers.length - running}）`, `进行中（${running}）`]);
    const titles = [...host.querySelectorAll('.ptool-coord-worker-title')].map((node) => node.textContent);
    const delegated = new Map(transcript.filter((item) => item.toolName === 'delegate_project_work')
      .map((item) => [JSON.parse(item.toolContent).assignment_id as string, args(item).title as string]));
    for (const worker of workers) expect(titles).toContain(delegated.get(worker.assignment_id));
    expect(primaryText(host)).not.toMatch(UUID);
  });

  it('leaves non-Coordinator polls to their own renderer', () => {
    const cards = presentation();
    const background = { ...round, toolArgs: JSON.stringify({ ids: ['bg_123'] }), toolContent: '{"commands": []}' };
    expect(cards.renderCardHtml(background, chrome)).toBe('');
    expect(cards.headerHtml(background)).toBe('');
  });
});

describe('get_project_assignment', () => {
  it('summarizes the assignment by its title and flags a shortened record', () => {
    const round = first('get_project_assignment');
    const host = render(round);
    const delegation = transcript.find((item) => item.toolName === 'delegate_project_work'
      && JSON.parse(item.toolContent).assignment_id === args(round).assignment_id)!;
    expect(host.querySelector('.ptool-coord-title')!.textContent).toBe('查看任务详情');
    expect(host.querySelector('summary .ptool-coord-summary')!.textContent).toBe(args(delegation).title);
    expect(host.textContent).toContain('完整记录较长，这里只显示开头部分。');
    expect(host.querySelectorAll('.ptool-coord-check').length).toBeGreaterThan(0);
  });
});

describe('reports, finish, cancel, control and project messages', () => {
  it.each([
    ['report_to_user', '向你汇报', 'Extension URL fix merged'],
    ['finish_project_request', '结束本次请求', '工作已全部完成'],
    ['cancel_project_work', '取消任务', 'SQLite sidecar'],
    ['control_conversation', '控制对话', '追加指令 · SQLite'],
    ['send_project_message', '发送项目消息', '发给 Browser extension'],
    ['get_project_messages', '读取项目消息', '收到的消息 · 共 1 条'],
    ['get_project_workers', '查看项目工作者', '2 个工作者'],
  ])('%s has a localized title and a meaningful summary', (name, title, summary) => {
    const host = render(first(name), 'zh', {
      'worker-c7dcc90702009950156f9570': 'SQLite sidecar: fair, fully-optimized configuration on NetFS',
      'worker-1590ca7432646d63dbed51b6': 'Browser extension: robust server URL input',
    });
    expect(host.querySelector('.ptool-coord-title')!.textContent).toBe(title);
    expect(host.querySelector('summary .ptool-coord-header')!.textContent).toContain(summary);
  });

  it('says a cancelled assignment whose result was already merged keeps that merge', () => {
    const round = first('cancel_project_work');
    const merged = { ...round, toolContent: JSON.stringify({ ...JSON.parse(round.toolContent), canonical_delivery: true,
      delivery: { status: 'delivered' }, execution_status: 'completed' }) };
    const host = render(merged);
    expect(host.querySelector('.ptool-coord-lead')!.textContent)
      .toBe('这项工作此前已合并。取消只会停止后续工作并撤回授权，不会撤回已合并的改动。');
    expect([...host.querySelectorAll('summary .ptool-coord-chip')].map((chip) => chip.textContent))
      .toEqual(['已取消', '已合并（此前）']);
    expect(render(round).querySelector('summary .ptool-coord-header')!.textContent).not.toContain('已合并');
  });

  it('titles a follow-up instruction to a Worker conversation the catalog does not hold', async () => {
    const round = first('control_conversation');
    const lookupConversationTitle = vi.fn(async () => 'SQLite sidecar: fair configuration');
    const cards = presentation('zh', {}, { lookupConversationTitle });
    expect(cards.headerText(round)).not.toContain('未命名对话');
    await vi.waitFor(() => expect(cards.headerText(round)).toContain('追加指令 · SQLite sidecar: fair configuration'));
  });

  it('keeps identifiers and revisions of plain result fields in technical details', () => {
    const round = { ...first('cancel_project_work'), toolName: 'dispatch_project_assignment' };
    let debug = false;
    const cards = presentation('zh', {}, { debugEnabled: () => debug });
    const host = document.createElement('div');
    host.innerHTML = cards.renderCardHtml(round, chrome);
    const content = JSON.parse(round.toolContent) as Record<string, unknown>;
    expect(host.textContent).not.toContain(String(content.assignment_id));
    expect(host.textContent).not.toContain(String(content.conversation_id));
    expect(host.textContent).not.toMatch(/Grant revision|Result revision/);
    expect(host.textContent).toContain('Execution status');
    debug = true;
    host.innerHTML = cards.renderCardHtml(round, chrome);
    expect(host.querySelector('.ptool-coord-debug')!.textContent).toContain(String(content.assignment_id));
    expect(host.querySelector('.ptool-coord-debug')!.textContent).toContain('Grant revision');
  });

  it('renders the report body as Markdown and the cancellation in plain language', () => {
    expect(render(first('report_to_user')).querySelector('.ptool-coord-prose')!.textContent).toContain('Nine other tasks');
    expect(render(first('cancel_project_work')).querySelector('.ptool-coord-lead')!.textContent)
      .toBe('工作者已停止并撤回授权，其结果和记录均已保留。');
    expect(render(first('consider_project_message')).querySelector('summary .ptool-coord-chip')!.textContent).toBe('已采纳');
  });
});

describe('every registered tool', () => {
  const registered = allRounds.filter((round) => round.toolName !== 'future_inventory_tool');

  it.each(registered.map((round) => [round.toolName, round] as const))(
    '%s renders a card whose collapsed line says more than the tool name', (_name, round) => {
      for (const language of ['zh', 'en'] as const) {
        const host = render(round, language);
        const summary = host.querySelector('summary .ptool-coord-header');
        expect(summary).not.toBeNull();
        const extra = summary!.querySelector('.ptool-coord-summary, .ptool-coord-chip');
        expect(extra?.textContent?.trim()).toBeTruthy();
        for (const key of Object.keys(args(round))) expect(summary!.textContent).not.toContain(`${key}=`);
        expect(host.querySelector('.ptool-coord-body > pre')).toBeNull();
      }
    });

  it('defers the body until opened and keeps the lazy-result contract', () => {
    const cards = presentation();
    const round = first('review_project_work');
    expect(cards.renderCardHtml({ ...round, _deferToolResultBody: true }, chrome)).toContain('data-deferred-tool-body=""');
    const lazy = { ...round, toolContent: undefined, _snapshotToolResult: { preview: '{"decision": "acc', chars: 712 } };
    const html = cards.renderCardHtml(lazy, chrome);
    expect(html).toContain('data-lazy-tool-result=""');
    expect(html).toContain('审核结果：采纳');
  });

  it('leaves running and failed rounds to the shared row states while localizing their header', () => {
    const cards = presentation();
    const running = { ...first('delegate_project_work'), status: 'searching', toolContent: '' };
    expect(cards.renderCardHtml(running, chrome)).toBe('');
    expect(cards.headerHtml(running)).toContain('分派任务');
  });
});

describe('generic fallback', () => {
  it('renders an unknown tool result as labeled fields with raw JSON secondary', () => {
    const round = first('future_inventory_tool');
    const cards = presentation('en');
    expect(cards.renderCardHtml(round, chrome)).toBe('');
    const host = document.createElement('div');
    host.innerHTML = cards.renderGenericCardHtml(round, { ...chrome, titleHtml: 'future_inventory_tool' });
    const fields = [...host.querySelectorAll('.ptool-coord-field')].map((field) =>
      [field.querySelector('.ptool-coord-label')!.textContent, field.querySelector('.ptool-coord-value')!.textContent]);
    expect(fields).toContainEqual(['Item count', '3']);
    expect(fields).toContainEqual(['Restocked', 'Yes']);
    expect(host.querySelector('.ptool-coord-field details.ptool-coord-more summary')!.textContent).toBe('Show text (2 lines)');
    expect(host.querySelector('.ptool-coord-body > .ptool-coord-debug .ptool-coord-raw pre')!.textContent).toContain('"item_count"');
    expect(primaryText(host)).not.toContain('{');
  });

  it('does not take over text results or partial results', () => {
    const cards = presentation();
    const text = { ...first('future_inventory_tool'), toolContent: 'plain output' };
    expect(cards.renderGenericCardHtml(text, { ...chrome, titleHtml: 'x' })).toBe('');
    expect(cards.renderGenericCardHtml(first('get_project_assignment'), { ...chrome, titleHtml: 'x' })).toBe('');
  });
});

/* ── i18n: no authored copy outside the translator ───────────────────── */

function dataCorpus(): string {
  const parts: string[] = [];
  const visit = (value: unknown): void => {
    if (typeof value === 'string') {
      parts.push(value);
      try { visit(JSON.parse(value)); } catch { /* plain text */ }
    } else if (typeof value === 'number') parts.push(String(value));
    else if (Array.isArray(value)) value.forEach(visit);
    else if (value && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) { parts.push(readableKey(key)); visit(item); }
    }
  };
  visit(allRounds);
  return parts.join('\n');
}

/** Visible text left after removing translator markers that is not round data. */
function leaksIn(html: string, corpus: string): string[] {
  const host = document.createElement('div');
  host.innerHTML = html;
  const texts: string[] = [];
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) texts.push(walker.currentNode.textContent ?? '');
  host.querySelectorAll('[aria-label], [title]').forEach((node) => {
    texts.push(node.getAttribute('aria-label') ?? '', node.getAttribute('title') ?? '');
  });
  const leaks: string[] = [];
  for (const value of texts) {
    let rest = value;
    while (/⟦[^⟦⟧]*⟧/.test(rest)) rest = rest.replace(/⟦[^⟦⟧]*⟧/g, ' ');
    rest = rest.trim();
    if (!rest || /^[\s·→/()\-:,.0-9]*$/.test(rest)) continue;
    for (const piece of rest.split(/\s*(?:·|\n)\s*/).filter(Boolean)) {
      if (!corpus.includes(piece)) leaks.push(piece);
    }
  }
  return leaks;
}

describe('i18n of tool cards', () => {
  const corpus = dataCorpus();

  it('renders no visible word that is neither translated copy nor round data', () => {
    const cards = presentation('marker');
    const leaks = allRounds.flatMap((round) => leaksIn(cards.renderCardHtml(round, chrome)
      || cards.renderGenericCardHtml(round, { ...chrome, titleHtml: '' }), corpus)
      .map((piece) => `${round.toolName}: ${piece}`));
    expect(leaks).toEqual([]);
  });

  it('fails on a hard-coded string in a card, visible or in an accessible name', () => {
    expect(leaksIn('<span>⟦toolCard.delegate.title⟧</span><button>Open worker</button>', corpus)).toEqual(['Open worker']);
    expect(leaksIn('<span aria-label="已完成">x</span>', corpus)).toContain('已完成');
  });

  it('authors no CJK copy and calls the translator only with keys present in zh and en', () => {
    for (const [name, source] of Object.entries(RENDERER_SOURCES)) {
      expect(source, name).not.toMatch(/[\u3400-\u9fff]/);
      for (const [, key] of source.matchAll(/'(toolCard\.[A-Za-z.]+)'/g)) {
        expect(locales.zh[key], `${name}: zh ${key}`).toBeTruthy();
        expect(locales.en[key], `${name}: en ${key}`).toBeTruthy();
      }
    }
  });
});
