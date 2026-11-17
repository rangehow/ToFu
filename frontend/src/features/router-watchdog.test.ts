// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RouterWatchdogPorts } from './router-watchdog';
import type { RouterWatchdogState } from '../api/router.generated';

const capabilities = { assign: false as const, sendMessage: false as const, steerSubagent: true };
// Health checks are on by default (docs/ROUTER.md R7): an ensured Watchdog has its periodic review on.
const review = { enabled: true, schedule: '7,37 * * * *', taskId: 'system-1-router-watchdog-review' };
const available: RouterWatchdogState = { projectPath: '/data/router/1/.tofu-watchdog', conversationId: 'coordinator-watchdog',
  revision: 1, availability: 'available', capabilities, review };
const unbound: RouterWatchdogState = { ...available, conversationId: '', revision: 0, availability: 'unbound',
  review: { ...review, enabled: false, taskId: null } };
const catalog = {
  'routerWatchdog.entry': '故障巡检', 'routerWatchdog.summaryOff': '故障巡检：未开启',
  'routerWatchdog.summaryOn': '故障巡检：已开启', 'routerWatchdog.summaryFaultsOnly': '故障巡检：只在出故障时运行',
  'routerWatchdog.hint': '出故障时自动排查原因，并定期巡检。', 'routerWatchdog.open': '打开巡检会话',
  'routerWatchdog.headerSubtitle': '负责排查故障', 'routerWatchdog.back': '返回总负责人',
  'routerWatchdog.unavailable': '故障巡检不可用：{error}',
};
let release: () => void = () => {};

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
