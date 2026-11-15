// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RouterPortfolioPorts } from './router-portfolio';
import type { RouterPortfolioState } from '../api/router.generated';

const state = (enabled: boolean): RouterPortfolioState => ({
  review: { enabled, schedule: '30 9 * * *', taskId: 'system-1-router-portfolio-review', lastRun: null, lastResult: null },
  counts: { proposed: 3, started: 1, delivered: 2, negative: 1, dropped: 0, awaitingOwner: 1 },
  ledger: [{ unit: 'cny_per_day', initiatives: 4, running: 1, delivered: 2, negative: 1, promised: 100, measured: 60 }],
});
const catalog = {
  'routerPortfolio.summaryOff': '项目组合：已暂停', 'routerPortfolio.summaryOn': '项目组合：运行中',
  'routerPortfolio.hint': '每天复盘并启动已授权的事项', 'routerPortfolio.switch': '启动已授权的事项',
  'routerPortfolio.noActivity': '还没有启动过任何事项。', 'routerPortfolio.lastReview': '上次复盘：{time} — {result}',
  'routerPortfolio.counts': '{proposed} 个候选 · {running} 个进行中 · {delivered} 个已交付 · {negative} 个负结果',
  'routerPortfolio.failed': '未更改：{error}',
};

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => catalog })));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function setup(options: { set?: (enabled: boolean) => Promise<RouterPortfolioState> } = {}) {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('../i18n');
  await i18n.ready?.();
  const { createRouterPortfolio } = await import('./router-portfolio');
  const ports: RouterPortfolioPorts = {
    read: vi.fn(async () => state(false)),
    setEnabled: vi.fn(options.set ?? (async (enabled: boolean) => state(enabled))),
  };
  const entry = createRouterPortfolio(ports);
  document.body.replaceChildren(entry.settings);
  await vi.waitFor(() => expect(ports.read).toHaveBeenCalled());
  await vi.waitFor(() => expect(entry.settings.querySelector('.router-portfolio-counts')!.textContent)
    .toBe('4 个候选 · 1 个进行中 · 2 个已交付 · 1 个负结果'));
  const toggle = entry.settings.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  const summary = entry.settings.querySelector('summary') as HTMLElement;
  return { ports, entry, toggle, summary };
}

describe('Portfolio ownership (stop/pause switch)', () => {
  it('shows paused state, the ledger counts and a translated summary', async () => {
    const { entry, summary, toggle } = await setup();
    expect(toggle.checked).toBe(false);
    expect(summary.dataset.i18n).toBe('routerPortfolio.summaryOff');
    expect(entry.settings.querySelector('.router-portfolio-last')!.textContent).toBe('还没有启动过任何事项。');
    entry.destroy();
  });

  it('pausing and resuming posts the owner choice and adopts the server state', async () => {
    const { ports, entry, toggle, summary } = await setup();
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(summary.dataset.i18n).toBe('routerPortfolio.summaryOn'));
    expect(ports.setEnabled).toHaveBeenCalledWith(true);
    toggle.checked = false;
    toggle.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(summary.dataset.i18n).toBe('routerPortfolio.summaryOff'));
    expect(ports.setEnabled).toHaveBeenLastCalledWith(false);
    entry.destroy();
  });

  it('a failed change restores the server state and names the error', async () => {
    const { entry, toggle, summary } = await setup({ set: async () => { throw new Error('denied'); } });
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));
    const error = entry.settings.querySelector<HTMLElement>('.router-portfolio-error')!;
    await vi.waitFor(() => expect(error.hidden).toBe(false));
    expect(error.textContent).toBe('未更改：denied');
    expect(toggle.checked).toBe(false);
    expect(summary.dataset.i18n).toBe('routerPortfolio.summaryOff');
    entry.destroy();
  });

  it('no polling: one read on creation and another when the disclosure opens', async () => {
    const { ports, entry } = await setup();
    expect(ports.read).toHaveBeenCalledTimes(1);
    const details = entry.settings as HTMLDetailsElement;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(ports.read).toHaveBeenCalledTimes(2));
    entry.destroy();
  });
});
