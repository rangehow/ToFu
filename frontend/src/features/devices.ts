import '../styles/application/38-devices.css';
/** Lazy Devices workspace (`#/devices`): the fleet page, the device drawer (`#/devices/<key>`) and the
 * add-device wizard (`#/devices/add`). Only this adapter knows the application shell; the page reads the
 * shared live store. Entry point: _mountDevicesPage (the workspace loader in application.ts).
 * Dependencies: page-host mount lifetime, the devices feature modules.
 */
import { featureRegistry } from '../feature-registry';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import {
  goToWorkspace, hasMountedWorkspace, mountWorkspacePage, showWorkspace, workspacePath,
} from '../application-shell/page-host';
import { mountDevicesPage } from './devices/page';
import { openDeviceDrawer } from './devices/drawer';
import { openAddDeviceWizard } from './devices/wizard';
import { closeDevicesOverlay, openDevicesOverlay, openBrowserAccessSettings } from './devices/navigation';
import { ensureRelay } from './devices/relay';
import { getDevicesStore } from './devices/shell';

function pageBase(): string {
  const origin = window.location.origin;
  if (!/^https?:\/\//.test(origin)) return '';
  return origin + window.location.pathname.replace(/\/(?:index\.html)?$/, '');
}

function openBrowserExtension(): (() => void) | undefined {
  const open = window.TofuModules?.resolveAction('openBrowserExtensionModal');
  return typeof open === 'function' ? () => { void open(); } : undefined;
}

export function _mountDevicesPage(): void {
  if (hasMountedWorkspace('devices')) { showWorkspace('devices'); return; }
  const legacy = featureRegistry as unknown as Record<string, any>;
  if (legacy.paperMode) legacy.exitPaperMode?.();
  if (legacy.imageGenMode) legacy.exitImageGenMode?.();
  showWorkspace('devices');
  const root = document.getElementById('devicesPageContainer');
  if (!root) throw new Error('Devices page container is missing');
  const store = getDevicesStore();
  let current: { path: string; close: () => void; restoreFocus: boolean } | null = null;
  let opener: { path: string; focusKey: string } | undefined;
  const openChild = (path: string, focusKey: string) => {
    opener = { path, focusKey };
    openDevicesOverlay(path);
  };
  // Only owner dismissal returns the address to the list; route-driven teardown never writes history.
  const route = (path: string) => {
    if (current?.path === path) return;
    const previous = current;
    current = null;
    previous?.close();
    const [, sub = '', ...rest] = path.split('/');
    if (!sub) {
      if (previous?.restoreFocus) page.focus(opener?.path === previous.path ? opener.focusKey : 'back');
      return;
    }
    const entry = { path, close: () => {}, restoreFocus: true };
    const closed = () => { if (current === entry) closeDevicesOverlay(path, route); };
    current = entry;
    if (sub === 'add') {
      const confirm = rest.length ? decodeURIComponent(rest.join('/')) : undefined;
      entry.close = openAddDeviceWizard(store, { pageBase, openBrowserExtension: openBrowserExtension() }, confirm, closed);
    } else {
      entry.close = openDeviceDrawer(store, decodeURIComponent([sub, ...rest].join('/')),
        {
          ensureRelay: () => ensureRelay(),
          openBrowserAccess: () => {
            // Parent history traversal can arrive after Settings has already taken focus.
            entry.restoreFocus = false;
            return openBrowserAccessSettings();
          },
        }, closed);
    }
  };
  const page = mountDevicesPage(root, store, {
    backToChat: () => goToWorkspace('chat'),
    openDevice: key => openChild(`devices/${encodeURIComponent(key)}`, `open:${key}`),
    openWizard: (nodeId, focusKey) => openChild(nodeId ? `devices/add/${encodeURIComponent(nodeId)}` : 'devices/add', focusKey),
    ensureRelay: () => ensureRelay(),
  });
  mountWorkspacePage(() => {
    const previous = current;
    current = null;
    previous?.close();
    page.dispose();
  }, route);
  const path = workspacePath();
  if (path.startsWith('devices/') && path !== 'devices/') route(path);
  else page.focus('back');
}
(featureRegistry as unknown as Record<string, unknown>)._mountDevicesPage = _mountDevicesPage;
export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('devices', name, args, stub);
}
