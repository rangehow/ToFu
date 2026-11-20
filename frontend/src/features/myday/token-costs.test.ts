// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { createDayTokenCostDisclosure, type DayTokenCosts } from './token-costs';

vi.mock('../../i18n', () => ({ _i18nLang: 'en', t: (key: string) => key }));

const date = '2026-11-19';
const empty = { tokens: 0, cost_cny: 0 };
const base: DayTokenCosts = {
  date, partial: false, total_cny: 0, models: [], used_fallback_rates: false,
  buckets: { input: empty, output: empty, cache_read: empty, cache_write: empty },
};

function open(host: HTMLElement): void {
  const details = host.querySelector('details')!;
  details.open = true;
  details.dispatchEvent(new Event('toggle'));
}

it.each([true, false])('distinguishes partial empty buckets from no usage (%s)', async (partial) => {
  const host = document.createElement('div');
  await createDayTokenCostDisclosure(() => host).show(date, Promise.resolve({ ...base, partial }));
  expect(host.textContent).not.toMatch(/myday.costEmpty|myday.costPartial/);
  open(host);
  expect(host.textContent?.includes('myday.costEmpty')).toBe(!partial);
  expect(host.textContent?.includes('myday.costPartial')).toBe(partial);
});

it('paints last known costs before the server responds and keeps the picker across refresh', async () => {
  const host = document.createElement('div');
  document.body.append(host);
  try {
    const card = createDayTokenCostDisclosure(() => host);
    const cached = {
      ...base, total_cny: 1,
      buckets: { ...base.buckets, input: { tokens: 7, cost_cny: 1 } },
      models: [{ id: 'm', name: 'Model', cost_cny: 2 }],
    };
    let finish!: (value?: DayTokenCosts) => void;
    const pending = new Promise<DayTokenCosts | undefined>((resolve) => { finish = resolve; });
    const render = card.show(date, pending, cached);
    expect(host.textContent).toContain('¥1.00');
    expect(host.textContent).toContain('myday.costLoading');
    expect(host.querySelector('select')).toBeNull();
    open(host);
    const picker = host.querySelector('select')!;
    picker.value = 'm';
    picker.focus();
    finish({ ...cached, total_cny: 3 });
    await render;
    expect(host.textContent).toContain('¥3.00');
    expect(document.activeElement).toBe(picker);
    expect(picker.value).toBe('m');
    await card.show(date, Promise.reject(new Error('offline')));
    expect(host.textContent).toContain('¥3.00');
    expect(host.textContent).toContain('myday.costUnavailable');
    expect(document.activeElement).toBe(picker);
  } finally {
    host.remove();
  }
});

it('cannot resurrect a closed card or paint yesterday in a new disclosure', async () => {
  const host = document.createElement('div');
  const card = createDayTokenCostDisclosure(() => host);
  const pending = card.show(date, Promise.resolve({ ...base, total_cny: 42 }));
  card.hide();
  await pending;
  expect(host.children).toHaveLength(0);
  await card.show('2026-11-20', Promise.resolve(base), base);
  card.restore(base);
  expect(host.textContent).toContain('myday.costUnavailable');
  expect(host.textContent).not.toContain('¥');
});
