import '../styles/application/38-devices.css';
/** Lazy Devices workspace (`#/devices`): the fleet page, the device drawer (`#/devices/<key>`) and the
 * add-device wizard (`#/devices/add`). Only this adapter knows the application shell; the page reads the
 * shared live store. Entry point: _mountDevicesPage (the workspace loader in application.ts).
 * Dependencies: page-host mount lifetime, the devices feature modules.
 */
import { featureRegistry } from '../feature-registry';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { goToWorkspace, hasMountedWorkspace, mountWorkspacePage, showWorkspace } from '../application-shell/page-host';
import { mountDevicesPage } from './devices/page';
import { openDeviceDrawer } from './devices/drawer';
import { openAddDeviceWizard } from './devices/wizard';
import { ensureRelay } from './devices/relay';
import { getDevicesStore } from './devices/shell';

const TOPOLOGY_SEEN_KEY = 'tofu:devices:topology-seen';

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
  let current: { path: string; close: () => void } | null = null;
  // An overlay the owner closes returns the address to the list; one replaced by navigation does not.
  const route = (path: string) => {
    if (current?.path === path) return;
    const previous = current;
    current = null;
    previous?.close();
    const [, sub = '', ...rest] = path.split('/');
    if (!sub) return;
    const entry = { path, close: () => {} };
    const closed = () => { if (current === entry) { current = null; goToWorkspace('devices'); } };
    current = entry;
    if (sub === 'add') {
      const confirm = rest.length ? decodeURIComponent(rest.join('/')) : undefined;
      entry.close = openAddDeviceWizard(store, { pageBase, openBrowserExtension: openBrowserExtension() }, confirm, closed);
    } else {
      entry.close = openDeviceDrawer(store, decodeURIComponent([sub, ...rest].join('/')),
        { ensureRelay: () => ensureRelay() }, closed);
    }
  };
  const page = mountDevicesPage(root, store, {
    openDevice: key => goToWorkspace(`devices/${encodeURIComponent(key)}`),
    openWizard: nodeId => goToWorkspace(nodeId ? `devices/add/${encodeURIComponent(nodeId)}` : 'devices/add'),
    ensureRelay: () => ensureRelay(),
    readTopologySeen: () => { try { return localStorage.getItem(TOPOLOGY_SEEN_KEY) === '1'; } catch { return false; } },
    writeTopologySeen: () => { try { localStorage.setItem(TOPOLOGY_SEEN_KEY, '1'); } catch { /* private mode */ } },
  });
  mountWorkspacePage(() => {
    const previous = current;
    current = null;
    previous?.close();
    page.dispose();
  }, route);
  const path = window.location.hash.startsWith('#/') ? window.location.hash.slice(2) : '';
  if (path.startsWith('devices/')) route(path);
}
(featureRegistry as unknown as Record<string, unknown>)._mountDevicesPage = _mountDevicesPage;
export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('devices', name, args, stub);
}
