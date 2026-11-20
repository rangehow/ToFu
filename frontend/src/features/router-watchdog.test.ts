// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RouterWatchdogPorts } from './router-watchdog';
import type { RouterWatchdogState } from '../api/router.generated';
import { ApiError } from '../api/errors';

const capabilities = { assign: false as const, sendMessage: false as const, steerSubagent: true };
// Health checks are on by default (docs/ROUTER.md R7): an ensured Watchdog has its periodic review on.
const review = { enabled: true, schedule: '7,37 * * * *', taskId: 'system-1-router-watchdog-review' };
const available: RouterWatchdogState = { projectPath: '/data/router/1/.tofu-watchdog', conversationId: 'coordinator-watchdog',
  revision: 1, availability: 'available', capabilities, review };
const unbound: RouterWatchdogState = { ...available, conversationId: '', revision: 0, availability: 'unbound',
  review: { ...review, enabled: false, taskId: null } };
const catalog = {
  'common.loading': '加载中…', 'rich.unknown': '未确认', 'err.k.permission.chip': '权限不足',
  'routerWatchdog.entry': '故障巡检', 'routerWatchdog.summaryOff': '故障巡检：未开启',
  'routerWatchdog.summaryOn': '故障巡检：已开启', 'routerWatchdog.summaryFaultsOnly': '故障巡检：只在出故障时运行',
  'routerWatchdog.hint': '出故障时自动排查原因，并定期巡检。', 'routerWatchdog.open': '打开巡检会话',
  'routerWatchdog.headerSubtitle': '负责排查故障', 'routerWatchdog.back': '返回总负责人',
  'routerWatchdog.unavailable': '故障巡检不可用：{error}',
};
let release: () => void = () => {};
const cleanup: Array<() => void> = [];

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  const arrived = new Promise<void>((resolve) => { release = resolve; });
  vi.stubGlobal('fetch', vi.fn(async () => {
    await arrived;
    return { ok: true, status: 200, json: async () => catalog };
  }));
});

afterEach(() => {
  cleanup.splice(0).forEach(destroy => destroy());
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function setup(read: RouterWatchdogState | Error | (() => Promise<RouterWatchdogState>),
  options: { ensure?: RouterWatchdogState | Error | (() => Promise<RouterWatchdogState>) } = {}) {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('../i18n');
  const { createRouterWatchdogEntry } = await import('./router-watchdog');
  let active = '';
  const answer = (value: RouterWatchdogState | Error | (() => Promise<RouterWatchdogState>)) => (
    typeof value === 'function' ? value() : value instanceof Error ? Promise.reject(value) : Promise.resolve(value));
  const ports: RouterWatchdogPorts = {
    read: vi.fn(() => answer(read)),
    ensure: vi.fn(() => answer(options.ensure ?? available)),
    open: vi.fn((id: string) => { active = id; }),
    back: vi.fn(),
    activeConversationId: () => active,
    notifyError: vi.fn(),
    changed: vi.fn(),
  };
  const header = document.createElement('div');
  document.body.replaceChildren(header);
  const entry = createRouterWatchdogEntry(ports, { header });
  cleanup.push(() => entry.destroy());
  // The main coordinator's header hosts the state line; nothing goes to the sidebar.
  document.body.append(entry.settings);
  const settings = entry.settings;
  const view = header.querySelector('header')!;
  const status = settings.querySelector<HTMLElement>('summary')!;
  const open = settings.querySelector<HTMLButtonElement>('button')!;
  return { i18n, ports, entry, settings, view, status, open, setActive: (id: string) => { active = id; } };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

async function translated(i18n: { ready(): Promise<void> }) {
  const ready = i18n.ready();
  release();
  await ready;
}

describe('Router Watchdog (health checks)', () => {
  it('is a translated fold with its state, one sentence and a way into its chat: nothing to switch', async () => {
    const { i18n, settings, view, status, open } = await setup(available);
    // Painted from the passive read before the catalog arrived: the lookup returns its key.
    expect(status.textContent).toBe('routerWatchdog.summaryOn');
    await translated(i18n);
    await settle();
    expect(settings.tagName).toBe('DETAILS');
    expect(status.className).toBe('ui-disclosure');
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(settings.querySelector('.router-watchdog-body p')!.textContent).toBe('出故障时自动排查原因，并定期巡检。');
    expect(open.textContent).toBe('打开巡检会话');
    expect(open.className).toContain('ui-btn');
    expect(settings.querySelectorAll('input, select, textarea')).toHaveLength(0);
    expect(view.textContent).toContain('负责排查故障');
    expect(view.querySelector('button')!.textContent).toBe('返回总负责人');
  });

  it('names a Watchdog whose periodic checks the owner stopped, and one that is not there', async () => {
    const faultsOnly = await setup({ ...available, review: { ...review, enabled: false } });
    await translated(faultsOnly.i18n);
    await settle();
    expect(faultsOnly.status.textContent).toBe('故障巡检：只在出故障时运行');
    const off = await setup(unbound);
    await translated(off.i18n);
    await settle();
    expect(off.status.textContent).toBe('故障巡检：未开启');
    // The path is known from the passive read already, so presence can tell its conversation apart.
    expect(off.entry.location()).toBe('/data/router/1/.tofu-watchdog');
    expect(off.entry.conversationId()).toBe('');
  });

  it('opens a known Watchdog at once and marks only its conversation, with a way back', async () => {
    const { i18n, ports, entry, view, open, setActive } = await setup(available);
    await translated(i18n);
    await settle();
    expect(view.hidden).toBe(true);
    open.click();
    expect(ports.ensure).not.toHaveBeenCalled();
    expect(ports.open).toHaveBeenCalledWith('coordinator-watchdog');
    entry.refresh();
    expect(view.hidden).toBe(false);
    view.querySelector('button')!.click();
    expect(ports.back).toHaveBeenCalledTimes(1);
    setActive('some-other-chat'); entry.refresh();
    expect(view.hidden).toBe(true);
  });

  it('turns health checks on before opening an unknown Watchdog, once per click', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, status, open } = await setup(unbound,
      { ensure: () => new Promise((resolve) => { finish = resolve; }) });
    await translated(i18n);
    await settle();
    expect(ports.changed).toHaveBeenCalledTimes(1);
    open.click(); open.click();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    expect(open.disabled).toBe(true);
    finish(available);
    await settle();
    expect(ports.open).toHaveBeenCalledWith('coordinator-watchdog');
    expect(open.disabled).toBe(false);
    expect(status.textContent).toBe('故障巡检：已开启');
    // A new conversation identity is announced so lists can stop showing it.
    expect(ports.changed).toHaveBeenCalledTimes(2);
    expect(entry.conversationId()).toBe('coordinator-watchdog');
  });

  it('does not pull the owner back when the ensure answers after they moved on', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, open, setActive } = await setup(unbound,
      { ensure: () => new Promise((resolve) => { finish = resolve; }) });
    await translated(i18n);
    await settle();
    setActive('coordinator-router');
    open.click();
    setActive('another-chat');
    finish(available);
    await settle();
    expect(ports.open).not.toHaveBeenCalled();
  });

  it('reports a refused ensure and lets the owner try again', async () => {
    const { i18n, ports, open } = await setup(new Error('offline'), { ensure: new Error('home not writable') });
    await translated(i18n);
    open.click();
    await settle();
    expect(ports.notifyError).toHaveBeenCalledWith('故障巡检不可用：home not writable');
    expect(ports.open).not.toHaveBeenCalled();
    expect(open.disabled).toBe(false);
  });

  it('keeps an initial read failure unconfirmed without automatically ensuring a Watchdog', async () => {
    const { i18n, ports, entry, status, settings, open } = await setup(new Error('offline'));
    await translated(i18n); await settle();
    expect(status.textContent).toContain('未确认');
    expect(settings.querySelector('[role="alert"]')?.textContent).toContain('offline');
    expect(open.hidden).toBe(false);
    expect(open.disabled).toBe(false);
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.ensure).not.toHaveBeenCalled();
    entry.destroy();
  });

  it('reads again on reload, since ensuring the main coordinator turns health checks on', async () => {
    let state = unbound;
    const { i18n, ports, entry, status } = await setup(async () => state);
    await translated(i18n);
    await settle();
    expect(status.textContent).toBe('故障巡检：未开启');
    state = available;
    entry.reload();
    await settle();
    expect(ports.read).toHaveBeenCalledTimes(2);
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(entry.conversationId()).toBe('coordinator-watchdog');
  });

  it.each([
    { label: 'transport failure', error: new Error('Passive refresh unavailable'), shown: '未确认' },
    { label: 'explicit denial', error: new ApiError('Request failed', { status: 403, code: 'forbidden' }), shown: '权限不足' },
  ])('withdraws a previously available action after $label and restores only fresh availability', async ({ error, shown }) => {
    const { i18n, ports, entry, status, settings, view, open, setActive } = await setup(available);
    await translated(i18n);
    await settle();
    setActive(available.conversationId); entry.refresh();
    expect(view.hidden).toBe(false);
    let reject!: (error: Error) => void;
    vi.mocked(ports.read).mockReturnValueOnce(new Promise((_resolve, no) => { reject = no; }));
    entry.reload();
    expect(status.textContent).toContain('加载中…');
    expect(status.dataset.i18n).not.toBe('routerWatchdog.summaryOn');
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    expect(view.hidden).toBe(true);
    expect(entry.conversationId()).toBe(available.conversationId);
    reject(error);
    await settle();
    expect(status.textContent).toContain(shown);
    expect(settings.querySelector('[role="alert"]')?.textContent).toContain(error.message);
    window.dispatchEvent(new Event('tofu:language-change'));
    expect(status.textContent).toContain(shown);
    open.click();
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.ensure).not.toHaveBeenCalled();

    const fresh = { ...available, conversationId: 'fresh-watchdog', revision: 2 };
    vi.mocked(ports.read).mockResolvedValueOnce(fresh);
    entry.reload(); await settle();
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(settings.querySelector<HTMLElement>('[role="alert"]')!.hidden).toBe(true);
    expect(open.hidden).toBe(false);
    expect(open.disabled).toBe(false);
    open.click();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
    expect(ports.ensure).not.toHaveBeenCalled();
    entry.destroy();
  });

  it('withdraws an explicitly unavailable binding without calling it off, then restores fresh faults-only state', async () => {
    const { i18n, ports, entry, status, settings, open } = await setup(available);
    await translated(i18n);
    await settle();
    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, revision: 2, availability: 'unavailable' });
    entry.reload(); await settle();
    expect(status.textContent).toContain('未确认');
    expect(status.dataset.i18n).not.toBe('routerWatchdog.summaryOff');
    expect(settings.querySelector('[role="alert"]')?.textContent).toContain('unavailable');
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    expect(entry.conversationId()).toBe('');
    open.click();
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.ensure).not.toHaveBeenCalled();

    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, revision: 3, review: { ...review, enabled: false } });
    entry.reload(); await settle();
    expect(status.textContent).toBe('故障巡检：只在出故障时运行');
    expect(open.disabled).toBe(false);
    expect(open.hidden).toBe(false);
    expect(ports.ensure).not.toHaveBeenCalled();
    entry.destroy();
  });

  it('keeps an unavailable binding withdrawn through pending and failed refreshes until authoritative restore', async () => {
    const { i18n, ports, entry, status, open } = await setup(available);
    await translated(i18n); await settle();
    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, revision: 2, availability: 'unavailable' });
    entry.reload(); await settle();
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);

    for (const httpStatus of [503, 403]) {
      let reject!: (error: Error) => void;
      vi.mocked(ports.read).mockReturnValueOnce(new Promise((_resolve, no) => { reject = no; }));
      entry.reload();
      expect(open.hidden).toBe(true);
      expect(open.disabled).toBe(true);
      open.click();
      expect(ports.open).not.toHaveBeenCalled();
      expect(ports.ensure).not.toHaveBeenCalled();
      reject(new ApiError('Request failed', { status: httpStatus, code: httpStatus === 403 ? 'forbidden' : 'service_unavailable' }));
      await settle();
      expect(open.hidden).toBe(true);
      expect(open.disabled).toBe(true);
      open.click();
      expect(ports.open).not.toHaveBeenCalled();
      expect(ports.ensure).not.toHaveBeenCalled();
    }

    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, revision: 3, conversationId: 'fresh-watchdog' });
    entry.reload(); await settle();
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(open.hidden).toBe(false);
    expect(open.disabled).toBe(false);
    open.click();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
    expect(ports.ensure).not.toHaveBeenCalled();
  });

  it('distinguishes identically worded typed denial and unavailable read evidence', async () => {
    const { i18n, ports, entry, status, settings, open } = await setup(available);
    await translated(i18n); await settle();
    vi.mocked(ports.read).mockRejectedValueOnce(new ApiError('Request failed', { status: 503, code: 'service_unavailable' }));
    entry.reload(); await settle();
    expect(status.textContent).toContain('未确认');
    const error = settings.querySelector<HTMLElement>('[role="alert"]')!;
    expect(error.textContent).toContain('Request failed');
    expect(error.textContent).toContain('503');
    expect(error.textContent).toContain('service_unavailable');
    expect(error.textContent).not.toContain('权限不足');

    vi.mocked(ports.read).mockRejectedValueOnce(new ApiError('Request failed', { status: 403, code: 'forbidden' }));
    entry.reload(); await settle();
    expect(status.textContent).toContain('权限不足');
    expect(status.textContent).not.toContain('未确认');
    expect(error.textContent).toContain('Request failed');
    expect(error.textContent).toContain('403');
    expect(error.textContent).toContain('forbidden');
    expect(error.textContent).not.toContain('service_unavailable');
    window.dispatchEvent(new Event('tofu:language-change'));
    expect(status.textContent).toContain('权限不足');
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    open.click();
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.ensure).not.toHaveBeenCalled();
  });

  it.each([
    { status: 403, code: null, shown: '权限不足' },
    { status: 0, code: 'forbidden', shown: '权限不足' },
    { status: 0, code: null, shown: '未确认' },
  ])('uses structured status $status / code $code rather than message text to present a read denial', async ({ status: httpStatus, code, shown }) => {
    const { i18n, ports, entry, status, open } = await setup(available);
    await translated(i18n); await settle();
    vi.mocked(ports.read).mockRejectedValueOnce(new ApiError('forbidden', { status: httpStatus, code }));
    entry.reload(); await settle();
    expect(status.textContent).toContain(shown);
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    expect(ports.ensure).not.toHaveBeenCalled();
  });

  it('keeps an initially denied read withdrawn through a failed refresh, but accepts fresh unbound evidence', async () => {
    const { i18n, ports, entry, status, open } = await setup(new ApiError('Request failed', { status: 403, code: 'forbidden' }));
    await translated(i18n); await settle();
    expect(status.textContent).toContain('权限不足');
    expect(open.hidden).toBe(true);
    vi.mocked(ports.read).mockRejectedValueOnce(new ApiError('Request failed', { status: 503, code: 'service_unavailable' }));
    entry.reload();
    expect(open.disabled).toBe(true);
    await settle();
    expect(status.textContent).toContain('未确认');
    expect(open.hidden).toBe(true);
    open.click();
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.ensure).not.toHaveBeenCalled();
    vi.mocked(ports.read).mockResolvedValueOnce(unbound);
    entry.reload(); await settle();
    expect(status.textContent).toBe('故障巡检：未开启');
    expect(open.hidden).toBe(false);
    expect(open.disabled).toBe(false);
    expect(ports.ensure).not.toHaveBeenCalled();
  });

  it('lets another surface ensure the Watchdog and adopt its binding without opening it', async () => {
    const { i18n, ports, entry, status } = await setup(unbound);
    await translated(i18n);
    await settle();
    await expect(entry.ensure()).resolves.toBe('coordinator-watchdog');
    expect(ports.open).not.toHaveBeenCalled();
    expect(entry.conversationId()).toBe('coordinator-watchdog');
    expect(status.textContent).toBe('故障巡检：已开启');
  });

  it('rejects an ensure from another surface with the localized reason', async () => {
    const refused = await setup(unbound, { ensure: new Error('home not writable') });
    await translated(refused.i18n);
    await expect(refused.entry.ensure()).rejects.toThrow('故障巡检不可用：home not writable');
    const unavailable = await setup(unbound, { ensure: unbound });
    await translated(unavailable.i18n);
    await expect(unavailable.entry.ensure()).rejects.toThrow('故障巡检不可用：unbound');
    expect(unavailable.ports.open).not.toHaveBeenCalled();
  });

  it('does not let an older ensure restore or open a binding after a newer read withdraws it', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, status, settings, open } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    open.click();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, revision: 2, availability: 'unavailable' });
    entry.reload(); await settle();
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    finish({ ...available, conversationId: 'old-watchdog', revision: 1 });
    await settle();
    expect(status.textContent).toContain('未确认');
    expect(settings.querySelector('[role="alert"]')?.textContent).toContain('unavailable');
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    expect(entry.conversationId()).toBe('');
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.notifyError).not.toHaveBeenCalled();

    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, conversationId: 'fresh-watchdog', revision: 3 });
    entry.reload(); await settle();
    expect(open.hidden).toBe(false);
    expect(open.disabled).toBe(false);
    open.click();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
    expect(ports.ensure).toHaveBeenCalledTimes(1);
  });

  it('rejects a superseded external ensure instead of returning its stale navigation identity', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, status } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    const result = entry.ensure().then(id => ({ id }), error => ({ error }));
    vi.mocked(ports.read).mockRejectedValueOnce(new ApiError('Request failed', { status: 403, code: 'forbidden' }));
    entry.reload(); await settle();
    finish(available);
    expect(await result).toMatchObject({ error: { name: 'AbortError' } });
    expect(status.textContent).toContain('权限不足');
    expect(ports.open).not.toHaveBeenCalled();
  });

  it('discards an older ensure while a newer passive read is still pending', async () => {
    let finish!: (state: RouterWatchdogState) => void, answer!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, status, open } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    open.click();
    vi.mocked(ports.read).mockReturnValueOnce(new Promise(resolve => { answer = resolve; }));
    entry.reload();
    finish(available); await settle();
    expect(status.textContent).toContain('加载中…');
    expect(entry.conversationId()).toBe('');
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.notifyError).not.toHaveBeenCalled();
    answer({ ...available, conversationId: 'fresh-watchdog', revision: 2 }); await settle();
    expect(status.textContent).toBe('故障巡检：已开启');
    open.click();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
  });

  it('suppresses queued navigation if a refresh starts after ensure adoption but before the click continuation', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, status, open } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    open.click();
    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, revision: 2, availability: 'unavailable' });
    finish(available);
    queueMicrotask(() => entry.reload());
    await settle();
    expect(status.textContent).toContain('未确认');
    expect(open.hidden).toBe(true);
    expect(open.disabled).toBe(true);
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.notifyError).not.toHaveBeenCalled();
  });

  it('lets a newer ensure supersede an older ensure without returning an old ID', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    const old = entry.ensure().then(id => ({ id }), error => ({ error }));
    vi.mocked(ports.ensure).mockResolvedValueOnce({ ...available, conversationId: 'fresh-watchdog', revision: 2 });
    await expect(entry.ensure()).resolves.toBe('fresh-watchdog');
    finish(available);
    expect(await old).toMatchObject({ error: { name: 'AbortError' } });
    expect(entry.conversationId()).toBe('fresh-watchdog');
    expect(ports.open).not.toHaveBeenCalled();
  });

  it('keeps a normal ensure authoritative over an older rejected read', async () => {
    let reject!: (error: Error) => void;
    const { i18n, ports, entry, status, open } = await setup(() => new Promise((_resolve, no) => { reject = no; }));
    await translated(i18n); await settle();
    open.click(); await settle();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith(available.conversationId);
    reject(new ApiError('Request failed', { status: 403, code: 'forbidden' })); await settle();
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(open.hidden).toBe(false);
    expect(open.disabled).toBe(false);
    expect(entry.conversationId()).toBe(available.conversationId);
  });

  it('rejects an ensure settled after destroy without returning a navigation ID', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    const result = entry.ensure().then(id => ({ id }), error => ({ error }));
    entry.destroy(); finish(available);
    expect(await result).toMatchObject({ error: { name: 'AbortError' } });
    entry.reload();
    await expect(entry.ensure()).rejects.toMatchObject({ name: 'AbortError' });
    expect(ports.read).toHaveBeenCalledTimes(1);
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    expect(ports.open).not.toHaveBeenCalled();
  });

  it('restores fresh Open without waiting for a superseded click ensure to finish', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, status, open } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finish = resolve; }) });
    await translated(i18n); await settle();
    open.click();
    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, conversationId: 'fresh-watchdog', revision: 2 });
    entry.reload(); await settle();
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(open.disabled).toBe(false);
    expect(open.hasAttribute('aria-busy')).toBe(false);
    open.click();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
    finish(available); await settle();
    expect(entry.conversationId()).toBe('fresh-watchdog');
    expect(ports.open).toHaveBeenCalledTimes(1);
  });

  it('does not let a superseded click finalizer release a newer click ensure', async () => {
    let finishOld!: (state: RouterWatchdogState) => void, finishNew!: (state: RouterWatchdogState) => void;
    const { i18n, ports, entry, open } = await setup(unbound,
      { ensure: () => new Promise(resolve => { finishOld = resolve; }) });
    await translated(i18n); await settle();
    open.click();
    vi.mocked(ports.read).mockResolvedValueOnce({ ...unbound, revision: 2 });
    entry.reload(); await settle();
    expect(open.disabled).toBe(false);
    vi.mocked(ports.ensure).mockReturnValueOnce(new Promise(resolve => { finishNew = resolve; }));
    open.click();
    expect(ports.ensure).toHaveBeenCalledTimes(2);
    finishOld(available); await settle();
    expect(open.disabled).toBe(true);
    expect(open.getAttribute('aria-busy')).toBe('true');
    expect(ports.open).not.toHaveBeenCalled();
    finishNew({ ...available, conversationId: 'fresh-watchdog', revision: 3 }); await settle();
    expect(open.disabled).toBe(false);
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
  });

  it('ends loading when an external ensure interrupts a read and then fails', async () => {
    let rejectRead!: (error: Error) => void;
    const { i18n, ports, entry, status } = await setup(() => new Promise((_resolve, reject) => { rejectRead = reject; }),
      { ensure: new Error('home not writable') });
    await translated(i18n); await settle();
    expect(status.textContent).toContain('加载中…');
    await expect(entry.ensure()).rejects.toThrow('故障巡检不可用：home not writable');
    expect(status.textContent).not.toContain('加载中…');
    expect(status.textContent).toContain('未确认');
    rejectRead(new Error('aborted passive read')); await settle();
    expect(status.textContent).toContain('未确认');
    expect(ports.open).not.toHaveBeenCalled();
  });

  it('does not let an older rejected read overwrite a newer authoritative restore', async () => {
    const { i18n, ports, entry, status, open } = await setup(available);
    await translated(i18n); await settle();
    let reject!: (error: Error) => void;
    vi.mocked(ports.read).mockReturnValueOnce(new Promise((_resolve, no) => { reject = no; }));
    entry.reload();
    vi.mocked(ports.read).mockResolvedValueOnce({ ...available, conversationId: 'fresh-watchdog', revision: 2 });
    entry.reload(); await settle();
    reject(new Error('outdated read failure')); await settle();
    expect(status.textContent).toBe('故障巡检：已开启');
    expect(open.disabled).toBe(false);
    open.click();
    expect(ports.open).toHaveBeenCalledExactlyOnceWith('fresh-watchdog');
    expect(ports.ensure).not.toHaveBeenCalled();
    entry.destroy();
  });

  it('repaints its header on every sidebar refresh without a DOM write', async () => {
    const { entry, view, setActive } = await setup(available);
    await settle();
    const observer = new MutationObserver(() => undefined);
    observer.observe(view, { subtree: true, childList: true, attributes: true, characterData: true });
    for (let event = 0; event < 5; event += 1) entry.refresh();
    expect(observer.takeRecords()).toEqual([]);
    setActive('coordinator-watchdog');
    entry.refresh(); entry.refresh();
    expect(observer.takeRecords().map(record => record.attributeName)).toEqual(['hidden']);
    observer.disconnect();
  });

  it('cleans up on destroy', async () => {
    const { entry, settings, view } = await setup(available);
    await settle();
    entry.destroy();
    expect(settings.isConnected).toBe(false);
    expect(view.isConnected).toBe(false);
  });
});
