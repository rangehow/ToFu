/** Shared action summary decoration for native intent headers;
 * evidence, disclosures, actions and reconciliation remain with their owners.
 * Used by Surface/default and classic rich rendering (including lazy hydration).
 */
import type { ConversationBlockViewModel } from '../presentation/conversation-view-model';
import { toolSummaryIdentity } from '../presentation/tool-summary-identity';
import { setStableData } from './renderers/stable-dom';

const INTENT_SELECTOR = '.ptool-text, .ptool-cmd-desc-inline, .conversation-tool-card__title, '
  + '.ptc-title, .hg-title, .sw-header-title';

/** Normalize native outer summaries without duplicating their tool identity. */
export function decorateToolSummary(node: HTMLElement, block: ConversationBlockViewModel): void {
  const identity = toolSummaryIdentity(block);
  if (!identity.name) return;
  let header = node.querySelector<HTMLElement>(
    '.ptool-cmd-header, .ptool-result-header, .ptool-results-header, .ptool-line, '
    + '.ptc-head, .conversation-tool-card__summary, .sw-header, .hg-header, .ptool-todo-header, '
    + '.timer-watcher-header, .conversation-tool-summary');
  if (!header && node.hasChildNodes()) {
    // Structured/custom bodies without an action heading use this same outer
    // grammar. Never wrap/reparent their form, media or interactive evidence.
    header = node.ownerDocument.createElement('div');
    header.className = 'conversation-tool-summary';
    header.dataset.toolSummaryGenerated = 'true';
    const title = node.ownerDocument.createElement('span'); title.className = 'ptool-text';
    header.append(title); node.prepend(header);
  }
  if (!header) return;
  // A renderer-authored summary row (a question's title) keeps its own text.
  if (header.dataset.toolSummaryGenerated === 'true') {
    const title = header.querySelector('.ptool-text')!;
    if (title.textContent !== identity.intent) title.textContent = identity.intent;
  }
  setStableData(header, 'actionSummary', 'true');
  // Exact identity stays available on demand without another header line.
  // A title the native renderer set is authoritative.
  const value = identity.name + (identity.target ? `  ${identity.target}` : '');
  const intent = header.querySelector<HTMLElement>(INTENT_SELECTOR);
  if (intent && (!intent.title || intent.dataset.intentTitle === 'true')) {
    const full = [intent.textContent?.trim() ?? '', value].filter(Boolean).join('\n');
    if (intent.title !== full) intent.title = full;
    setStableData(intent, 'intentTitle', 'true');
  }
  // Routine terminal labels repeat the return itself. Preserve all meaningful
  // counts, exit codes, partial effects, warnings and unknown/custom badges.
  for (const badge of header.querySelectorAll<HTMLElement>(
    '.ptool-badge, .conversation-tool-card__chip, .conversation-tool-card__badge')) {
    const routine = /^(?:done|completed|success|ok|完成|已完成|成功)(?:\s*·\s*\d+(?:\.\d+)?s)?$/i
      .test(badge.textContent?.trim() ?? '');
    if (badge.hidden !== routine) badge.hidden = routine;
  }
}
