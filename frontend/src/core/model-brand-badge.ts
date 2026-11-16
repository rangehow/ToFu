/**
 * Startup model/provider brand badge.
 *
 * Responsibility: render a correctly sized badge synchronously while the SVG
 * registry (model-brand-icons.ts) loads as its own chunk on first use. Each
 * badge holds a <tofu-brand-mark> element; the registry defines that element
 * when it arrives, so every mark already in the document, and any inserted
 * later from HTML built before the load, upgrades in place to its SVG and
 * brand color. Once loaded, badges render complete.
 * Entry point: brandBadgeHtml. Dependencies: model-brand-icons.ts (dynamic).
 */
type BrandRegistry = typeof import('./model-brand-icons');

const MARK_ELEMENT = 'tofu-brand-mark';
let registry: BrandRegistry | null = null;
let loading: Promise<void> | null = null;

function defineMark(module: BrandRegistry): void {
  if (typeof customElements === 'undefined' || customElements.get(MARK_ELEMENT)) return;
  customElements.define(MARK_ELEMENT, class extends HTMLElement {
    connectedCallback(): void {
      const { svg, color } = module.brandMark(this.getAttribute('brand'));
      const badge = this.parentElement;
      if (badge?.classList.contains('stg-brand-icon')) {
        badge.style.color = color;
        badge.innerHTML = svg;
      } else {
        this.outerHTML = svg;
      }
    }
  });
}

function loadRegistry(): void {
  loading ??= import('./model-brand-icons').then((module) => {
    registry = module;
    defineMark(module);
  }, () => { loading = null; });
}

/** Same markup contract as brandIconHtml; the mark fills in when the registry loads. */
export function brandBadgeHtml(brand: unknown, size = 20): string {
  if (registry) return registry.brandIconHtml(brand, size);
  loadRegistry();
  const key = typeof brand === 'string' && /^[\w-]+$/.test(brand) ? brand : 'generic';
  const safeSize = Number.isFinite(size) && size > 0 ? size : 20;
  return `<span class="stg-brand-icon" style="width:${safeSize}px;height:${safeSize}px">`
    + `<${MARK_ELEMENT} brand="${key}"></${MARK_ELEMENT}></span>`;
}
