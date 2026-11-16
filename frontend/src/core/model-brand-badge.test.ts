// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { brandBadgeHtml } from './model-brand-badge';
import { MODEL_BRAND_ICONS, brandIconHtml } from './model-brand-icons';

const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const parsed = (html: string) => { const node = document.createElement('span'); node.innerHTML = html; return node.innerHTML; };

describe('brandBadgeHtml', () => {
  it('renders a sized slot, then fills marks already mounted and inserted later', async () => {
    const early = brandBadgeHtml('claude', 14);
    const stale = brandBadgeHtml('"><img src=x>', 12);
    expect(early).toContain('width:14px;height:14px');
    expect(stale).toContain('brand="generic"');
    document.body.innerHTML = early;
    for (let i = 0; i < 20 && !customElements.get('tofu-brand-mark'); i++) await settle();
    const badge = document.querySelector<HTMLElement>('.stg-brand-icon')!;
    expect(badge.innerHTML).toBe(parsed(MODEL_BRAND_ICONS.claude));
    expect(badge.style.color).not.toBe('');
    document.body.insertAdjacentHTML('beforeend', stale);
    expect(document.body.lastElementChild!.innerHTML).toBe(parsed(MODEL_BRAND_ICONS.generic));
    expect(brandBadgeHtml('openai', 16)).toBe(brandIconHtml('openai', 16));
  });
});
