// @vitest-environment jsdom
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { openDeviceDrawer } from './drawer';
import type { DevicesSnapshot, DevicesStore } from './live';
import { projectFleet, type FleetSources, type Tiers } from './model';
import { mountDevicesPage } from './page';

const tiers = (on: Array<keyof Tiers>): Tiers => ({
  read: on.includes('read'), write: on.includes('write'), exec: on.includes('exec'), gui: on.includes('gui'),
  egress: on.includes('egress'), codex_login: on.includes('codex_login'),
});

beforeAll(() => {
  // Platform dialog semantics jsdom lacks.
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) { this.open = true; };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement, result?: string) {
    if (!this.open) return;
    this.open = false;
    this.returnValue = result ?? '';
    this.dispatchEvent(new Event('close'));
  };
  const scope = globalThis as unknown as { CSS?: { escape?: (value: string) => string } };
  scope.CSS = { ...(scope.CSS || {}), escape: scope.CSS?.escape || ((value: string) => value) };
});

afterEach(() => { document.body.replaceChildren(); });

function sources(): FleetSources {
  return {
    nodes: { state: 'ok', value: [
      { node_id: 'mac', name: 'ruan-macbook', online: true, last_seen: 1, kind: 'computer', route: 'browser_relay',
        tiers: tiers(['read', 'write', 'exec']), credential: { id: 'k', name: 'mac' }, rtt_ms: 38, version: '2026.11.15' },
      { node_id: 'gpu', name: 'gpu-a100-07', online: false, last_seen: 1, kind: 'server', state: 'pending',
        fingerprint: '7F3A-91C2', tiers: tiers(['read']), credential: { id: 'k2', name: 'gpu' } },
    ] },
    permissions: { state: 'ok', value: { permissions: {
      mac: { node_id: 'mac', home: tiers(['read', 'write', 'gui']), device: tiers(['read', 'write', 'exec']),
        effective: tiers(['read', 'write']) },
    } } },
    defaults: { state: 'ok', value: { defaults: { computer: 'mac' } } },
    browsers: { state: 'ok', value: [] },
    waits: { state: 'ok', value: [] },
    home: { state: 'ok', value: { host: 'codelab-main', version: '0.19.2', storage: 'tofudb', bootedAt: 0 } },
  };
}

function fakeStore(overrides: Partial<DevicesSnapshot> = {}) {
  const fleetSources = sources();
  let snapshot: DevicesSnapshot = {
    fleet: projectFleet(fleetSources), sources: fleetSources, loading: false, disconnected: false,
    syncedAt: Date.UTC(2026, 10, 17, 10, 42), restartedAt: null, staleSources: [], ...overrides,
  };
  const listeners = new Set<(value: DevicesSnapshot) => void>();
  const client = {
    setCeiling: vi.fn(async () => { throw new Error('ceiling refused'); }),
    setDefault: vi.fn(async () => ({ defaults: {} })),
  };
  const store = {
    client,
    snapshot: () => snapshot,
    subscribe(listener: (value: DevicesSnapshot) => void) { listeners.add(listener); listener(snapshot); return () => listeners.delete(listener); },
    refresh: vi.fn(async () => { listeners.forEach(listener => listener(snapshot)); }),
    publish(next: Partial<DevicesSnapshot>) { snapshot = { ...snapshot, ...next }; listeners.forEach(listener => listener(snapshot)); },
  };
  return store as unknown as DevicesStore & typeof store;
}

const ports = { openDevice: vi.fn(), openWizard: vi.fn(), ensureRelay: vi.fn(), readTopologySeen: () => true };

it('lists each device with health, route and two-ended capability chips', () => {
  const root = document.createElement('div');
  document.body.append(root);
  mountDevicesPage(root, fakeStore(), ports);
  const mac = root.querySelector('[data-device-key="node:mac"]')!;
  expect(mac.textContent).toContain('ruan-macbook');
  expect(mac.querySelector('.devices-row__title .ui-badge')).not.toBeNull();
  const chips = [...mac.querySelectorAll('.devices-chip')].map(chip => chip.className);
  expect(chips.some(name => name.includes('ui-badge--success'))).toBe(true);
  expect(chips.some(name => name.includes('devices-chip--off'))).toBe(true);
  expect(chips.some(name => name.includes('ui-badge--warning'))).toBe(true);
  const pending = root.querySelector('[data-device-key="node:gpu"]')!;
  pending.querySelector<HTMLButtonElement>('[data-focus-key="review:node:gpu"]')!.click();
  expect(ports.openWizard).toHaveBeenCalledWith('gpu');
  expect(root.querySelector('[data-device-key="self"]')).not.toBeNull();
});

it('says honestly when the Home cannot be reached and keeps the last state', () => {
  const root = document.createElement('div');
  document.body.append(root);
  const store = fakeStore();
  mountDevicesPage(root, store, ports);
  expect(root.querySelector('.ui-alert--warning')).toBeNull();
  store.publish({ disconnected: true });
  expect(root.querySelector('.ui-alert--warning')).not.toBeNull();
  expect(root.querySelector('.devices-dot--stale')).not.toBeNull();
  expect(root.querySelector('[data-device-key="node:mac"]')).not.toBeNull();
});

it('writes the Home ceiling from the drawer and returns to the Home value when the write fails', async () => {
  const store = fakeStore();
  openDeviceDrawer(store, 'node:mac', { ensureRelay: vi.fn() });
  const drawer = document.querySelector('dialog')!;
  // Rows follow the contract tier order: read, write, exec, …; the first switch of a row is this Home's.
  const exec = drawer.querySelectorAll('tbody tr')[2].querySelector<HTMLInputElement>('input')!;
  expect(exec.checked).toBe(false);
  exec.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(store.client.setCeiling).toHaveBeenCalledWith('mac', { write: true, exec: true, gui: true, egress: false, codex_login: false });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(store.refresh).toHaveBeenCalled();
  const after = [...document.querySelectorAll('dialog tbody tr')][2].querySelector<HTMLInputElement>('input')!;
  expect(after.checked).toBe(false);
  // The device's own switch is shown read-only, never as an operable control.
  expect(document.querySelectorAll('dialog tbody tr td:nth-child(3) input')).toHaveLength(0);
  expect(document.querySelectorAll('dialog tbody tr td:nth-child(3) .ui-badge')).toHaveLength(6);
});
