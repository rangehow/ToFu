/**
 * Shared tool-card markup vocabulary: chips, labeled fields, collapsible
 * text, check marks and the technical-details disclosure. Every visible
 * label arrives already translated; this module authors no copy.
 */
import { escapeHtmlText as escape } from '../../../html-safety';
import { TOOL_ROUND_CHEVRON_RIGHT_SVG } from '../../presentation/tool-round-icons';

import type { ToolChip, ToolDebugField } from './contracts';

export { escape };

const MARK_ICONS = Object.freeze({
  met: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>',
  unmet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  unknown: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><circle cx="12" cy="17" r=".8" fill="currentColor"/></svg>',
  cancelled: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M8 12h8"/></svg>',
  open: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>',
});

export type MarkKind = keyof typeof MARK_ICONS;

export function markIcon(kind: MarkKind): string {
  return MARK_ICONS[kind];
}

export function chipHtml(chip: ToolChip): string {
  return `<span class="ptool-coord-chip ptool-coord-chip--${chip.tone}">${escape(chip.label)}</span>`;
}

/** One labeled fact; `valueHtml` must already be escaped markup. */
export function fieldHtml(label: string, valueHtml: string): string {
  return valueHtml
    ? `<div class="ptool-coord-field"><span class="ptool-coord-label">${escape(label)}</span><span class="ptool-coord-value">${valueHtml}</span></div>`
    : '';
}

export function sectionHtml(heading: string, innerHtml: string): string {
  return innerHtml
    ? `<section class="ptool-coord-section"><h4 class="ptool-coord-heading">${escape(heading)}</h4>${innerHtml}</section>`
    : '';
}

/** Every disclosure inside a card opens from the tool rows' chevron, which turns down when open. */
function toggleHtml(label: string): string {
  return `<summary class="ptool-coord-toggle">${TOOL_ROUND_CHEVRON_RIGHT_SVG}<span>${escape(label)}</span></summary>`;
}

/** A labeled disclosure around already-escaped markup. */
export function disclosureHtml(label: string, innerHtml: string, open = false): string {
  return innerHtml
    ? `<details class="ptool-coord-more"${open ? ' open' : ''}>${toggleHtml(label)}${innerHtml}</details>`
    : '';
}

/** Long prose stays one click away; `markdownHtml` is the trusted renderer's output. */
export function collapsibleHtml(label: string, markdownHtml: string, open = false): string {
  return markdownHtml ? disclosureHtml(label, `<div class="ptool-coord-prose md-content">${markdownHtml}</div>`, open) : '';
}

/**
 * Free text as shown (its translation when there is one) with the authored
 * original behind a toggle; both arguments are already-escaped markup.
 */
export function translatedTextHtml(
  shownHtml: string, originalHtml: string, labels: { readonly original: string; readonly translating: string },
  pending: boolean,
): string {
  return shownHtml
    + (originalHtml ? `<details class="ptool-coord-original">${toggleHtml(labels.original)}${originalHtml}</details>` : '')
    + (pending ? `<span class="ptool-coord-translating">${escape(labels.translating)}</span>` : '');
}

export function leadHtml(sentence: string): string {
  return sentence ? `<p class="ptool-coord-lead">${escape(sentence)}</p>` : '';
}

/** The card's one-line outcome, above its explanation. */
export function verdictHtml(sentence: string): string {
  return sentence ? `<p class="ptool-coord-lead ptool-coord-verdict"><strong>${escape(sentence)}</strong></p>` : '';
}

export function checkRowHtml(kind: MarkKind, stateLabel: string, titleHtml: string, detailHtml = ''): string {
  return `<li class="ptool-coord-check ptool-coord-check--${kind}">`
    + `<span class="ptool-coord-mark" role="img" aria-label="${escape(stateLabel)}" title="${escape(stateLabel)}">${markIcon(kind)}</span>`
    + `<span class="ptool-coord-check-text">${titleHtml}${detailHtml ? `<span class="ptool-coord-check-detail">${detailHtml}</span>` : ''}</span>`
    + '</li>';
}

export function listHtml(rows: readonly string[], className = 'ptool-coord-checks'): string {
  return rows.length ? `<ul class="${className}">${rows.join('')}</ul>` : '';
}

export function openConversationHtml(conversationId: string, label: string): string {
  return conversationId
    ? `<button type="button" class="ptool-coord-open" data-tool-open-conversation="${escape(conversationId)}">${markIcon('open')}<span>${escape(label)}</span></button>`
    : '';
}

/** Identifiers and the raw result stay available, never primary. */
export function debugHtml(label: string, fields: readonly ToolDebugField[], rawLabel: string, raw: string): string {
  const rows = fields.filter(([, value]) => value)
    .map(([name, value]) => `<div class="ptool-coord-debug-row"><span>${escape(name)}</span><code>${escape(value)}</code></div>`)
    .join('');
  const rawHtml = raw.trim()
    ? `<details class="ptool-coord-raw">${toggleHtml(rawLabel)}<pre>${escape(raw)}</pre></details>`
    : '';
  return rows || rawHtml
    ? `<details class="ptool-coord-debug">${toggleHtml(label)}${rows}${rawHtml}</details>`
    : '';
}
