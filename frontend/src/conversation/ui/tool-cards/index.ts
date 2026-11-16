/**
 * Tool card registry: one presentation layer for tool rows. A registered
 * tool declares a localized title, a one-line summary from its arguments and
 * result, and an expanded view (`coordination.ts`); any other tool whose
 * result is a JSON object gets the generic labeled-field view. Identifiers
 * stay in the technical-details disclosure.
 *
 * Entry points: createToolCardPresentation, handleToolCardClick.
 * Dependencies: the translator/Markdown/conversation-title ports, the
 * checklist icon vocabulary and the shared lazy-result markup contract
 * (`details.ptool-result-block[data-lazy-tool-result]`,
 * `[data-deferred-tool-body]`) consumed by `../renderers/tool-blocks.ts`.
 */
import type {
  FreeText, ToolCardChrome, ToolPresentationPorts, ToolPresenter, ToolPresenterContext, ToolSummary,
  WorkDirectory, WorkRecord,
} from './contracts';
import { COORDINATION_TOOL_PRESENTERS } from './coordination';
import { createToolCardEnrichment, type ToolCardEnrichment } from './enrichment';
import { genericFieldsHtml } from './generic';
import { chipHtml, debugHtml, escape } from './markup';
import { readToolView, record, type ToolView } from './values';

export type { ToolCardChrome, ToolPresentationPorts } from './contracts';

const SETTLED = new Set(['done', 'completed']);
const FAILED = new Set(['error', 'failed']);

export interface ToolCardPresentation {
  /** Escaped header markup (title, summary, chips), or '' when no presenter applies. */
  headerHtml(round: unknown): string;
  /** The same header as plain text, for tooltips and accessible names. */
  headerText(round: unknown): string;
  /** A settled round's card, or '' to fall through to the next renderer. */
  renderCardHtml(round: unknown, chrome: ToolCardChrome): string;
  /** Labeled-field card for an unregistered tool with a JSON-object result, else ''. */
  renderGenericCardHtml(round: unknown, chrome: ToolCardChrome & { readonly titleHtml: string }): string;
  /** Registered tool names (sibling-title derivation leaves their rows alone). */
  hasPresenter(round: unknown): boolean;
  /** A failed round's plain-language reason from its presenter, or '' for the generic one. */
  failureText(round: unknown): string;
  /** Called when a card's late facts (a task title, a translation) settle; returns the unsubscribe. */
  subscribe(listener: () => void): () => void;
  /** Repaint subscribed cards after a setting they read changed (translation, debug mode). */
  refresh(): void;
}

function createWorkDirectory(ports: ToolPresentationPorts, enrichment: ToolCardEnrichment): WorkDirectory {
  // Delegations name their assignments once; a page revisits the same few.
  const assignments = new Map<string, WorkRecord>();
  // The catalog holds only loaded pages of Worker conversations; an older one is looked up once.
  // undefined while that lookup runs, so no card paints a placeholder it must retract.
  const conversationTitle = (conversationId: string): string | undefined => {
    if (!conversationId) return '';
    const known = ports.conversationTitle(conversationId)?.trim();
    if (known) return known;
    const looked = enrichment.conversationTitle(conversationId);
    if (!looked) return '';
    return looked.settled ? looked.value : undefined;
  };
  return {
    remember(assignmentId, work) {
      if (!assignmentId) return;
      const known = assignments.get(assignmentId);
      assignments.set(assignmentId, {
        title: work.title || known?.title || '',
        acceptance: work.acceptance?.length ? work.acceptance : known?.acceptance ?? [],
        conversationId: work.conversationId || known?.conversationId || '',
      });
    },
    knows: (assignmentId) => assignments.has(assignmentId),
    /**
     * Every assignment of a Worker conversation (follow-ups included) is named
     * by that conversation's title; an assignment this page never saw is looked
     * up once, and its title is '' until the lookup settles.
     */
    taskTitle(assignmentId, conversationId = '') {
      const known = assignments.get(assignmentId);
      if (known?.title) return known.title;
      let conversation = known?.conversationId || conversationId;
      if (!conversation) {
        const looked = enrichment.assignmentConversation(assignmentId);
        if (looked && !looked.settled) return '';
        conversation = looked?.value ?? '';
      }
      const title = conversationTitle(conversation);
      return title === undefined ? '' : title || ports.translate('toolCard.unnamedTask');
    },
    acceptance: (assignmentId) => assignments.get(assignmentId)?.acceptance ?? [],
    conversationTitle(conversationId) {
      const title = conversationTitle(conversationId);
      return title === undefined ? '' : title || ports.translate('toolCard.unnamedConversation');
    },
  };
}

function summaryHtml(title: string, summary: ToolSummary): string {
  return `<span class="ptool-coord-title">${escape(title)}</span>`
    + (summary.text ? `<span class="ptool-coord-sep" aria-hidden="true"> · </span><span class="ptool-coord-summary">${escape(summary.text)}</span>` : '')
    + (summary.chips ?? []).map(chipHtml).join('');
}

function cardHtml(
  view: ToolView, chrome: ToolCardChrome, headerHtml: string, headerText: string, bodyHtml: string,
  kind: string,
): string {
  const snapshot = record(view.round._snapshotToolResult);
  const lazy = typeof snapshot.preview === 'string' && !view.round.toolContent;
  const deferred = view.round._deferToolResultBody === true && !lazy;
  const body = deferred
    ? '<div class="ptool-result-body ptool-coord-body" data-deferred-tool-body=""></div>'
    : `<div class="ptool-result-body ptool-coord-body">${bodyHtml}</div>`;
  return `<details class="ptool-result-block ptool-coord-card" data-tool-card="${escape(kind)}"${lazy ? ' data-lazy-tool-result=""' : ''} data-rn="${escape(view.round.roundNum)}">`
    + '<summary class="ptool-line ptool-result-header">'
    + `<span class="ptool-icon">${chrome.iconHtml}</span>${chrome.compactionLabelHtml ?? ''}${chrome.rootPillHtml ?? ''}`
    + `<span class="ptool-text ptool-coord-header" title="${escape(headerText)}">${headerHtml}</span>`
    + `${chrome.badgeHtml ?? ''}${chrome.rightControlsHtml ?? ''}</summary>${body}</details>`;
}

export function createToolCardPresentation(ports: ToolPresentationPorts): ToolCardPresentation {
  const enrichment = createToolCardEnrichment(ports);
  const freeText = (source: string): FreeText => {
    const translation = enrichment.translation(source);
    if (translation?.settled && translation.value) return { shown: translation.value, original: source, pending: false };
    return { shown: source, original: '', pending: Boolean(translation && !translation.settled) };
  };
  const context: ToolPresenterContext = {
    t: ports.translate, markdown: ports.markdown, work: createWorkDirectory(ports, enrichment), freeText,
  };

  function presenter(view: ToolView): ToolPresenter | null {
    const found = Object.prototype.hasOwnProperty.call(COORDINATION_TOOL_PRESENTERS, view.name)
      ? COORDINATION_TOOL_PRESENTERS[view.name] : null;
    return found && (!found.applies || found.applies(view, context)) ? found : null;
  }

  function header(view: ToolView, found: ToolPresenter): { html: string; text: string } {
    const title = found.title(view, context);
    const summary = found.summary(view, context);
    // A failed call changed nothing: chips built from its arguments would
    // present the requested outcome (an accept, a merge) as if it happened.
    if (FAILED.has(view.status)) {
      return { html: summaryHtml(title, { text: summary.text }), text: [title, summary.text].filter(Boolean).join(' · ') };
    }
    const parts = [title, summary.text, ...(summary.chips ?? []).map((chip) => chip.label)].filter(Boolean);
    return { html: summaryHtml(title, summary), text: parts.join(' · ') };
  }

  function debugSection(view: ToolView, found: ToolPresenter | null): string {
    if (!ports.debugEnabled?.()) return '';
    return debugHtml(context.t('toolCard.debug.title'), found?.debug?.(view, context) ?? [],
      context.t('toolCard.debug.raw'), view.resultText);
  }

  return {
    headerHtml(round) {
      const view = readToolView(round);
      const found = presenter(view);
      return found ? header(view, found).html : '';
    },
    headerText(round) {
      const view = readToolView(round);
      const found = presenter(view);
      return found ? header(view, found).text : '';
    },
    hasPresenter: (round) => presenter(readToolView(round)) !== null,
    failureText(round) {
      const view = readToolView(round);
      const found = presenter(view);
      return found && FAILED.has(view.status) ? found.failure?.(view, context) ?? '' : '';
    },
    subscribe: (listener) => enrichment.subscribe(listener),
    refresh: () => enrichment.refresh(),
    renderCardHtml(round, chrome) {
      const view = readToolView(round);
      const found = presenter(view);
      if (!found || !SETTLED.has(view.status)) return '';
      const { html, text } = header(view, found);
      return cardHtml(view, chrome, html, text,
        found.body(view, context) + debugSection(view, found), view.name);
    },
    renderGenericCardHtml(round, chrome) {
      const view = readToolView(round);
      if (presenter(view) || !SETTLED.has(view.status) || !view.result || view.partial) return '';
      const fields = genericFieldsHtml(view.result, context.t, context.markdown);
      if (!fields) return '';
      return cardHtml(view, chrome, chrome.titleHtml, '', fields + debugSection(view, null), 'generic');
    },
  };
}

/** Delegated click for a card's "open conversation" control. */
export function handleToolCardClick(event: Event, openConversation: (conversationId: string) => void): void {
  const target = event.target as Partial<Element> | null;
  if (!target || typeof target.closest !== 'function') return;
  const button = (target as Element).closest<HTMLElement>('[data-tool-open-conversation]');
  const conversationId = button?.dataset.toolOpenConversation;
  if (!conversationId) return;
  event.preventDefault();
  event.stopPropagation();
  openConversation(conversationId);
}
