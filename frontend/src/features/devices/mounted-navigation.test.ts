// @vitest-environment jsdom
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { installWorkspaceNavigation, showWorkspace } from '../../application-shell/page-host';
import { jumpToSetting } from '../settings/setting-jump';
import { projectFleet, type FleetSources } from './model';
import type { DevicesSnapshot, DevicesStore } from './live';

const fixture = vi.hoisted(() => ({ store: null as DevicesStore | null }));
vi.mock('./shell', () => ({ getDevicesStore: () => fixture.store }));
vi.mock('../../runtime-bridge', () => ({ invokeFeatureEntry: vi.fn() }));
vi.mock('./relay', () => ({ ensureRelay: vi.fn(), subscribeRelay: () => () => {} }));
import { _mountDevicesPage } from '../devices';

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event('close'));
  };
  HTMLElement.prototype.scrollIntoView = () => {};
  Object.defineProperty(globalThis, 'CSS', { configurable: true, value: { escape: (value: string) => value.replaceAll(':', '\\:') } });
});

let uninstall: (() => void) | undefined;
afterEach(() => {
  uninstall?.();
  uninstall = undefined;
  showWorkspace('chat', false);
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mountedFixture() {
  const sources: FleetSources = {
    nodes: { state: 'ok', value: [] }, permissions: { state: 'absent' }, defaults: { state: 'absent' },
    waits: { state: 'ok', value: [] }, home: { state: 'ok', value: { host: 'synthetic-home' } },
    browsers: { state: 'ok', value: [
      { client_id: 'owner-work', name: 'Chrome', profile: 'Work', capabilities: ['read'], last_poll: 1 },
      { client_id: 'owner-personal', name: 'Chrome', profile: 'Personal', capabilities: ['read', 'click'], last_poll: 99 },
    ] },
  };
  const snapshot = { sources, fleet: projectFleet(sources), loading: false, disconnected: false,
    syncedAt: 1, restartedAt: null, staleSources: [], ceilingReviews: new Map() };
  const mutation = vi.fn(() => { throw new Error('No mutations or commands'); });
  fixture.store = { snapshot: () => snapshot, subscribe: (listener: (value: DevicesSnapshot) => void) => { listener(snapshot); return () => {}; },
    refresh: mutation, setCeilingTier: mutation, setCeiling: mutation, confirmCeilingReview: mutation,
    discardCeilingReview: mutation, client: new Proxy({}, { get: () => mutation }) } as unknown as DevicesStore;
  document.body.innerHTML = '<div id="devicesPageContainer"></div><div id="settingsModal" class="open"><input value="unsaved"><details><summary>Access</summary><div id="browserAccessList">Work grant · Personal grant</div></details></div>';
  history.replaceState(null, '', '/proxy/fixture/?scope=owner#/devices');
  const openSettings = vi.fn();
  const activate = vi.fn();
  const bridge = featureRegistry as unknown as Record<string, unknown>;
  const oldOpen = bridge.openSettings, oldSwitch = bridge.switchSettingsTab;
  bridge.openSettings = openSettings;
  bridge.switchSettingsTab = activate;
  vi.stubGlobal('fetch', mutation);
  vi.stubGlobal('TofuModules', { resolveAction: () => jumpToSetting,
    invokeFeature: async (_name: string, args: readonly unknown[]) => jumpToSetting(args[0]) });
  showWorkspace('devices', false);
  uninstall = installWorkspaceNavigation(async () => _mountDevicesPage(), error => { throw error; });
  _mountDevicesPage();
  const back = vi.spyOn(history, 'back').mockImplementation(() => {});
  return { mutation, openSettings, activate, back, restore: () => {
    bridge.openSettings = oldOpen; bridge.switchSettingsTab = oldSwitch;
  }, deliverBack: () => {
    history.replaceState(null, '', '#/devices');
    window.dispatchEvent(new PopStateEvent('popstate'));
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } };
}

it.each([1360, 390])('keeps warm Settings focus after delayed mounted drawer history traversal (%i synthetic width)', async width => {
  vi.stubGlobal('innerWidth', width);
  const fixture = mountedFixture();
  try {
    const opener = document.querySelector<HTMLButtonElement>('[data-focus-key="open:browser:owner-work"]')!;
    opener.focus();
    opener.click();
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    const drawer = document.querySelector<HTMLDialogElement>('dialog.devices-drawer-shell')!;
    expect(drawer.open).toBe(true);
    expect(drawer.textContent).toContain('Chrome · Work · owner-work · Work');
    [...drawer.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === t('settings.browserConnection'))!.click();
    await vi.waitFor(() => expect(fixture.back).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(document.activeElement?.id).toBe('browserAccessList'));
    expect(fixture.activate).toHaveBeenCalledWith('search');
    fixture.deliverBack();
    await Promise.resolve();
    expect(document.activeElement?.id).toBe('browserAccessList');
    expect(document.querySelector('.ui-toast')?.textContent).toContain('owner-work');
    expect(document.querySelector('.ui-toast')?.textContent).not.toContain('Personal');
    expect(document.querySelector<HTMLDetailsElement>('#settingsModal details')?.open).toBe(true);
    expect(document.getElementById('browserAccessList')?.textContent).toContain('Personal grant');
    expect(document.querySelector<HTMLInputElement>('#settingsModal input')?.value).toBe('unsaved');
    expect(fixture.openSettings).not.toHaveBeenCalled();
    expect(fixture.mutation).not.toHaveBeenCalled();
    expect(location.pathname + location.search).toBe('/proxy/fixture/?scope=owner');
    expect(location.hash).toBe('#/devices');
    // Focus ownership resets for the next mounted drawer, not the whole Devices page.
    opener.focus(); opener.click();
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    const next = document.querySelector<HTMLDialogElement>('dialog.devices-drawer-shell')!;
    next.querySelector<HTMLButtonElement>('.ui-dialog__header button')!.focus();
    next.querySelector<HTMLButtonElement>('.ui-dialog__header button')!.click();
    await vi.waitFor(() => expect(fixture.back).toHaveBeenCalledTimes(2));
    fixture.deliverBack();
    expect(document.activeElement).toBe(opener);
  } finally { fixture.restore(); }
});

it('restores the exact list opener after ordinary drawer dismissal and delayed history traversal', async () => {
  const fixture = mountedFixture();
  try {
    const opener = document.querySelector<HTMLButtonElement>('[data-focus-key="open:browser:owner-work"]')!;
    opener.focus(); opener.click();
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    const drawer = document.querySelector<HTMLDialogElement>('dialog.devices-drawer-shell')!;
    drawer.querySelector<HTMLButtonElement>('.ui-dialog__header button')!.focus();
    drawer.querySelector<HTMLButtonElement>('.ui-dialog__header button')!.click();
    await vi.waitFor(() => expect(fixture.back).toHaveBeenCalledOnce());
    fixture.deliverBack();
    expect(document.activeElement).toBe(opener);
    expect(fixture.activate).not.toHaveBeenCalled();
    expect(fixture.mutation).not.toHaveBeenCalled();
  } finally { fixture.restore(); }
});

it('keeps Settings focus when mounted history traversal settles before its async navigation', async () => {
  const fixture = mountedFixture();
  let ready!: () => void;
  const loaded = new Promise<void>(resolve => { ready = resolve; });
  vi.stubGlobal('TofuModules', { resolveAction: () => jumpToSetting,
    invokeFeature: async (_name: string, args: readonly unknown[]) => { await loaded; jumpToSetting(args[0]); } });
  try {
    const opener = document.querySelector<HTMLButtonElement>('[data-focus-key="open:browser:owner-work"]')!;
    opener.focus(); opener.click();
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    const drawer = document.querySelector<HTMLDialogElement>('dialog.devices-drawer-shell')!;
    [...drawer.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === t('settings.browserConnection'))!.click();
    await vi.waitFor(() => expect(fixture.back).toHaveBeenCalledOnce());
    fixture.deliverBack();
    ready();
    await vi.waitFor(() => expect(document.activeElement?.id).toBe('browserAccessList'));
    expect(fixture.mutation).not.toHaveBeenCalled();
  } finally { ready(); fixture.restore(); }
});
