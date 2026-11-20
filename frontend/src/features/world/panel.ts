/** The person panel beside the office (owner request 2026-11-18: the office shows progress and files, the
 * classic view the whole conversation). One header (portrait, name, a status chip from the office facts, the
 * "full conversation" switch to the classic view, close) over the brief, one calm column in this order:
 * - the decisions waiting on the owner: a lead's open questions as one compact question card (the whole
 *   question, the shared answer control with its choices, own-answer box and default, '1/N' paging,
 *   "answer in conversation") and overdue commitments;
 * - now: a lead's board (summary, done/total, its open tasks with their progress notes, folded); a Worker's
 *   assignment steps, its task, the first paragraph of its result (the rest on request) and the
 *   acceptance checks with the lead's verdicts;
 * - progress: the timeline of reports and completed tasks, newest first, by day; each opens itself;
 * - files: every file the person delivered, each opening itself, with a download link;
 * - the way to the full conversation in the classic view, the only place tool calls are shown.
 * Entry point: createPanel. Dependencies: i18n, ui factories, icons, the item model, safe text projection,
 * sprites. */
import { t, type I18nKey } from '../../i18n';
import { iconHtml } from '../../icons';
import { createButton, createDisclosureSummary, createIconButton } from '../../ui';
import type { ChoiceInteraction, ChoicePorts } from '../../rich-response/choices';
import { mountGuidanceAnswerFor, type GuidanceAnswerMount } from '../../rich-response/guidance-answer';
import { deliverableDayLabel, type DeliverableVisual, type LeadDeliverable } from '../lead-deliverables/model';
import type { Brief, BriefCheck, BriefDecision, BriefEntry, BriefFile, TurnRef, Verdict, WorkerStep } from './brief';
import { renderText } from './text';
import { worldTheme } from './skin';
import { portraitStyle } from './sprites';

export interface PanelPorts {
  close(): void;
  /** The person's whole conversation in the classic view, optionally at one Turn. */
  conversation(turn: TurnRef | null): void;
  /** Answer ports bound to this question's conversation and Turn; undefined where it cannot be answered. */
  answerPorts(decision: BriefDecision): ChoicePorts | undefined;
  /** Opens the lead's conversation in the classic view at this question with the composer replying to it. */
  answerInChat(decision: BriefDecision): void;
  /** The owner answered this question from the card. */
  answered(decision: BriefDecision): void;
  /** Show a deliverable itself: a report or work report as its card, a file in the file viewer. */
  open(item: LeadDeliverable): void;
  /** The download address of a published file. */
  downloadUrl(artifactId: string): string;
  /** Read the next older page of the timeline; retry a failed read. */
  more(): void;
  retry(): void;
  /** Settles a non-blocking question on its stated default without sending the lead anything; rejects on failure. */
  confirmDefault(decision: BriefDecision): Promise<void>;
}
export type StatusTone = 'decision' | 'exception' | 'working' | 'waiting' | 'idle';
export interface PanelIdentity { team: string; color: string; state: string; tone: StatusTone }

const VERDICT_ICON: Record<Verdict, string> = { met: '✓', unmet: '✕', unknown: '?', unreviewed: '○' };
const VERDICT_KEYS = {
  met: 'world.brief.check.met', unmet: 'world.brief.check.unmet', unknown: 'world.brief.check.unknown', unreviewed: 'world.brief.check.unreviewed',
} as const satisfies Record<Verdict, I18nKey>;
const STEP_KEYS = {
  assigned: 'world.brief.step.assigned', working: 'world.brief.step.working', reported: 'world.brief.step.reported',
  reviewed: 'world.brief.step.reviewed', merged: 'world.brief.step.merged',
} as const satisfies Record<WorkerStep, I18nKey>;
const REVIEW_KEYS = {
  accept: 'world.brief.review.accept', rework: 'world.brief.review.rework', reject: 'world.brief.review.reject',
} as const satisfies Record<'accept' | 'rework' | 'reject', I18nKey>;
const GLYPHS: Readonly<Record<DeliverableVisual, string>> = {
  delivery: 'checkCircle', progress: 'chart', decision: 'messageCircle', blocked: 'alertTriangle', report: 'inbox',
  deck: 'slides', pdf: 'file', sheet: 'fileSheet', image: 'image', document: 'file', file: 'fileCode', merge: 'gitMerge',
  completion: 'checkCircle',
};
/** The tone a timeline entry's dot and label take. */
const TONE: Readonly<Record<DeliverableVisual, 'good' | 'info' | 'bad' | 'warn'>> = {
  delivery: 'good', completion: 'good', merge: 'good', progress: 'info', report: 'info', decision: 'warn', blocked: 'bad',
  deck: 'info', pdf: 'info', sheet: 'info', image: 'info', document: 'info', file: 'info',
};

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, parent?: HTMLElement): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  parent?.append(node);
  return node;
}

export function ago(at: number, now: number): string {
  const minutes = Math.max(0, Math.floor((now - at) / 60_000));
  if (minutes < 1) return t('world.ago.now');
  if (minutes < 60) return t('world.ago.minutes', { count: minutes });
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? t('world.ago.hours', { count: hours }) : t('world.ago.days', { count: Math.floor(hours / 24) });
}

function glyph(name: string): HTMLElement {
  const holder = document.createElement('span');
  holder.className = 'w-glyph';
  holder.setAttribute('aria-hidden', 'true');
  holder.innerHTML = iconHtml(name);
  return holder;
}

const locale = () => document.documentElement.lang || undefined;
const clock = (at: number) => new Intl.DateTimeFormat(locale(), { hour: '2-digit', minute: '2-digit' }).format(at);

/** A report's opening paragraph as plain text: the timeline shows its gist, the card opens the rest. */
function gist(markdown: string): string {
  const opening = markdown.trim().split(/\n\s*\n/)[0] || '';
  return opening.replace(/^#+\s*/gm, '').replace(/^\s*[-*]\s+/gm, '').replace(/(\*\*|__|`)/g, '').replace(/\s+/g, ' ').trim();
}

function size(bytes: number): string {
  if (!bytes) return '';
  const format = new Intl.NumberFormat(locale(), { maximumFractionDigits: 1 });
  return bytes < 1024 * 1024 ? `${format.format(Math.max(1, Math.round(bytes / 1024)))} KB` : `${format.format(bytes / (1024 * 1024))} MB`;
}

function kindLabel(item: LeadDeliverable): string {
  if (item.kind === 'report' && item.reportKind) return t(`leadDeliverables.reportKind.${item.reportKind}` as const);
  return t(`leadDeliverables.kind.${item.kind}` as const);
}

export function createPanel(ports: PanelPorts) {
  const head = el('header', 'w-panel-head');
  head.hidden = true;
  head.setAttribute('role', 'region');
  const portrait = el('span', 'w-portrait', head);
  portrait.setAttribute('aria-hidden', 'true');
  const titles = el('div', 'w-panel-titles', head);
  const name = el('h2', '', titles);
  name.id = 'w-panel-name';
  head.setAttribute('aria-labelledby', name.id);
  const line = el('p', 'w-panel-line', titles);
  const status = el('span', 'w-status', line);
  const team = el('span', 'w-panel-team', line);
  const actions = el('div', 'w-panel-actions', head);
  // A narrow column keeps only the glyph; the accessible name stays the whole action.
  const full = createButton({ label: '', size: 'sm', variant: 'secondary', title: t('world.panel.fullHint'),
    onClick: () => ports.conversation(null) });
  full.classList.add('w-panel-full');
  full.innerHTML = iconHtml('messageSquare');
  el('span', 'w-panel-full-label', full).textContent = t('world.panel.full');
  full.setAttribute('aria-label', t('world.panel.fullHint'));
  const close = createIconButton({ icon: glyph('x'), label: t('world.panel.close'), size: 'sm', onClick: () => ports.close() });
  close.classList.add('w-panel-close');
  actions.append(full, close);
  const body = el('section', 'w-brief');
  body.hidden = true;
  body.tabIndex = -1;
  body.setAttribute('aria-labelledby', name.id);
  let personId = '';
  // The question card's state for the open person: the page shown, the mounted answer controls (kept so a
  // draft survives live repaints) and the questions answered here (still shown, marked answered, after the
  // office stops listing them). The result's disclosure survives repaints too.
  let questionId = '';
  const answers = new Map<string, GuidanceAnswerMount>();
  const answeredHere = new Map<string, BriefDecision>();
  // Questions settled here on their default (shown confirmed), and confirmations still in flight or failed.
  const confirmedHere = new Set<string>();
  const confirming = new Map<string, 'pending' | 'failed'>();
  let lastRender: { brief: Brief; identity: PanelIdentity; now: number } | null = null;
  let resultOpen = false;
  let tasksOpen = false;
  function resetPerson() {
    resultOpen = false;
    tasksOpen = false;
    for (const mount of answers.values()) mount.dispose();
    answers.clear();
    answeredHere.clear();
    confirmedHere.clear();
    confirming.clear();
    questionId = '';
  }
  const markdownHtml = (source: string) => {
    const holder = document.createElement('div');
    renderText(source, holder);
    return holder.innerHTML;
  };

  function heading(parent: HTMLElement, text: string, tone = '', count?: number): HTMLElement {
    const node = el('h3', `w-brief-head${tone ? ` w-tone-${tone}` : ''}`, parent);
    node.textContent = text;
    if (count !== undefined) el('span', 'w-brief-count', node).textContent = String(count);
    return node;
  }
  function section(parent: HTMLElement, name: string): HTMLElement {
    const node = el('section', 'w-brief-section', parent);
    node.dataset.section = name;
    return node;
  }
  function rerender() {
    if (lastRender && personId === lastRender.brief.person.id) render(lastRender.brief, lastRender.identity, lastRender.now);
  }
  function markAnswered(decision: BriefDecision) {
    if (answeredHere.has(decision.guidanceId)) return;
    answeredHere.set(decision.guidanceId, decision);
    ports.answered(decision);
    rerender();
  }
  function confirmDefault(decision: BriefDecision) {
    if (confirming.get(decision.guidanceId) === 'pending') return;
    confirming.set(decision.guidanceId, 'pending');
    rerender();
    const owner = personId;
    void ports.confirmDefault(decision).then(() => {
      if (personId !== owner) return;
      confirming.delete(decision.guidanceId);
      confirmedHere.add(decision.guidanceId);
      // Remount the answer control so it reads the confirmed card from its Turn.
      answers.get(decision.guidanceId)?.dispose();
      answers.delete(decision.guidanceId);
      markAnswered(decision);
    }, () => {
      if (personId !== owner) return;
      confirming.set(decision.guidanceId, 'failed');
      rerender();
    });
  }
  function answerFor(decision: BriefDecision): GuidanceAnswerMount {
    const known = answers.get(decision.guidanceId);
    if (known) return known;
    const bound = ports.answerPorts(decision);
    const observe = (value: ChoiceInteraction) => {
      if (value.state === 'answered') queueMicrotask(() => markAnswered(decision));
      return value;
    };
    const observed: ChoicePorts | undefined = bound && {
      ...bound,
      load: async (id, signal) => observe(await bound.load(id, signal)),
      submit: async (id, response, command, signal) => observe(await bound.submit(id, response, command, signal)),
    };
    const mount = mountGuidanceAnswerFor(document, decision.guidanceId, observed, markdownHtml, { submitOnChoose: true });
    answers.set(decision.guidanceId, mount);
    return mount;
  }
  function questionCard(parent: HTMLElement, decisions: BriefDecision[]) {
    let index = decisions.findIndex(item => item.guidanceId === questionId);
    if (index < 0) index = 0;
    const decision = decisions[index];
    questionId = decision.guidanceId;
    const answered = answeredHere.has(decision.guidanceId);
    const confirmed = confirmedHere.has(decision.guidanceId);
    const card = el('section', 'w-question-card', parent);
    card.dataset.guidanceId = decision.guidanceId;
    card.dataset.state = confirmed ? 'confirmed' : answered ? 'answered' : 'open';
    const top = el('header', 'w-question-top', card);
    heading(top, t('world.brief.decisions'), 'decision');
    if (answered) el('span', 'w-question-badge', top).textContent = t(confirmed ? 'world.brief.confirmed' : 'world.brief.answered');
    if (decisions.length > 1) {
      const pager = el('div', 'w-question-pager', top);
      const step = (delta: number) => {
        questionId = decisions[(index + delta + decisions.length) % decisions.length].guidanceId;
        rerender();
        body.querySelector<HTMLElement>(`.w-question-pager [data-step="${delta}"]`)?.focus({ preventScroll: true });
      };
      const previous = createButton({ label: '‹', size: 'sm', variant: 'ghost', onClick: () => step(-1) });
      previous.dataset.step = '-1';
      previous.setAttribute('aria-label', t('world.brief.question.previous'));
      const count = el('span', 'w-question-count', pager);
      count.textContent = `${index + 1}/${decisions.length}`;
      count.setAttribute('aria-live', 'polite');
      const next = createButton({ label: '›', size: 'sm', variant: 'ghost', onClick: () => step(1) });
      next.dataset.step = '1';
      next.setAttribute('aria-label', t('world.brief.question.next'));
      pager.prepend(previous);
      pager.append(next);
    }
    const meta = el('p', 'w-meta w-question-meta', card);
    meta.textContent = [decision.project ? t('world.brief.project', { name: decision.project }) : '',
      t(decision.blocking ? 'world.brief.blocking' : 'world.brief.nonBlocking')].filter(Boolean).join(' · ');
    card.append(answerFor(decision).node);
    const actions = el('div', 'w-question-actions', card);
    if (!decision.blocking && !answered) {
      const pending = confirming.get(decision.guidanceId) === 'pending';
      const confirm = createButton({ label: t('world.brief.confirmDefault'), size: 'sm', variant: 'primary',
        disabled: pending, onClick: () => confirmDefault(decision) });
      confirm.classList.add('w-question-confirm');
      confirm.title = t('world.brief.confirmDefaultTitle');
      actions.append(confirm);
      if (confirming.get(decision.guidanceId) === 'failed') {
        const error = el('p', 'w-question-error', card);
        error.setAttribute('role', 'alert');
        error.textContent = t('world.brief.confirmFailed');
      }
    }
    const inChat = el('button', 'w-link w-link-primary', actions);
    inChat.type = 'button';
    inChat.textContent = t('world.brief.answer');
    inChat.dataset.turn = decision.turn.turnId;
    inChat.addEventListener('click', () => ports.answerInChat(decision));
  }

  function check(list: HTMLElement, item: BriefCheck) {
    const row = el('li', 'w-check', list);
    row.dataset.verdict = item.verdict;
    const icon = el('span', 'w-check-icon', row);
    icon.textContent = VERDICT_ICON[item.verdict];
    icon.title = t(VERDICT_KEYS[item.verdict]);
    icon.setAttribute('role', 'img');
    icon.setAttribute('aria-label', t(VERDICT_KEYS[item.verdict]));
    const text = el('div', 'w-check-text', row);
    renderText(item.text, el('div', '', text));
    if (item.evidence) renderText(item.evidence, el('div', 'w-check-evidence', text));
  }

  function leadNow(parent: HTMLElement, brief: Brief, now: number) {
    const board = brief.board;
    if (!board) return;
    const block = section(parent, 'now');
    const top = heading(block, t('world.brief.now'));
    if (board.updatedAt) el('span', 'w-brief-when', top).textContent = t('world.brief.updated', { when: ago(board.updatedAt, now) });
    const card = el('div', 'w-card w-now', block);
    if (board.summary) el('p', 'w-now-summary', card).textContent = board.summary;
    const meter = el('div', 'w-meter', card);
    const bar = el('div', 'w-progress', meter);
    bar.style.setProperty('--done', String(board.total ? board.done / board.total : 0));
    bar.setAttribute('role', 'img');
    const label = t('world.team.board', { done: board.done, total: board.total });
    bar.setAttribute('aria-label', label);
    el('span', 'w-meta', meter).textContent = label;
    if (!board.tasks.length) return;
    // The open tasks fold under the summary, so the timeline stays in view however long the board is.
    const fold = el('details', 'w-tasks-fold', card);
    const summary = createDisclosureSummary(document, t('world.brief.openTasks', { count: board.tasks.length }));
    summary.classList.add('w-tasks-summary');
    fold.append(summary);
    fold.open = tasksOpen;
    fold.addEventListener('toggle', () => { tasksOpen = fold.open; });
    const list = el('ul', 'w-tasks', fold);
    for (const task of board.tasks) {
      const row = el('li', 'w-task', list);
      row.dataset.status = task.status;
      el('span', 'w-task-dot', row).setAttribute('aria-hidden', 'true');
      const text = el('div', 'w-task-text', row);
      el('b', '', text).textContent = task.title;
      if (task.progress) {
        const note = el('span', '', text);
        note.textContent = task.progress;
        note.title = task.progress;
      }
    }
  }

  function workerNow(parent: HTMLElement, brief: Brief) {
    if (brief.steps.length) {
      const block = section(parent, 'now');
      heading(block, t('world.brief.now'));
      const card = el('div', 'w-card w-now', block);
      const steps = el('ol', 'w-steps', card);
      steps.setAttribute('aria-label', t('world.brief.stepsLabel'));
      for (const item of brief.steps) {
        const step = el('li', 'w-step', steps);
        step.dataset.done = String(item.done);
        if (item.failed) step.dataset.failed = 'true';
        step.textContent = t(STEP_KEYS[item.step]);
        if (item.step === 'reviewed' && brief.review) step.title = t(REVIEW_KEYS[brief.review.decision]);
      }
      const facts: string[] = [];
      if (brief.review) facts.push(t(REVIEW_KEYS[brief.review.decision]));
      const met = brief.checks.filter(item => item.verdict === 'met').length;
      if (brief.checks.length && brief.review) facts.push(t('world.brief.checksMet', { met, total: brief.checks.length }));
      if (facts.length || brief.delivery) {
        const meta = el('p', 'w-meta w-now-facts', card);
        meta.textContent = facts.join(' · ');
        if (brief.delivery) {
          meta.dataset.delivery = brief.delivery.status;
          if (facts.length) meta.append(' · ');
          meta.append(brief.delivery.status === 'blocked'
            ? t('world.brief.deliveryBlocked', { reason: brief.delivery.reason || brief.delivery.status })
            : t('world.brief.deliveryState', { state: brief.delivery.status }), ' ');
          el('code', '', meta).textContent = brief.delivery.checkpoint.slice(0, 10);
        }
      }
    }
    if (brief.task) {
      const block = section(parent, 'task');
      heading(block, t('world.brief.task'));
      renderText(brief.task, el('div', 'w-markdown w-task-brief', block));
    }
    const lead = brief.lead;
    if (lead.kind === 'result') {
      const block = section(parent, 'result');
      heading(block, t(lead.status === 'failed' ? 'world.brief.stopped' : 'world.brief.result'), lead.status === 'failed' ? 'exception' : '');
      const paragraphs = lead.markdown.trim().split(/\n\s*\n/);
      const holder = el('div', 'w-markdown w-result', block);
      holder.id = `w-result-${brief.person.id}`;
      renderText(resultOpen ? lead.markdown : paragraphs[0], holder);
      if (paragraphs.length > 1) {
        const toggle = el('button', 'w-link', block);
        toggle.type = 'button';
        toggle.textContent = t(resultOpen ? 'world.brief.showLess' : 'world.brief.showAll');
        toggle.setAttribute('aria-expanded', String(resultOpen));
        toggle.setAttribute('aria-controls', holder.id);
        toggle.addEventListener('click', () => {
          resultOpen = !resultOpen;
          rerender();
          body.querySelector<HTMLElement>('[data-section="result"] .w-link')?.focus({ preventScroll: true });
        });
      }
    }
    if (brief.checks.length) {
      const block = section(parent, 'checks');
      heading(block, t('world.brief.checks'));
      const list = el('ul', 'w-checks', block);
      for (const item of brief.checks) check(list, item);
    }
  }

  function fileChip(parent: HTMLElement, file: BriefFile) {
    const chip = el('button', 'w-file-chip', parent);
    chip.type = 'button';
    chip.dataset.visual = file.visual;
    chip.innerHTML = iconHtml(GLYPHS[file.visual]);
    el('span', '', chip).textContent = file.name;
    chip.title = file.path || file.name;
    chip.setAttribute('aria-label', t('world.brief.openFile', { name: file.name }));
    chip.addEventListener('click', () => ports.open(file.item));
  }

  function entry(list: HTMLElement, item: BriefEntry) {
    const tone = TONE[item.visual];
    const row = el('li', 'w-entry', list);
    row.dataset.visual = item.visual;
    row.dataset.tone = tone;
    el('span', 'w-entry-dot', row).setAttribute('aria-hidden', 'true');
    const open = el('button', 'w-entry-open', row);
    open.type = 'button';
    const meta = el('span', 'w-entry-meta', open);
    const time = el('time', 'w-entry-time', meta);
    time.dateTime = new Date(item.at).toISOString();
    time.textContent = clock(item.at);
    const kind = el('span', 'w-kind', meta);
    kind.dataset.tone = tone;
    kind.textContent = kindLabel(item.item);
    el('span', 'w-entry-title', open).textContent = item.title;
    const summary = gist(item.summary);
    if (summary && summary !== item.title) el('span', 'w-entry-summary', open).textContent = summary;
    open.addEventListener('click', () => ports.open(item.item));
    if (item.files.length) {
      const files = el('div', 'w-entry-files', row);
      files.setAttribute('role', 'group');
      files.setAttribute('aria-label', t('world.brief.files'));
      for (const file of item.files) fileChip(files, file);
    }
  }

  /** While the journal loads, a lead's latest report (already read by the office) stands in for its timeline. */
  function provisional(brief: Brief): BriefEntry | null {
    const lead = brief.lead;
    if (lead.kind !== 'report') return null;
    const item: LeadDeliverable = { id: `report:${lead.turn.turnId}`, kind: 'report', reportKind: lead.reportKind, title: lead.title,
      projectPath: '', createdAt: lead.at, summary: lead.markdown.startsWith(lead.title) ? lead.markdown.slice(lead.title.length) : lead.markdown,
      source: { conversationId: brief.person.id, turnId: lead.turn.turnId, ...(lead.turn.ordinal !== undefined ? { ordinal: lead.turn.ordinal } : {}) } };
    return { item, visual: lead.reportKind, at: lead.at, title: lead.title, summary: item.summary || '', task: '', files: [] };
  }

  function skeleton(parent: HTMLElement) {
    const rows = el('div', 'w-skeleton', parent);
    rows.setAttribute('aria-hidden', 'true');
    for (const width of ['72%', '54%', '64%']) el('i', '', rows).style.setProperty('--w', width);
  }

  function timeline(parent: HTMLElement, brief: Brief, now: number) {
    const journal = brief.journal;
    const block = section(parent, 'progress');
    const count = journal.days.reduce((sum, day) => sum + day.entries.length, 0);
    heading(block, t('world.brief.progress'), '', journal.status === 'ok' ? count : undefined);
    block.setAttribute('aria-busy', String(journal.status === 'loading'));
    const stand = journal.status === 'loading' || journal.status === 'unavailable' ? provisional(brief) : null;
    if (stand) {
      el('h4', 'w-day', block).textContent = deliverableDayLabel(new Date(stand.at).setHours(0, 0, 0, 0), now, locale());
      entry(el('ol', 'w-timeline', block), stand);
    }
    if (journal.status === 'loading') {
      el('p', 'w-visually-hidden', block).textContent = t('world.brief.loading');
      skeleton(block);
      return;
    }
    if (stand) return;
    if (journal.status === 'error') {
      const note = el('p', 'w-empty-note', block);
      note.textContent = t('world.brief.failed');
      const retry = createButton({ label: t('world.brief.retry'), size: 'sm', variant: 'secondary', onClick: () => ports.retry() });
      note.append(' ', retry);
      return;
    }
    if (!count) {
      el('p', 'w-empty-note', block).textContent = t('world.brief.noTimeline');
      return;
    }
    for (const day of journal.days) {
      el('h4', 'w-day', block).textContent = deliverableDayLabel(day.start, now, locale());
      const list = el('ol', 'w-timeline', block);
      for (const item of day.entries) entry(list, item);
    }
    if (journal.more) {
      const more = createButton({ label: t('world.brief.more'), size: 'sm', variant: 'secondary', disabled: journal.loadingMore,
        onClick: () => ports.more() });
      more.classList.add('w-more');
      block.append(more);
    }
  }

  function files(parent: HTMLElement, brief: Brief, now: number) {
    const journal = brief.journal;
    if (journal.status !== 'ok') return;
    const block = section(parent, 'files');
    heading(block, t('world.brief.files'), '', journal.files.length);
    if (!journal.files.length) {
      el('p', 'w-empty-note', block).textContent = t('world.brief.noFiles');
      return;
    }
    const list = el('ul', 'w-files', block);
    for (const file of journal.files) {
      const row = el('li', 'w-card w-file', list);
      row.dataset.visual = file.visual;
      const open = el('button', 'w-file-open', row);
      open.type = 'button';
      open.title = file.path || file.name;
      open.setAttribute('aria-label', t('world.brief.openFile', { name: file.name }));
      const icon = el('span', 'w-file-icon', open);
      icon.innerHTML = iconHtml(GLYPHS[file.visual]);
      const text = el('span', 'w-file-text', open);
      el('b', '', text).textContent = file.name;
      el('span', '', text).textContent = [deliverableDayLabel(new Date(file.at).setHours(0, 0, 0, 0), now, locale()), clock(file.at), size(file.sizeBytes)]
        .filter(Boolean).join(' · ');
      open.addEventListener('click', () => ports.open(file.item));
      if (file.item.artifactId) {
        const download = el('a', 'ui-icon-btn ui-icon-btn--sm w-file-download', row);
        download.href = ports.downloadUrl(file.item.artifactId);
        download.setAttribute('download', file.name);
        download.innerHTML = iconHtml('download');
        download.title = t('world.brief.download', { name: file.name });
        download.setAttribute('aria-label', t('world.brief.download', { name: file.name }));
      }
    }
  }

  function render(brief: Brief, identity: PanelIdentity, now: number) {
    const person = brief.person;
    if (person.id !== personId) resetPerson();
    lastRender = { brief, identity, now };
    personId = person.id;
    head.dataset.person = person.id;
    head.hidden = false;
    body.hidden = false;
    head.style.setProperty('--c', identity.color);
    body.style.setProperty('--c', identity.color);
    portrait.style.cssText = portraitStyle(person, worldTheme(), 92);
    name.textContent = person.name;
    name.title = person.detail ? `${person.name} — ${person.detail}` : person.name;
    status.textContent = identity.state;
    status.dataset.tone = identity.tone;
    team.textContent = identity.team;
    team.hidden = !identity.team;
    body.replaceChildren();
    const listed = new Set(brief.decisions.map(item => item.guidanceId));
    const decisions = [...brief.decisions, ...[...answeredHere.values()].filter(item => !listed.has(item.guidanceId))]
      .sort((a, b) => Number(b.blocking) - Number(a.blocking) || a.at - b.at);
    for (const [id, mount] of answers) {
      if (decisions.some(item => item.guidanceId === id)) continue;
      mount.dispose();
      answers.delete(id);
    }
    if (decisions.length) questionCard(body, decisions);
    if (brief.overdue.length) {
      const block = section(body, 'overdue');
      heading(block, t('world.brief.overdue'), 'exception');
      const list = el('ul', 'w-list', block);
      for (const item of brief.overdue) {
        el('li', '', list).textContent = `${item.title} · ${t('world.brief.dueAgo', { when: ago(item.dueAt, now) })}`;
      }
    }
    if (person.role === 'worker') workerNow(body, brief);
    else leadNow(body, brief, now);
    if (person.role !== 'worker' || brief.journal.days.length) timeline(body, brief, now);
    files(body, brief, now);
    const footer = el('footer', 'w-classic-note', body);
    el('p', '', footer).textContent = t('world.brief.classicNote');
    const classic = createButton({ label: t('world.brief.openClassic'), size: 'sm', variant: 'secondary', onClick: () => ports.conversation(null) });
    footer.append(classic);
  }

  return {
    head, body,
    render,
    person: () => personId,
    isOpen: () => !head.hidden,
    hide() { head.hidden = true; body.hidden = true; personId = ''; resetPerson(); lastRender = null; },
    focus() { body.focus({ preventScroll: true }); },
  };
}
export type Panel = ReturnType<typeof createPanel>;
