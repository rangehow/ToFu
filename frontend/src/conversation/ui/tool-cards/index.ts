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
  ToolCardChrome, ToolPresentationPorts, ToolPresenter, ToolPresenterContext, ToolSummary,
  WorkDirectory, WorkRecord,
} from './contracts';
import { COORDINATION_TOOL_PRESENTERS } from './coordination';
import { genericFieldsHtml } from './generic';
import { chipHtml, debugHtml, escape } from './markup';
import { readToolView, record, type ToolView } from './values';

export type { ToolCardChrome, ToolPresentationPorts } from './contracts';

const SETTLED = new Set(['done', 'completed']);

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
}

function createWorkDirectory(ports: ToolPresentationPorts): WorkDirectory {
  // Delegations name their assignments once; a page revisits the same few.
  const assignments = new Map<string, WorkRecord>();
  const conversationTitle = (conversationId: string): string => (conversationId
    ? ports.conversationTitle(conversationId)?.trim() ?? '' : '');
  return {
    remember(assignmentId, work) {
      const known = assignments.get(assignmentId);
      assignments.set(assignmentId, {
        title: work.title || known?.title || '',
        acceptance: work.acceptance?.length ? work.acceptance : known?.acceptance ?? [],
        conversationId: work.conversationId || known?.conversationId || '',
      });
    },
    knows: (assignmentId) => assignments.has(assignmentId),
    taskTitle(assignmentId, conversationId = '') {
      const known = assignments.get(assignmentId);
      return known?.title
        || conversationTitle(known?.conversationId || conversationId)
        || ports.translate('toolCard.unnamedTask');
    },
    acceptance: (assignmentId) => assignments.get(assignmentId)?.acceptance ?? [],
    conversationTitle: (conversationId) => conversationTitle(conversationId)
      || ports.translate('toolCard.unnamedConversation'),
  };
}

function summaryHtml(title: string, summary: ToolSummary): string {
  return `<span class="ptool-coord-title">${escape(title)}</span>`
    + (summary.text ? `<span class="ptool-coord-summary">${escape(summary.text)}</span>` : '')
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
  const context: ToolPresenterContext = {
    t: ports.translate, markdown: ports.markdown, work: createWorkDirectory(ports),
  };

  function presenter(view: ToolView): ToolPresenter | null {
    const found = Object.prototype.hasOwnProperty.call(COORDINATION_TOOL_PRESENTERS, view.name)
      ? COORDINATION_TOOL_PRESENTERS[view.name] : null;
    return found && (!found.applies || found.applies(view, context)) ? found : null;
  }

  function header(view: ToolView, found: ToolPresenter): { html: string; text: string } {
    const title = found.title(view, context);
    const summary = found.summary(view, context);
    const parts = [title, summary.text, ...(summary.chips ?? []).map((chip) => chip.label)].filter(Boolean);
    return { html: summaryHtml(title, summary), text: parts.join(' · ') };
  }

  function debugSection(view: ToolView, found: ToolPresenter | null): string {
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
