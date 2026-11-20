// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { installWorkspaceNavigation, mountWorkspacePage, showWorkspace } from '../../application-shell/page-host';
import { closeDevicesOverlay, openDevicesOverlay } from './navigation';

let uninstall: (() => void) | undefined;
afterEach(() => {
  showWorkspace('chat', false);
  uninstall?.();
});

function openHost(address: string) {
  history.replaceState(null, '', address);
  const onRoute = vi.fn();
  uninstall = installWorkspaceNavigation(async page => {
    showWorkspace(page, false);
    mountWorkspacePage(() => {}, onRoute);
  }, error => { throw error; });
  return onRoute;
}

it('dismisses an in-app child by returning to its parent entry, not adding a duplicate', async () => {
  const onRoute = openHost('/proxy/15000/?example=1#/devices');
  const length = history.length;
  openDevicesOverlay('devices/add');
  expect(location.hash).toBe('#/devices/add');
  expect(history.length).toBe(length + 1);
  closeDevicesOverlay('devices/add', onRoute);
  await vi.waitFor(() => expect(location.hash).toBe('#/devices'));
  expect(location.pathname + location.search).toBe('/proxy/15000/?example=1');
  expect(history.length).toBe(length + 1);
  expect(onRoute).toHaveBeenLastCalledWith('devices');
});

it('dismisses a direct link in place and refuses a late close from another route', () => {
  const onRoute = openHost('/proxy/15000/?example=1#/devices/node%3Awork');
  const length = history.length;
  closeDevicesOverlay('devices/add', onRoute);
  expect(location.hash).toBe('#/devices/node%3Awork');
  closeDevicesOverlay('devices/node%3Awork', onRoute);
  expect(location.hash).toBe('#/devices');
  expect(history.length).toBe(length);
  expect(onRoute).toHaveBeenLastCalledWith('devices');
});

it('awaits the lazy Settings owner before revealing its existing browser-access section', async () => {
  const { openBrowserAccessSettings } = await import('./navigation');
  const jump = vi.fn();
  let ready!: () => void;
  const loaded = new Promise<void>(resolve => { ready = resolve; });
  const invokeFeature = vi.fn(async () => {
    await loaded;
    document.body.innerHTML = '<details><summary>Access</summary><div id="browserAccessList"></div></details>';
  });
  vi.stubGlobal('TofuModules', { resolveAction: () => jump, invokeFeature });
  const scroll = vi.fn();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
  try {
    const navigation = openBrowserAccessSettings();
    expect(document.getElementById('browserAccessList')).toBeNull();
    ready();
    await navigation;
    expect(invokeFeature).toHaveBeenCalledWith('jumpToSetting', ['search'], jump);
    expect(jump).not.toHaveBeenCalled();
    expect(document.querySelector('details')?.open).toBe(true);
    expect(document.activeElement?.id).toBe('browserAccessList');
  } finally {
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  }
});
