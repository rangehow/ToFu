/**
 * The app's one fold control: a chevron pointing right when closed and
 * turning down when open. Never a bare triangle glyph or the native <details>
 * marker.
 *
 * Entry points: `DISCLOSURE_CHEVRON_SVG` (inline markup, used by
 * conversation cards and JS-driven toggles) and `createDisclosureSummary`.
 * Markup and classic runtime sections may write `class="ui-disclosure"`
 * directly on a <summary>, or on a button/row that carries aria-expanded;
 * 30-ui-kit.css paints the same path through a mask. Dependency-free.
 */

export const DISCLOSURE_CHEVRON_SVG = '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true"><path d="m6.5 5 3 3-3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export const DISCLOSURE_CLASS = 'ui-disclosure';

export function createDisclosureSummary(doc: Document, label?: string): HTMLElement {
  const summary = doc.createElement('summary');
  summary.className = DISCLOSURE_CLASS;
  if (label !== undefined) summary.textContent = label;
  return summary;
}
