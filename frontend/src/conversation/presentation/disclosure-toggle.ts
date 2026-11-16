/**
 * The one fold control inside conversation cards: the tool rows' chevron,
 * pointing right when closed and turning down when open (09-tool-cards.css
 * `.ptool-coord-toggle`). Entry point: disclosureToggleHtml. Callers pass
 * label markup they have already escaped; no native ▶ marker is ever shown.
 */
import { TOOL_ROUND_CHEVRON_RIGHT_SVG } from './tool-round-icons';

export function disclosureToggleHtml(labelHtml: string): string {
  return `<summary class="ptool-coord-toggle">${TOOL_ROUND_CHEVRON_RIGHT_SVG}<span>${labelHtml}</span></summary>`;
}
