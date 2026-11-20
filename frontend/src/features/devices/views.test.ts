// @vitest-environment jsdom
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/transport';
import { setLanguage, t } from '../../i18n';
import en from '../../i18n/locales/en.json';
import zh from '../../i18n/locales/zh.json';
import { featureRegistry } from '../../feature-registry';
import { jumpToSetting } from '../settings/setting-jump';
import { openBrowserAccessSettings } from './navigation';
import { openDeviceDrawer } from './drawer';
import { createDevicesStore, type DevicesSnapshot, type DevicesStore } from './live';
import { projectFleet, type DevicePermissions, type FleetSources, type PermissionsView, type Source, type Tiers } from './model';
import { mountDevicesPage } from './page';
import { openAddDeviceWizard } from './wizard';

const tiers = (on: Array<keyof Tiers>): Tiers => ({
  read: on.includes('read'), write: on.includes('write'), exec: on.includes('exec'), gui: on.includes('gui'),
  egress: on.includes('egress'), codex_login: on.includes('codex_login'),
});

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = () => {};
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
    permissions: { state: 'ok', value: { rev: 0, permissions: {
      mac: { node_id: 'mac', home: tiers(['read', 'write', 'gui']), device: tiers(['read', 'write', 'exec']),
        effective: tiers(['read', 'write']), rev: 0 },
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
    fleet: projectFleet(fleetSources), sources: fleetSources, loading: false, disconnected: false, ceilingReviews: new Map(),
    syncedAt: Date.UTC(2026, 10, 17, 10, 42), restartedAt: null, staleSources: [], ...overrides,
  };
  const listeners = new Set<(value: DevicesSnapshot) => void>();
  const client = {
    remove: vi.fn(async (_nodeId: string): Promise<void> => { throw new Error('removal refused'); }),
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

const ports = { backToChat: vi.fn(), openDevice: vi.fn(), openWizard: vi.fn(), ensureRelay: vi.fn() };

it.each([
  { language: 'en', catalog: en, title: 'Devices', home: 'This Home',
    fleet: 'Services registered with this Home', local: 'local to this Home',
    body: "Conversations, memory, credentials and approvals live here. This tab is the client interface. The entries below are service registrations with this Home, not separate physical computers. Each status describes that service's own connection.",
    states: ['online', 'offline', 'never connected', 'waiting for your confirmation', 'identity conflict'] },
  { language: 'zh', catalog: zh, title: '设备', home: '这个家',
    fleet: '向这个家注册的服务', local: '在这个家所在的服务器上',
    body: '对话、记忆、凭证和审批都保存在这里。当前标签页是客户端界面。下面的条目是向这个家注册的服务，不代表不同的实体电脑；每个状态只描述对应服务自己的连接。',
    states: ['在线', '离线', '从未连接', '等待你确认', '身份冲突'] },
])('distinguishes the viewer and independent service registrations in $language', async copy => {
  const network = vi.fn(async (url: string) => {
    if (!url.includes(`/${copy.language}.generated`)) throw new Error(`Unexpected fetch: ${url}`);
    return { ok: true, json: async () => copy.catalog };
  });
  vi.stubGlobal('fetch', network);
  await setLanguage(copy.language);
  const fleetSources = sources();
  const base = fleetSources.nodes.state === 'ok' ? fleetSources.nodes.value[0] : undefined;
  fleetSources.nodes = { state: 'ok', value: [
    { ...base!, node_id: 'a', name: 'same-name', route: 'direct' },
    { ...base!, node_id: 'b', name: 'same-name', online: false, route: 'direct' },
    { ...base!, node_id: 'never', online: false, last_seen: null },
    { ...base!, node_id: 'pending', online: false, state: 'pending' },
    { ...base!, node_id: 'conflict', state: 'conflict', attachment: 'process' },
  ] };
  const store = fakeStore({ sources: fleetSources, fleet: projectFleet(fleetSources) });
  const openDevice = vi.fn();
  const ensureRelay = vi.fn();
  const root = document.createElement('div');
  document.body.append(root);
  const page = mountDevicesPage(root, store, { ...ports, openDevice, ensureRelay });
  try {
    expect(root.querySelector('h1')?.textContent).toBe(copy.title);
    expect(root.querySelector('.devices-home__title')?.textContent).toBe(copy.home);
    expect(root.querySelector('.devices-home__body')?.textContent).toBe(copy.body);
    expect(root.querySelector('#devicesFleetTitle')?.textContent).toBe(copy.fleet);
    expect(root.querySelector('[data-device-key="node:conflict"] .devices-row__text')?.textContent).toContain(copy.local);
    const rows = [...root.querySelectorAll('.devices-row')];
    expect(rows.map(row => row.getAttribute('data-device-key')))
      .toEqual(['node:a', 'node:b', 'node:never', 'node:pending', 'node:conflict']);
    expect(rows.map(row => row.querySelector('.devices-status__main')?.textContent?.split(' · ')[0])).toEqual(copy.states);
    for (const row of rows) row.querySelector<HTMLButtonElement>('.devices-details-button')!.click();
    expect(openDevice.mock.calls).toEqual([['node:a'], ['node:b'], ['node:never'], ['node:pending'], ['node:conflict']]);
    expect(root.textContent).not.toMatch(/devicesPage\./);
    expect(ensureRelay).not.toHaveBeenCalled();
    expect(store.refresh).not.toHaveBeenCalled();
    expect(network).toHaveBeenCalledTimes(1);

    fleetSources.nodes.value = [fleetSources.nodes.value[1], { ...fleetSources.nodes.value[0], online: false }];
    store.publish({ sources: fleetSources, fleet: projectFleet(fleetSources) });
    const remaining = [...root.querySelectorAll('.devices-row')];
    expect(remaining.map(row => row.getAttribute('data-device-key'))).toEqual(['node:b', 'node:a']);
    expect(remaining.map(row => row.querySelector('.devices-status__main')?.textContent)).toEqual([copy.states[1], copy.states[1]]);
    remaining[0].querySelector<HTMLButtonElement>('.devices-details-button')!.click();
    expect(openDevice).toHaveBeenLastCalledWith('node:b');
  } finally {
    page.dispose();
    vi.unstubAllGlobals();
  }
});

it.each(['absent', 'failed', 'ok'] as const)('keeps %s inventory reads distinct from unknown and offline service states', state => {
  const fleetSources = sources();
  fleetSources.nodes = state === 'ok' ? { state, value: [] }
    : state === 'failed' ? { state, message: 'synthetic unreadable inventory' } : { state };
  fleetSources.browsers = state === 'ok' ? { state, value: [] }
    : state === 'failed' ? { state, message: 'synthetic unreadable inventory' } : { state };
  const store = fakeStore({ sources: fleetSources, fleet: projectFleet(fleetSources) });
  const root = document.createElement('div');
  document.body.append(root);
  const page = mountDevicesPage(root, store, ports);
  try {
    expect(root.querySelectorAll('.devices-row')).toHaveLength(0);
    expect(root.querySelector('.devices-dot--offline')).toBeNull();
    expect(store.snapshot().sources?.nodes.state).toBe(state);
    expect(root.textContent?.includes(t('devicesPage.empty.title'))).toBe(state === 'ok');
    if (state !== 'ok') expect(root.textContent).toContain(t('devicesPage.banner.failed', {
      sources: `${t('devicesPage.source.nodes')}, ${t('devicesPage.source.browsers')}`,
    }));
  } finally { page.dispose(); }
});

it('keeps an unavailable browser inventory separate from a known-empty computer filter', () => {
  const fleetSources = sources();
  if (fleetSources.nodes.state === 'ok') fleetSources.nodes.value = [fleetSources.nodes.value[1]];
  fleetSources.browsers = { state: 'absent' };
  const root = document.createElement('div');
  document.body.append(root);
  const page = mountDevicesPage(root, fakeStore({ sources: fleetSources, fleet: projectFleet(fleetSources) }), ports);
  const filter = (label: string) => {
    root.querySelector<HTMLButtonElement>('[data-focus-key="filter"]')!.click();
    [...root.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === label)!.click();
  };
  try {
    filter(t('devicesPage.kindShort.computer'));
    expect(root.textContent).toContain(t('devicesPage.empty.title'));
    filter(t('devicesPage.kindShort.browser'));
    expect(root.textContent).not.toContain(t('devicesPage.empty.title'));
    expect(root.textContent).toContain(t('devicesPage.banner.failed', { sources: t('devicesPage.source.browsers') }));
    filter(t('devicesPage.filter.all'));
    expect(root.querySelector('[data-device-key="node:gpu"]')).not.toBeNull();
  } finally { page.dispose(); }
});

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
  expect(ports.openWizard).toHaveBeenCalledWith('gpu', 'review:node:gpu');
  expect(root.querySelectorAll('[data-device-key="self"]')).toHaveLength(1);
  expect(root.querySelector('details')?.open).toBe(false);
});

it('preserves the return control and the device filter during a live refresh', () => {
  const root = document.createElement('div');
  document.body.append(root);
  const store = fakeStore();
  const mounted = mountDevicesPage(root, store, ports);
  const selector = '[aria-haspopup="menu"]';
  const filter = root.querySelector<HTMLButtonElement>(selector)!;
  filter.focus();
  filter.click();
  [...root.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    .find(button => button.textContent === t('devicesPage.kindShort.server'))!.click();
  expect(root.querySelector('[data-device-key="node:mac"]')).toBeNull();
  expect(root.querySelector('[data-device-key="node:gpu"]')).not.toBeNull();
  store.publish({ disconnected: true });
  expect(document.activeElement).toBe(root.querySelector(selector));
  expect(root.querySelector('[data-device-key="node:mac"]')).toBeNull();
  root.querySelector<HTMLButtonElement>('[data-focus-key="back"]')!.click();
  expect(ports.backToChat).toHaveBeenCalled();
  mounted.dispose();
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

async function ceilingFixture() {
  const fleetSources = sources();
  let home = tiers(['read', 'write', 'gui']);
  let rev = 1;
  const read = () => {
    const device = tiers(['read', 'write', 'exec']);
    const effective = { ...device, write: home.write && device.write, exec: home.exec && device.exec };
    return { node_id: 'mac', home, device, effective, rev };
  };
  const client = {
    nodes: async () => fleetSources.nodes,
    permissions: vi.fn(async (): Promise<Source<PermissionsView>> => ({ state: 'ok', value: { permissions: { mac: read() }, rev } })),
    defaults: async () => fleetSources.defaults,
    browsers: async () => fleetSources.browsers,
    waits: async () => fleetSources.waits,
    home: async () => fleetSources.home,
    confirm: vi.fn(async () => {}),
    rename: vi.fn(async () => {}),
    setDefault: vi.fn(async () => ({ defaults: { computer: 'mac', server: null, browser: null }, rev, execution: 'not_split' as const })),
    setCeiling: vi.fn(async (_id: string, _next: Partial<Tiers>, _expectedRevision: number): Promise<DevicePermissions> => { throw new Error('ceiling refused'); }),
  };
  const store = createDevicesStore(null, client as unknown as DevicesStore['client']);
  const close = openDeviceDrawer(store, 'node:mac', { ensureRelay: vi.fn() });
  await vi.waitFor(() => expect(store.snapshot().fleet).not.toBeNull());
  return { store, client, close, commit(next: Partial<Tiers>) {
    home = { ...tiers(['read']), ...next };
    rev += 1;
    return read();
  } };
}

function homeSwitch(index: number) {
  return document.querySelectorAll('dialog.devices-drawer-shell tbody tr')[index].querySelector<HTMLInputElement>('input')!;
}

it('writes the Home ceiling from the drawer and returns to the Home value when the write fails', async () => {
  const { store, client, close } = await ceilingFixture();
  const drawer = document.querySelector('dialog')!;
  // Rows follow the contract tier order: read, write, exec, …; the first switch of a row is this Home's.
  const exec = drawer.querySelectorAll('tbody tr')[2].querySelector<HTMLInputElement>('input')!;
  expect(exec.checked).toBe(false);
  exec.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(client.setCeiling).toHaveBeenCalledWith('mac', { write: true, exec: true, gui: true, egress: false, codex_login: false }, 1);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(client.permissions).toHaveBeenCalledTimes(2);
  const after = [...document.querySelectorAll('dialog tbody tr')][2].querySelector<HTMLInputElement>('input')!;
  expect(after.checked).toBe(false);
  expect(after.disabled).toBe(true);
  expect(store.snapshot().ceilingReviews.get('mac')?.message).toBe('ceiling refused');
  expect(drawer.querySelector('.ui-alert--error')?.textContent)
    .toBe(t('devicesPage.error.save', { message: 'ceiling refused' }));
  // The device's own switch is shown read-only, never as an operable control.
  expect(document.querySelectorAll('dialog tbody tr td:nth-child(3) input')).toHaveLength(0);
  expect(document.querySelectorAll('dialog tbody tr td:nth-child(3) .ui-badge')).toHaveLength(6);
  close();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

it('serializes two rapid reductions on successive revisions and ignores a pre-save read', async () => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  const second = deferred<DevicePermissions>();
  const staleRead = deferred<Source<PermissionsView>>();
  const oldPermissions = await client.permissions();
  client.permissions.mockImplementationOnce(() => staleRead.promise);
  const refreshing = store.refresh();
  client.setCeiling.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise);

  homeSwitch(1).click(); // turn off write
  homeSwitch(3).click(); // turn off GUI before the first PUT has replied
  await Promise.resolve();
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  expect(client.setCeiling).toHaveBeenNthCalledWith(1, 'mac', {
    write: false, exec: false, gui: true, egress: false, codex_login: false,
  }, 1);
  first.resolve(commit(client.setCeiling.mock.calls[0][1]));

  await vi.waitFor(() => expect(client.setCeiling).toHaveBeenCalledTimes(2));
  expect(client.setCeiling).toHaveBeenNthCalledWith(2, 'mac', {
    write: false, exec: false, gui: false, egress: false, codex_login: false,
  }, 2);
  second.resolve(commit(client.setCeiling.mock.calls[1][1]));
  await vi.waitFor(() => expect(homeSwitch(3).checked).toBe(false));
  expect(homeSwitch(1).checked).toBe(false);
  // A GET started before either write could otherwise repaint a stale ceiling.
  const publishedGui: Array<boolean | undefined> = [];
  const unsubscribe = store.subscribe(snapshot => {
    publishedGui.push(snapshot.fleet?.rows.find(row => row.nodeId === 'mac')?.permissions?.home.gui);
  });
  staleRead.resolve(oldPermissions);
  await refreshing;
  unsubscribe();
  expect(publishedGui.every(value => value === false)).toBe(true);
  expect(homeSwitch(1).checked).toBe(false);
  expect(homeSwitch(3).checked).toBe(false);
  close();
});

it('keeps a newer permissions read when an older ceiling save is acknowledged late', async () => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  client.setCeiling.mockImplementationOnce(() => first.promise).mockImplementationOnce(async (_id, next) => commit(next));
  const saving = store.setCeilingTier('mac', 'write', false, 1);
  await Promise.resolve();
  const lateAcknowledgement = commit(client.setCeiling.mock.calls[0][1]);
  commit({ ...lateAcknowledgement.home, gui: false }); // another writer reduced the ceiling
  await store.refresh();
  expect(homeSwitch(3).checked).toBe(false);

  const queued = store.setCeilingTier('mac', 'exec', true, 3);
  first.resolve(lateAcknowledgement);
  await Promise.all([saving, queued]);
  expect(client.setCeiling).toHaveBeenNthCalledWith(2, 'mac', {
    write: false, exec: true, gui: false, egress: false, codex_login: false,
  }, 3);
  expect(homeSwitch(3).checked).toBe(false);
  close();
});

it.each(['conflict', 'lost response'])('requires explicit review after a %s and only retains unsent intent', async failure => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  client.setCeiling.mockImplementationOnce(() => first.promise).mockImplementationOnce(async (_id, next) => commit(next));
  homeSwitch(2).click(); // sent grant: exec
  homeSwitch(4).click(); // unsent grant: egress
  await Promise.resolve();
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  if (failure === 'lost response') commit(client.setCeiling.mock.calls[0][1]);
  const latest = commit({ write: true }); // another owner mutation removes GUI (and any exec grant)
  first.reject(failure === 'conflict'
    ? new ApiError('Stale policy', { status: 409, code: 'stale_device_policy' })
    : new TypeError('Response lost'));

  await vi.waitFor(() => expect(store.snapshot().ceilingReviews.get('mac')?.unsent).toEqual({ egress: true }));
  expect(client.permissions).toHaveBeenCalledTimes(2);
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  expect(homeSwitch(2).checked).toBe(false);
  expect(homeSwitch(3).checked).toBe(false);
  const drawer = document.querySelector('dialog')!;
  expect(drawer.querySelector('.ui-alert--error')).not.toBeNull();
  close();
  const closeAgain = openDeviceDrawer(store, 'node:mac', { ensureRelay: vi.fn() });
  const confirm = [...document.querySelectorAll<HTMLButtonElement>('dialog button')]
    .find(button => button.textContent === t('devicesPage.wizard.confirm'))!;
  confirm.click();
  await vi.waitFor(() => expect(homeSwitch(4).checked).toBe(true));
  expect(client.setCeiling).toHaveBeenNthCalledWith(2, 'mac', {
    write: true, exec: false, gui: false, egress: true, codex_login: false,
  }, latest.rev);
  expect(store.snapshot().ceilingReviews.has('mac')).toBe(false);
  closeAgain();
});

it('does not review an unsent grant against a cached or failed reconciliation read', async () => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  const oldRead = deferred<Source<PermissionsView>>();
  const cached = store.snapshot().sources!.permissions;
  client.permissions.mockImplementationOnce(() => oldRead.promise)
    .mockResolvedValueOnce({ state: 'failed', message: 'unreachable' });
  const refreshing = store.refresh();
  client.setCeiling.mockImplementationOnce(() => first.promise).mockImplementationOnce(async (_id, next) => commit(next));
  homeSwitch(2).click();
  homeSwitch(4).click();
  await Promise.resolve();
  const latest = commit({ write: true });
  first.reject(new TypeError('Response lost'));
  await vi.waitFor(() => expect(store.snapshot().ceilingReviews.get('mac')?.reading).toBe(true));
  oldRead.resolve(cached);
  await refreshing;
  const confirm = () => [...document.querySelectorAll<HTMLButtonElement>('dialog button')]
    .find(button => button.textContent === t('devicesPage.wizard.confirm'))!;
  await vi.waitFor(() => expect(confirm().disabled).toBe(true));
  expect(store.snapshot().staleSources).toContain('permissions');
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  await store.refresh();
  expect(confirm().disabled).toBe(false);
  confirm().click();
  await vi.waitFor(() => expect(homeSwitch(4).checked).toBe(true));
  expect(client.setCeiling).toHaveBeenLastCalledWith('mac', {
    write: true, exec: false, gui: false, egress: true, codex_login: false,
  }, latest.rev);
  close();
});

it('keeps a rejected removal visible inside the open drawer and closes only after an acknowledged retry', async () => {
  const store = fakeStore();
  const onClosed = vi.fn();
  openDeviceDrawer(store, 'node:mac', { ensureRelay: vi.fn() }, onClosed);
  const drawer = document.querySelector<HTMLDialogElement>('dialog.devices-drawer-shell')!;
  const confirmRemoval = () => {
    drawer.querySelector<HTMLButtonElement>('.devices-drawer__actions .ui-btn--danger')!.click();
    document.querySelector<HTMLButtonElement>('dialog:not(.devices-drawer-shell) .ui-dialog__footer .ui-btn--danger')!.click();
  };
  confirmRemoval();
  await vi.waitFor(() => expect(drawer.querySelector('.ui-alert--error')?.textContent)
    .toBe(t('devicesPage.error.save', { message: 'removal refused' })));
  expect(store.client.remove).toHaveBeenCalledWith('mac');
  expect(drawer.open).toBe(true);
  expect(onClosed).not.toHaveBeenCalled();
  expect(document.querySelector('.ui-toast--success')).toBeNull();

  store.client.remove.mockImplementationOnce(async () => {});
  confirmRemoval();
  await vi.waitFor(() => expect(onClosed).toHaveBeenCalledTimes(1));
  expect(document.querySelector('.ui-toast--success')).not.toBeNull();
});


const wizardButton = (key: 'confirm' | 'done') => [...document.querySelectorAll<HTMLButtonElement>('.devices-wizard-shell button')]
  .find(item => item.textContent === t(`devicesPage.wizard.${key}`))!;

it.each(['ACK first', 'GET first', 'newer owner revision'])('handles queued Read Only with %s', async ordering => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  client.setCeiling.mockImplementationOnce(() => first.promise).mockImplementationOnce(async (_id, next) => commit(next));
  const grant = store.setCeilingTier('mac', 'exec', true, 1);
  await vi.waitFor(() => expect(client.setCeiling).toHaveBeenCalledTimes(1));
  const onClosed = vi.fn();
  const closeWizard = openAddDeviceWizard(store, { pageBase: () => 'https://test.invalid' }, 'mac', onClosed);
  wizardButton('confirm').click();
  await vi.waitFor(() => expect(wizardButton('done').disabled).toBe(false));
  wizardButton('done').click();
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  const ack = commit(client.setCeiling.mock.calls[0][1]);
  if (ordering === 'newer owner revision') commit(ack.home);
  if (ordering !== 'ACK first') await store.refresh();
  first.resolve(ack);
  await grant;
  if (ordering === 'newer owner revision') {
    await vi.waitFor(() => expect(store.snapshot().ceilingReviews.has('mac')).toBe(true));
    expect(client.setCeiling).toHaveBeenCalledTimes(1);
    expect(onClosed).not.toHaveBeenCalled();
    closeWizard();
    close();
    return;
  }
  await vi.waitFor(() => expect(onClosed).toHaveBeenCalledTimes(1));
  expect(client.setCeiling).toHaveBeenNthCalledWith(2, 'mac', {
    write: false, exec: false, gui: false, egress: false, codex_login: false,
  }, 2);
  expect(homeSwitch(2).checked).toBe(false);
  close();
});

it('does not finish the wizard or run follow-up writes if newer permissions superseded its acknowledgement', async () => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  client.setCeiling.mockImplementationOnce(() => first.promise);
  const onClosed = vi.fn();
  const closeWizard = openAddDeviceWizard(store, { pageBase: () => 'https://test.invalid' }, 'mac', onClosed);
  wizardButton('confirm').click();
  await vi.waitFor(() => expect(wizardButton('done').disabled).toBe(false));
  const name = document.querySelector<HTMLInputElement>('.devices-wizard-shell input[type="text"]')!;
  name.value = 'renamed';
  name.dispatchEvent(new Event('input'));
  document.querySelector<HTMLInputElement>('.devices-wizard-shell input[type="checkbox"]')!.click();
  wizardButton('done').click();
  await vi.waitFor(() => expect(client.setCeiling).toHaveBeenCalledTimes(1));
  const ack = commit(client.setCeiling.mock.calls[0][1]);
  const newer = commit({ exec: true });
  await store.refresh();
  const published = vi.fn((snapshot: DevicesSnapshot) => snapshot.fleet?.rows.find(row => row.nodeId === 'mac')?.permissions);
  const release = store.subscribe(published);
  first.resolve(ack);
  await vi.waitFor(() => expect(document.querySelector('.devices-wizard-shell .ui-alert--error')).not.toBeNull());
  expect(published.mock.results.every(result => result.value?.rev === newer.rev && result.value.home.exec)).toBe(true);
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  expect(client.rename).not.toHaveBeenCalled();
  expect(client.setDefault).not.toHaveBeenCalled();
  expect(onClosed).not.toHaveBeenCalled();
  release();
  closeWizard();
  close();
});

it.each(['conflict', 'lost response'])('keeps queued Read Only after %s and replaces it only on explicit Done', async failure => {
  const { store, client, close, commit } = await ceilingFixture();
  const first = deferred<DevicePermissions>();
  client.setCeiling.mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(async (_id, next) => commit(next));
  homeSwitch(2).click();
  await vi.waitFor(() => expect(client.setCeiling).toHaveBeenCalledTimes(1));
  close();

  const closeWizard = openAddDeviceWizard(store, { pageBase: () => 'https://test.invalid' }, 'mac');
  wizardButton('confirm').click();
  await vi.waitFor(() => expect(wizardButton('done').disabled).toBe(false));
  wizardButton('done').click(); // Default Read Only queues behind the first save.
  // Either another writer granted exec or the first write committed with no response.
  const reconciled = commit(client.setCeiling.mock.calls[0][1]);
  first.reject(failure === 'conflict'
    ? new ApiError('Stale policy', { status: 409, code: 'stale_device_policy' })
    : new TypeError('Response lost'));
  await vi.waitFor(() => expect(store.snapshot().ceilingReviews.get('mac')?.unsent)
    .toEqual({ write: false, exec: false, gui: false, egress: false, codex_login: false }));
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  await vi.waitFor(() => expect(document.querySelector('.devices-wizard-shell .ui-alert--error')).not.toBeNull());
  const review = store.snapshot().ceilingReviews.get('mac')!;
  const latest = commit({ ...reconciled.home, egress: true });
  await store.refresh();
  expect(store.snapshot().ceilingReviews.get('mac')?.unsent.egress).toBe(false);
  await expect(store.setCeiling('mac', { gui: false }, reconciled.rev, review)).rejects.toThrow();
  document.querySelector<HTMLInputElement>('.devices-wizard-shell input[value="developer"]')!.click();
  expect(store.snapshot().ceilingReviews.get('mac')).toBe(review);
  expect(client.setCeiling).toHaveBeenCalledTimes(1);
  expect(wizardButton('done').disabled).toBe(false);
  wizardButton('done').click();
  await vi.waitFor(() => expect(client.setCeiling).toHaveBeenCalledTimes(2));
  expect(client.setCeiling).toHaveBeenLastCalledWith('mac', {
    write: true, exec: true, gui: false, egress: false, codex_login: false,
  }, latest.rev);
  expect(store.snapshot().ceilingReviews.has('mac')).toBe(false);
  closeWizard();
});

function browserFixture() {
  const fleetSources = sources();
  fleetSources.browsers = { state: 'ok', value: [
    { client_id: 'owner-work', name: 'Chrome', profile: 'Work', capabilities: ['read', 'click'], last_poll: 1 },
    { client_id: 'owner-personal', name: 'Chrome', profile: 'Personal', capabilities: ['read'], last_poll: 99 },
  ] };
  const store = fakeStore({ sources: fleetSources, fleet: projectFleet(fleetSources) });
  return { store, fleetSources };
}

it.each([1360, 390])('opens unfiltered canonical access for the selected browser at %ipx without writes or commands', async width => {
  vi.stubGlobal('innerWidth', width);
  const { store } = browserFixture();
  const network = vi.fn(() => { throw new Error('No network allowed'); });
  vi.stubGlobal('fetch', network);
  const ensureRelay = vi.fn();
  const bridge = featureRegistry as unknown as Record<string, unknown>;
  const oldOpen = bridge.openSettings;
  const oldSwitch = bridge.switchSettingsTab;
  const open = vi.fn(() => {
    expect(document.querySelector<HTMLDialogElement>('dialog.devices-drawer-shell')?.open).toBe(false);
    const modal = document.createElement('div');
    modal.id = 'settingsModal';
    modal.className = 'open';
    modal.innerHTML = '<input value="unsaved"><details><summary>Access</summary><div id="browserAccessList">Work grant · Personal grant</div></details>';
    document.body.append(modal);
  });
  const activate = vi.fn();
  bridge.openSettings = open;
  bridge.switchSettingsTab = activate;
  const resolveAction = vi.fn(() => jumpToSetting);
  const invokeFeature = vi.fn(async (_name: string, args: readonly unknown[]) => jumpToSetting(args[0]));
  vi.stubGlobal('TofuModules', { resolveAction, invokeFeature });
  const scroll = vi.fn();
  vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(scroll);
  const close = openDeviceDrawer(store, 'browser:owner-work', { ensureRelay, openBrowserAccess: openBrowserAccessSettings });
  try {
    const drawer = document.querySelector('dialog.devices-drawer-shell')!;
    expect(drawer.textContent).toContain(t('devicesPage.drawer.browserBody'));
    expect(drawer.textContent).toContain('Chrome · Work · owner-work · Work');
    expect(drawer.textContent).not.toContain(t('devicesPage.drawer.caps'));
    expect(drawer.querySelectorAll('.devices-caps input, .devices-caps [role="switch"]')).toHaveLength(0);
    [...drawer.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent === t('settings.browserConnection'))!.click();
    await vi.waitFor(() => expect(document.querySelector('.ui-toast')?.textContent).toContain('owner-work'));
    expect(resolveAction).toHaveBeenCalledWith('jumpToSetting');
    expect(invokeFeature).toHaveBeenCalledWith('jumpToSetting', ['search'], jumpToSetting);
    expect(activate).toHaveBeenCalledWith('search');
    expect(open).toHaveBeenCalledOnce();
    expect(document.querySelector('details')?.open).toBe(true);
    expect(document.activeElement?.id).toBe('browserAccessList');
    expect(document.activeElement?.textContent).toContain('Personal grant');
    expect(document.querySelector('.ui-toast')?.textContent).not.toContain('Personal');
    // Re-entering uses the existing Settings owner without replacing its unsaved editor.
    await openBrowserAccessSettings();
    expect(open).toHaveBeenCalledOnce();
    expect(document.querySelector<HTMLInputElement>('#settingsModal input')?.value).toBe('unsaved');
    expect(network).not.toHaveBeenCalled();
    expect(ensureRelay).not.toHaveBeenCalled();
    expect(store.refresh).not.toHaveBeenCalled();
    expect(store.client.remove).not.toHaveBeenCalled();
  } finally {
    close();
    bridge.openSettings = oldOpen;
    bridge.switchSettingsTab = oldSwitch;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  }
});

it.each(['gone', 'profile-changed', 'stale', 'disconnected', 'ambiguous'])(
  'never navigates to another browser when the selected browser is %s', async state => {
    const { store, fleetSources } = browserFixture();
    const openBrowserAccess = vi.fn();
    const close = openDeviceDrawer(store, 'browser:owner-work', { ensureRelay: vi.fn(), openBrowserAccess });
    const button = [...document.querySelectorAll<HTMLButtonElement>('dialog button')]
      .find(candidate => candidate.textContent === t('settings.browserConnection'))!;
    const clients = fleetSources.browsers.state === 'ok' ? fleetSources.browsers.value : [];
    if (state === 'gone') clients.shift();
    if (state === 'profile-changed') clients[0] = { ...clients[0], profile: 'Other' };
    if (state === 'ambiguous') clients.push({ ...clients[0] });
    store.publish({ sources: fleetSources, fleet: projectFleet(fleetSources),
      staleSources: state === 'stale' ? ['browsers'] : [], disconnected: state === 'disconnected' });
    // A retired button reference must not act after the live repaint either.
    button.click();
    await Promise.resolve();
    expect(openBrowserAccess).not.toHaveBeenCalled();
    expect(store.refresh).not.toHaveBeenCalled();
    if (state === 'gone') expect(document.querySelector('dialog')?.textContent).toContain(t('devicesPage.drawer.gone'));
    close();
  });

it('reports unavailable navigation instead of guessing an alternative destination', async () => {
  vi.stubGlobal('TofuModules', { resolveAction: () => undefined });
  try {
    await expect(openBrowserAccessSettings()).rejects.toThrow('Settings navigation unavailable');
  } finally { vi.unstubAllGlobals(); }
});

it('reports the exact selected browser unavailable if it disappears while Settings is loading', async () => {
  const { store, fleetSources } = browserFixture();
  let ready!: () => void;
  const opening = new Promise<void>(resolve => { ready = resolve; });
  const openBrowserAccess = vi.fn(() => opening);
  const close = openDeviceDrawer(store, 'browser:owner-work', { ensureRelay: vi.fn(), openBrowserAccess });
  [...document.querySelectorAll<HTMLButtonElement>('dialog button')]
    .find(button => button.textContent === t('settings.browserConnection'))!.click();
  fleetSources.browsers = { state: 'ok', value: [{ client_id: 'owner-personal', profile: 'Personal' }] };
  store.publish({ sources: fleetSources, fleet: projectFleet(fleetSources) });
  ready();
  await vi.waitFor(() => expect(document.querySelector('.ui-toast')?.textContent).toContain(t('settings.browserStatusUnavailable')));
  expect(document.querySelector('.ui-toast')?.textContent).toContain('owner-work');
  expect(document.querySelector('.ui-toast')?.textContent).not.toContain('Personal');
  expect(store.refresh).not.toHaveBeenCalled();
  close();
});
