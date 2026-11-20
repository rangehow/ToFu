// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/transport';
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import devicesPanel from '../../settings-panels/devices.html?raw';
import type { DefaultsView, WireNode } from '../devices/model';
import './devices-defaults';

const { request, toast } = vi.hoisted(() => ({ request: vi.fn(), toast: vi.fn() }));
vi.mock('../../api/transport', async (original) => ({ ...await original<typeof import('../../api/transport')>(), request }));
vi.mock('../../ui', async (original) => ({ ...await original<typeof import('../../ui')>(), toast }));

const populate = featureRegistry._populateDevicesTab as () => void;
const destroy = featureRegistry._destroyDevicesTab as () => void;
const defaultsPath = '/api/v4/devices/defaults';
const node = (id: string, kind: WireNode['kind'] = 'computer'): WireNode => ({
  node_id: id, name: id, kind, state: 'active', online: true, tiers: {}, credential: null,
});
const setDefaults = vi.fn<(defaults: DefaultsView['defaults']) => Promise<DefaultsView>>();
const readDefaults = vi.fn<(signal?: AbortSignal) => Promise<DefaultsView>>();
let saved: DefaultsView['defaults'];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function computerSelect(): HTMLSelectElement {
  const select = document.querySelector<HTMLSelectElement>('#devicesSettingsRoot select');
  expect(select).not.toBeNull();
  return select!;
}

function choose(select: HTMLSelectElement, value: string): void {
  select.value = value;
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

async function openSettings(): Promise<HTMLSelectElement> {
  populate();
  await vi.waitFor(computerSelect);
  return computerSelect();
}

beforeEach(() => {
  document.body.innerHTML = devicesPanel;
  saved = { computer: 'unlisted-computer', server: null, browser: null };
  setDefaults.mockReset().mockImplementation(async patch => {
    saved = { ...saved, ...patch };
    return { defaults: saved };
  });
  readDefaults.mockReset().mockImplementation(async () => ({ defaults: saved }));
  toast.mockClear();
  request.mockReset().mockImplementation(async (path: string, options: {
    method?: string; json?: DefaultsView['defaults']; signal?: AbortSignal;
  }) => {
    if (path === defaultsPath) return options.method === 'PUT' ? setDefaults(options.json!) : readDefaults(options.signal);
    if (path === '/api/v4/desktop/nodes') return { nodes: [node('laptop'), node('workstation'), node('test-server', 'server')] };
    if (path === '/api/v4/desktop/device-waits') return { waits: [] };
    if (path === '/api/v4/browser/status') return { clients: [] };
    throw new Error(`Unexpected API request: ${path}`);
  });
});

afterEach(() => {
  destroy();
  document.body.replaceChildren();
});

describe('Settings device defaults', () => {
  it('restores the last read value, including an unlisted default, and shows the API error', async () => {
    setDefaults.mockRejectedValueOnce(new ApiError('Device is no longer registered', { status: 404 }));
    const select = await openSettings();
    expect(select.value).toBe('unlisted-computer');

    choose(select, 'laptop');

    await vi.waitFor(() => expect(select.value).toBe('unlisted-computer'));
    expect(select.disabled).toBe(false);
    expect(document.querySelector('#devicesSettingsRoot [role="alert"]')?.textContent)
      .toBe(t('devicesPage.error.save', { message: 'Device is no longer registered' }));
    expect(toast).not.toHaveBeenCalledWith(t('settings.devices.saved'), { variant: 'success' });
  });

  it('locks a pending kind and uses the PUT acknowledgement as the rollback baseline', async () => {
    const put = deferred<DefaultsView>();
    setDefaults.mockReturnValueOnce(put.promise);
    const select = await openSettings();

    choose(select, 'laptop');
    expect(select.disabled).toBe(true);
    choose(select, 'workstation'); // Even a programmatic duplicate event cannot start a second write.
    expect(setDefaults.mock.calls).toEqual([[{ computer: 'laptop' }]]);

    put.resolve({ defaults: { ...saved, computer: 'acknowledged-computer' } });
    await vi.waitFor(() => expect(select.disabled).toBe(false));
    expect(select.value).toBe('acknowledged-computer');
    expect(toast).toHaveBeenCalledWith(t('settings.devices.saved'), { variant: 'success' });

    setDefaults.mockRejectedValueOnce(new ApiError('Write rejected', { status: 409 }));
    choose(select, '');
    await vi.waitFor(() => expect(select.value).toBe('acknowledged-computer'));
    expect(setDefaults).toHaveBeenLastCalledWith({ computer: null });
    expect(select.getAttribute('aria-invalid')).toBe('true');

    choose(select, '');
    await vi.waitFor(() => expect(select.disabled).toBe(false));
    expect(select.value).toBe('');
    expect(select.hasAttribute('aria-invalid')).toBe(false);
  });

  it('keeps an unconfirmed kind locked until the automatic GET establishes its current value', async () => {
    const select = await openSettings();
    const read = deferred<DefaultsView>();
    readDefaults.mockReturnValueOnce(read.promise);
    setDefaults.mockRejectedValueOnce(new ApiError('Outcome unknown', { status: 503 }));

    choose(select, 'laptop');
    await vi.waitFor(() => expect(readDefaults).toHaveBeenCalledTimes(2));
    expect(select.disabled).toBe(true);
    expect(select.selectedIndex).toBe(-1);
    choose(select, 'workstation');
    expect(setDefaults).toHaveBeenCalledTimes(1);

    read.resolve({ defaults: { ...saved, computer: 'authoritative-computer' } });
    await vi.waitFor(() => expect(select.disabled).toBe(false));
    expect(select.value).toBe('authoritative-computer');
    expect(select.hasAttribute('aria-invalid')).toBe(false);
    expect(toast).not.toHaveBeenCalled();
  });

  it('keeps saves for different kinds independent when acknowledgements arrive out of order', async () => {
    const computerPut = deferred<DefaultsView>();
    setDefaults.mockReturnValueOnce(computerPut.promise);
    const computer = await openSettings();
    const [, server] = document.querySelectorAll<HTMLSelectElement>('#devicesSettingsRoot select');
    const serverBeforeWrite = saved.server;

    choose(computer, 'laptop');
    expect(server.disabled).toBe(false);
    choose(server, 'test-server');
    await vi.waitFor(() => expect(server.disabled).toBe(false));

    computerPut.resolve({ defaults: { ...saved, computer: 'laptop', server: serverBeforeWrite } });
    await vi.waitFor(() => expect(computer.disabled).toBe(false));
    expect(server.value).toBe('test-server');
    expect(computer.value).toBe('laptop');
  });

  it('waits for an accepted save before reading on reload and does not act through retired controls', async () => {
    const put = deferred<DefaultsView>();
    setDefaults.mockReturnValueOnce(put.promise);
    const retiredSelect = await openSettings();
    choose(retiredSelect, 'laptop');

    populate();
    expect(document.querySelector('#devicesSettingsRoot select')).toBeNull();
    choose(retiredSelect, 'workstation');
    expect(setDefaults).toHaveBeenCalledTimes(1);
    expect(readDefaults).toHaveBeenCalledTimes(1);

    saved = { ...saved, computer: 'laptop' };
    put.resolve({ defaults: saved });
    await vi.waitFor(() => expect(computerSelect().value).toBe('laptop'));
    expect(computerSelect().disabled).toBe(false);
    expect(readDefaults).toHaveBeenCalledTimes(2);
    expect(toast).not.toHaveBeenCalled();
  });

  it('aborts a superseded read without interrupting the new visit', async () => {
    let aborted = false;
    readDefaults.mockImplementationOnce(signal => new Promise((_resolve, reject) => {
      signal!.addEventListener('abort', () => {
        aborted = true;
        reject(new DOMException('Aborted', 'AbortError'));
      }, { once: true });
    }));
    populate();

    const select = await openSettings();

    expect(aborted).toBe(true);
    expect(select.value).toBe('unlisted-computer');
  });

  it('does not start a deferred read for a retired visit or paint its rejection into a new opening', async () => {
    const put = deferred<DefaultsView>();
    setDefaults.mockReturnValueOnce(put.promise);
    const retiredSelect = await openSettings();
    choose(retiredSelect, 'laptop');
    populate();
    destroy();
    const fresh = openSettings();

    put.reject(new ApiError('Previous opening failed', { status: 409 }));
    const select = await fresh;
    expect(select.value).toBe('unlisted-computer');
    expect(readDefaults).toHaveBeenCalledTimes(2);
    expect(document.getElementById('devicesSettingsRoot')?.textContent).not.toContain('Previous opening failed');
    expect(toast).not.toHaveBeenCalled();
  });
});
