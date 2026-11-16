/** The person panel beside the office: one header (portrait, name, state, the Brief/Chat switch, close)
 * over the brief, which leads with what the person did, the evidence and the decisions waiting on the owner.
 * A lead with open questions leads with a compact question card instead (question, the shared answer control
 * with its choices, own-answer box and default, the project, '1/N' paging, "answer in conversation"); the
 * rest of the brief folds behind a "progress" disclosure below it.
 * "Chat" hands the same column to the application's own conversation view and composer (the shell docks the
 * classic ConversationSurface below this header), so chatting with a person is the classic chat itself.
 * Entry point: createPanel. Dependencies: i18n, ui button factory, safe text projection, sprites. */
import { t, type I18nKey } from '../../i18n';
import { createButton } from '../../ui';
import type { ChoiceInteraction, ChoicePorts } from '../../rich-response/choices';
import { mountGuidanceAnswerFor, type GuidanceAnswerMount } from '../../rich-response/guidance-answer';
import type { Brief, BriefCheck, BriefDecision, TurnRef, Verdict } from './brief';
import type { ReportKind } from './model';
import { renderText } from './text';
import { worldTheme } from './skin';
import { portraitStyle } from './sprites';

export type PanelTab = 'brief' | 'chat';
export interface PanelPorts {
  close(): void;
  chat(turn: TurnRef | null): void;
  brief(): void;
  /** Answer ports bound to this question's conversation and Turn; undefined where it cannot be answered. */
  answerPorts(decision: BriefDecision): ChoicePorts | undefined;
  /** Opens the lead's conversation at this question with the composer replying to it. */
  answerInChat(decision: BriefDecision): void;
  /** The owner answered this question from the card. */
  answered(decision: BriefDecision): void;
}
export interface PanelIdentity { team: string; color: string; state: string }

const REPORT_KEYS = {
  progress: 'world.brief.report.progress', delivery: 'world.brief.report.delivery',
  blocked: 'world.brief.report.blocked', decision: 'world.brief.report.decision',
} as const satisfies Record<ReportKind, I18nKey>;
const REVIEW_KEYS = {
  accept: 'world.brief.review.accept', rework: 'world.brief.review.rework', reject: 'world.brief.review.reject',
} as const satisfies Record<'accept' | 'rework' | 'reject', I18nKey>;
const VERDICT_ICON: Record<Verdict, string> = { met: '✓', unmet: '✕', unknown: '?', unreviewed: '○' };
const VERDICT_KEYS = {
  met: 'world.brief.check.met', unmet: 'world.brief.check.unmet', unknown: 'world.brief.check.unknown', unreviewed: 'world.brief.check.unreviewed',
} as const satisfies Record<Verdict, I18nKey>;

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
  const line = el('p', '', titles);
  const tabs = el('div', 'w-tabs', head);
  tabs.setAttribute('role', 'group');
  const briefTab = el('button', 'w-tab', tabs);
  briefTab.type = 'button';
  briefTab.textContent = t('world.panel.brief');
  const chatTab = el('button', 'w-tab', tabs);
  chatTab.type = 'button';
  chatTab.textContent = t('world.panel.chat');
  const close = createButton({ label: t('world.panel.close'), size: 'sm', onClick: () => ports.close() });
  close.classList.add('w-panel-close');
  head.append(close);
  const body = el('section', 'w-brief');
  body.hidden = true;
  body.tabIndex = -1;
  body.setAttribute('aria-labelledby', name.id);
  let tab: PanelTab = 'brief';
  let personId = '';
  // The question card's state for the open person: the page shown, the mounted answer controls (kept so a
  // draft survives live repaints) and the questions answered here (still shown, marked answered, after the
  // office stops listing them).
  let questionId = '';
  const answers = new Map<string, GuidanceAnswerMount>();
  const answeredHere = new Map<string, BriefDecision>();
  let lastRender: { brief: Brief; identity: PanelIdentity; now: number } | null = null;
  let progressOpen = false;
  function resetQuestions() {
    progressOpen = false;
    for (const mount of answers.values()) mount.dispose();
    answers.clear();
    answeredHere.clear();
    questionId = '';
  }
  const markdownHtml = (source: string) => {
    const holder = document.createElement('div');
    renderText(source, holder);
    return holder.innerHTML;
  };
  briefTab.addEventListener('click', () => { setTab('brief'); ports.brief(); });
  chatTab.addEventListener('click', () => ports.chat(null));

  function setTab(next: PanelTab) {
    tab = next;
    briefTab.setAttribute('aria-pressed', String(next === 'brief'));
    chatTab.setAttribute('aria-pressed', String(next === 'chat'));
    body.hidden = head.hidden || next !== 'brief';
  }

  function heading(parent: HTMLElement, text: string, tone = ''): HTMLElement {
    const node = el('h3', `w-brief-head${tone ? ` w-tone-${tone}` : ''}`, parent);
    node.textContent = text;
    return node;
  }
  function revealLink(parent: HTMLElement, turn: TurnRef | null, text = t('world.brief.showInChat')) {
    const link = el('button', 'w-link', parent);
    link.type = 'button';
    link.textContent = text;
    if (turn) link.dataset.turn = turn.turnId;
    link.addEventListener('click', () => ports.chat(turn));
    return link;
  }
  function markAnswered(decision: BriefDecision) {
    if (answeredHere.has(decision.guidanceId)) return;
    answeredHere.set(decision.guidanceId, decision);
    ports.answered(decision);
    if (lastRender && personId === lastRender.brief.person.id) render(lastRender.brief, lastRender.identity, lastRender.now);
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
    const card = el('section', 'w-question-card', parent);
    card.dataset.guidanceId = decision.guidanceId;
    card.dataset.state = answered ? 'answered' : 'open';
    const top = el('header', 'w-question-top', card);
    heading(top, t('world.brief.decisions'), 'decision');
    if (answered) el('span', 'w-question-badge', top).textContent = t('world.brief.answered');
    if (decisions.length > 1) {
      const pager = el('div', 'w-question-pager', top);
      const step = (delta: number) => {
        questionId = decisions[(index + delta + decisions.length) % decisions.length].guidanceId;
        if (lastRender) render(lastRender.brief, lastRender.identity, lastRender.now);
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
    const text = el('div', 'w-check-text', row);
    renderText(item.text, el('div', '', text));
    if (item.evidence) renderText(item.evidence, el('div', 'w-check-evidence', text));
  }

  function render(brief: Brief, identity: PanelIdentity, now: number) {
    const person = brief.person;
    if (person.id !== personId) resetQuestions();
    lastRender = { brief, identity, now };
    personId = person.id;
    head.dataset.person = person.id;
    head.hidden = false;
    head.style.setProperty('--c', identity.color);
    body.style.setProperty('--c', identity.color);
    portrait.style.cssText = portraitStyle(person, worldTheme(), 92);
    name.textContent = person.name;
    line.textContent = [identity.team, identity.state].filter(Boolean).join(' · ');
    body.replaceChildren();
    const listed = new Set(brief.decisions.map(item => item.guidanceId));
    const decisions = [...brief.decisions, ...[...answeredHere.values()].filter(item => !listed.has(item.guidanceId))]
      .sort((a, b) => Number(b.blocking) - Number(a.blocking) || a.at - b.at);
    for (const [id, mount] of answers) {
      if (decisions.some(item => item.guidanceId === id)) continue;
      mount.dispose();
      answers.delete(id);
    }
    // Questions first; everything else in the brief folds behind "progress" while one is open.
    let rest: HTMLElement = body;
    if (decisions.length) {
      questionCard(body, decisions);
      const more = el('details', 'w-brief-more', body);
      el('summary', 'w-brief-more-summary', more).textContent = t('world.brief.progress');
      more.open = progressOpen;
      more.addEventListener('toggle', () => { progressOpen = more.open; });
      rest = more;
    }
    if (brief.overdue.length) {
      const section = el('section', 'w-brief-section', rest);
      heading(section, t('world.brief.overdue'), 'exception');
      const list = el('ul', 'w-list', section);
      for (const item of brief.overdue) {
        el('li', '', list).textContent = `${item.title} · ${t('world.brief.dueAgo', { when: ago(item.dueAt, now) })}`;
      }
    }
    const did = el('section', 'w-brief-section', rest);
    const lead = brief.lead;
    if (lead.kind === 'report') {
      heading(did, t(REPORT_KEYS[lead.reportKind]), lead.reportKind === 'delivery' ? 'deliverable' : '');
      const meta = el('p', 'w-meta', did);
      meta.textContent = `${ago(lead.at, now)}${lead.unread ? ` · ${t('world.brief.unread')}` : ''}`;
      if (lead.title) el('p', 'w-brief-title', did).textContent = lead.title;
      renderText(lead.markdown.startsWith(lead.title) ? lead.markdown.slice(lead.title.length) : lead.markdown, el('div', 'w-markdown', did));
      revealLink(did, lead.turn);
    } else if (lead.kind === 'result') {
      heading(did, t(lead.status === 'failed' ? 'world.brief.stopped' : 'world.brief.reported'), lead.status === 'failed' ? 'exception' : '');
      renderText(lead.markdown, el('div', 'w-markdown', did));
      revealLink(did, lead.turn);
    } else if (lead.kind === 'task') {
      heading(did, t(person.state === 'working' ? 'world.brief.workingOn' : 'world.brief.assigned'));
      renderText(lead.markdown, el('div', 'w-markdown', did));
      revealLink(did, lead.turn);
    } else {
      heading(did, t('world.brief.did'));
      el('p', 'w-meta', did).textContent = t('world.brief.nothingYet');
      revealLink(did, null);
    }
    if (brief.checks.length || brief.review || brief.delivery) {
      const section = el('section', 'w-brief-section', rest);
      heading(section, t('world.brief.evidence'));
      if (brief.checks.length) {
        const list = el('ul', 'w-checks', section);
        for (const item of brief.checks) check(list, item);
      }
      if (brief.review) {
        const review = el('p', 'w-review', section);
        review.dataset.decision = brief.review.decision;
        review.textContent = `${t(REVIEW_KEYS[brief.review.decision])}${brief.review.note ? ` · ${brief.review.note}` : ''}`;
      }
      if (brief.delivery) {
        const delivery = el('p', 'w-delivery', section);
        delivery.dataset.status = brief.delivery.status;
        const code = el('code', '', delivery);
        code.textContent = brief.delivery.checkpoint.slice(0, 10);
        delivery.append(` · ${brief.delivery.status === 'blocked'
          ? t('world.brief.deliveryBlocked', { reason: brief.delivery.reason || brief.delivery.status })
          : t('world.brief.deliveryState', { state: brief.delivery.status })}`);
      }
    }
    if (brief.board) {
      const section = el('section', 'w-brief-section', rest);
      heading(section, t('world.brief.board'));
      const bar = el('div', 'w-progress', section);
      bar.style.setProperty('--done', String(brief.board.total ? brief.board.done / brief.board.total : 0));
      bar.setAttribute('role', 'img');
      bar.setAttribute('aria-label', t('world.team.board', { done: brief.board.done, total: brief.board.total }));
      el('span', 'w-meta', section).textContent = t('world.team.board', { done: brief.board.done, total: brief.board.total });
      if (brief.board.summary) renderText(brief.board.summary, el('div', 'w-markdown', section));
      if (brief.board.tasks.length) {
        const list = el('ul', 'w-tasks', section);
        for (const task of brief.board.tasks) {
          const row = el('li', '', list);
          row.dataset.status = task.status;
          row.textContent = task.progress ? `${task.title} · ${task.progress}` : task.title;
        }
      }
    }
    setTab(tab);
  }

  return {
    head, body,
    render,
    setTab,
    tab: () => tab,
    person: () => personId,
    isOpen: () => !head.hidden,
    hide() { head.hidden = true; body.hidden = true; personId = ''; tab = 'brief'; resetQuestions(); lastRender = null; },
    focus() { (tab === 'brief' ? body : chatTab).focus({ preventScroll: true }); },
  };
}
export type Panel = ReturnType<typeof createPanel>;
