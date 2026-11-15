import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';
import { createButton } from '../../ui/button';

interface ShareRoot {
  name?: string;
  path?: string;
}

type Tier = 'read' | 'write' | 'exec' | 'gui' | 'egress' | 'codex_login';

/** One node (`Node` in contracts/desktop_node_v1.schema.json). */
interface DesktopNode {
  node_id?: string | null;
  name?: string;
  online?: boolean;
  platform?: string;
  share_roots?: ShareRoot[];
  tiers?: Partial<Record<Tier, boolean>>;
  credential?: { id?: string; name?: string | null } | null;
  attachment?: 'paired' | 'process';
}

interface NodeListing {
  nodes?: DesktopNode[];
}

/** One pending await_device registration (contracts/device_waits_v1.schema.json publicWait). */
interface DeviceWait {
  waitId?: string;
  conversationId?: string;
  reason?: string;
  createdAt?: number;
  device?: { selector?: string; agentId?: string | null; name?: string | null };
}

interface DeviceWaitPage {
  waits?: DeviceWait[];
  nextCursor?: string | null;
}

interface DesktopApi {
  nodes(): Promise<NodeListing | null>;
  removeNode(nodeId: string): Promise<unknown>;
  revokeToken(keyId: string): Promise<unknown>;
  deviceWaits(cursor?: string): Promise<DeviceWaitPage | null>;
  cancelDeviceWait(waitId: string): Promise<unknown>;
}

type DevicesWindow = Window & {
  Api?: { desktop?: DesktopApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  showToast?: (message: string) => void;
  closeSettings?: () => void;
  openLocalControlModal?: () => void;
  _populateDevicesTab?: () => void;
  _renderDevicesLoadFailed?: (error?: unknown) => void;
  _renderDeviceNodes?: (nodes: readonly DesktopNode[]) => void;
  _destroyDevicesTab?: () => void;
};

/** Contract order of `x-permission-tiers`. */
const TIER_LABELS: readonly (readonly [Tier, I18nKey])[] = [
  ['read', 'local.tierRead'], ['write', 'local.tierWrite'],
  ['exec', 'local.tierExec'], ['gui', 'local.tierGui'],
  ['egress', 'local.tierEgress'], ['codex_login', 'local.tierCodexLogin'],
];

let loadGeneration = 0;
let nodeScope: LifecycleScope | null = null;
let waitScope: LifecycleScope | null = null;

function globals(): DevicesWindow {
  return featureRegistry as unknown as DevicesWindow;
}

function desktopApi(): DesktopApi {
  const api = globals().Api?.desktop;
  if (!api) throw new Error('Desktop API is not ready');
  return api;
}

function translate(key: I18nKey): string {
  return globals().t?.(key) || key;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className = '',
  text = '',
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function nodesLane(): HTMLElement | null {
  const lane = document.getElementById('devicesAgentsList');
  return lane instanceof HTMLElement ? lane : null;
}

function loadingLine(): HTMLElement {
  return element('p', 'stg-loading', translate('settings.loading'));
}

/** The waits lane is optional: older fragments carry only agents and tokens. */
function waitsLane(): HTMLElement | null {
  const lane = document.getElementById('devicesWaitsList');
  return lane instanceof HTMLElement ? lane : null;
}

function loadFailedBox(error?: unknown): HTMLElement {
  let detail = '';
  if (error instanceof Error) detail = error.message;
  else if (error && typeof error === 'object' && 'message' in error) {
    detail = String((error as { message?: unknown }).message ?? '');
  }
  const box = element('div', 'devices-load-failed');
  box.append(element('span', 'devices-load-failed-icon', '⚠'));
  const text = element('span', '', translate('devices.loadFailed'));
  if (detail) {
    text.append(' ', element('span', 'devices-load-failed-detail', detail));
  }
  box.append(text);
  return box;
}

function formatCreatedAt(timestamp: number | undefined): string {
  if (!timestamp) return '—';
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toISOString().slice(0, 10);
}

export function renderDevicesLoadFailed(error?: unknown): void {
  const target = nodesLane();
  if (!target) return;
  nodeScope?.destroy();
  nodeScope = null;
  target.replaceChildren(loadFailedBox(error));
}

function tiersText(tiers: DesktopNode['tiers']): string {
  const granted = TIER_LABELS.filter(([tier]) => tiers?.[tier]);
  if (granted.length === 0) return '—';
  if (granted.length === 1 && granted[0][0] === 'read') return translate('local.tierReadOnly');
  return granted.map(([, key]) => translate(key)).join(' · ');
}

function openPairing(): void {
  const open = globals().openLocalControlModal;
  if (typeof open !== 'function') return;
  globals().closeSettings?.();
  open();
}

/** Every node of the caller, then the one way to add another: pairing in Local Control. */
export function renderDeviceNodes(nodes: readonly DesktopNode[]): void {
  const target = nodesLane();
  if (!target) return;
  nodeScope?.destroy();
  const scope = createLifecycleScope();
  nodeScope = scope;

  const add = createButton({
    label: translate('local.pairAddBtn'), variant: 'secondary', size: 'sm',
  });
  add.classList.add('devices-add-btn');
  scope.listen(add, 'click', openPairing);

  if (nodes.length === 0) {
    const empty = element('p', 'stg-empty', translate('devices.empty'));
    empty.dataset.i18n = 'devices.empty';
    target.replaceChildren(empty, add);
    return;
  }

  const table = element('table', 'stg-table');
  const header = document.createElement('thead');
  const headerRow = document.createElement('tr');
  for (const key of [
    'devices.colDevice', 'devices.colPlatform', 'devices.colTiers',
    'devices.colRoots', 'devices.colStatus',
  ] satisfies readonly I18nKey[]) headerRow.append(element('th', '', translate(key)));
  headerRow.append(element('th'));
  header.append(headerRow);

  const body = document.createElement('tbody');
  for (const node of nodes) {
    const online = Boolean(node.online);
    const row = element(
      'tr', `devices-agent-row${online ? '' : ' devices-offline'}`);
    const nodeId = String(node.node_id ?? '');
    const keyId = String(node.credential?.id ?? '');
    row.dataset.nodeId = nodeId;
    const device = document.createElement('td');
    device.append(String(node.name || nodeId || keyId));
    if (nodeId) device.append(' ', element('span', 'stg-dim', `(${nodeId.slice(0, 8)})`));
    row.append(
      device,
      element('td', '', String(node.platform || '—')),
      element('td', 'devices-tiers', tiersText(node.tiers)),
    );

    const roots = (node.share_roots ?? []).map(
      (root) => String(root.name ?? root.path ?? ''));
    const rootCell = document.createElement('td');
    rootCell.append(roots.length
      ? document.createTextNode(roots.join(', '))
      : element('span', 'stg-dim', '—'));
    row.append(rootCell);

    const status = document.createElement('td');
    if (online) {
      status.append(
        element('span', 'devices-online-dot', '●'),
        ` ${translate('devices.online')}`,
      );
    } else {
      status.append(element(
        'span', 'stg-dim', `○ ${translate('devices.offline')}`));
    }
    row.append(status);

    const action = document.createElement('td');
    if (node.attachment === 'process') {
      action.append(element('span', 'stg-dim', translate('local.nodeProcessHint')));
    } else if (nodeId || keyId) {
      const remove = element(
        'button', 'stg-btn stg-btn-danger devices-remove-btn',
        translate('devices.remove'));
      remove.type = 'button';
      scope.listen(remove, 'click', () => {
        void devicesRemoveNode(nodeId || null, keyId || null, remove);
      });
      action.append(remove);
    }
    row.append(action);
    body.append(row);
  }
  table.append(header, body);
  target.replaceChildren(table, add);
}

export function renderDeviceWaits(waits: readonly DeviceWait[]): void {
  const target = waitsLane();
  if (!target) return;
  waitScope?.destroy();
  const scope = createLifecycleScope();
  waitScope = scope;

  if (waits.length === 0) {
    const empty = element('p', 'stg-empty', translate('devices.noWaits'));
    empty.dataset.i18n = 'devices.noWaits';
    target.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const wait of waits) {
    const id = String(wait.waitId ?? '');
    const device = wait.device ?? {};
    const row = element('div', 'stg-row devices-wait-row');
    row.dataset.waitId = id;
    const label = element('span', 'devices-wait-label');
    label.append(
      element('span', 'devices-wait-device',
        String(device.name || device.selector || device.agentId || '')),
      element('span', 'stg-dim', [
        formatCreatedAt(wait.createdAt ? wait.createdAt / 1000 : undefined),
        String(wait.conversationId ?? ''),
      ].join(' · ')),
      element('span', 'devices-wait-reason', String(wait.reason ?? '')),
    );
    const cancel = createButton({
      label: translate('devices.waitCancel'), variant: 'secondary', size: 'sm',
    });
    cancel.classList.add('devices-wait-cancel-btn');
    cancel.dataset.waitId = id;
    scope.listen(cancel, 'click', () => devicesCancelWait(id, cancel));
    row.append(label, cancel);
    fragment.append(row);
  }
  target.replaceChildren(fragment);
}

export function destroyDevicesTab(): void {
  loadGeneration += 1;
  nodeScope?.destroy();
  nodeScope = null;
  waitScope?.destroy();
  waitScope = null;
}

/** Every pending wait: the server pages its pending index by device, so rows are
 * collected across pages and shown in registration order. */
async function populateDeviceWaits(generation: number): Promise<void> {
  const target = waitsLane();
  if (!target) return;
  waitScope?.destroy();
  waitScope = null;
  target.replaceChildren(loadingLine());
  const waits: DeviceWait[] = [];
  let cursor = '';
  try {
    do {
      const page = await desktopApi().deviceWaits(cursor);
      if (generation !== loadGeneration) return;
      if (!page) {
        target.replaceChildren(loadFailedBox());
        return;
      }
      waits.push(...(page.waits ?? []));
      cursor = page.nextCursor ?? '';
    } while (cursor);
  } catch (error: unknown) {
    if (generation !== loadGeneration) return;
    target.replaceChildren(loadFailedBox(error));
    return;
  }
  renderDeviceWaits(waits.sort((a, b) => (a.createdAt ?? 0) - (b.createdAt ?? 0)));
}

export async function populateDevicesTab(): Promise<void> {
  const target = nodesLane();
  if (!target) return;
  const generation = ++loadGeneration;
  nodeScope?.destroy();
  nodeScope = null;
  target.replaceChildren(loadingLine());
  const waits = populateDeviceWaits(generation);

  try {
    const response = await desktopApi().nodes();
    if (generation === loadGeneration) {
      if (response) renderDeviceNodes(response.nodes ?? []);
      else renderDevicesLoadFailed();
    }
  } catch (error: unknown) {
    if (generation === loadGeneration) renderDevicesLoadFailed(error);
  }
  await waits;
}

/** Remove = revoke + disconnect. A live node goes through its node id; an offline
 * paired credential (no node id) is revoked directly. */
export async function devicesRemoveNode(
  nodeId: string | null,
  keyId: string | null,
  button: HTMLButtonElement | null = null,
): Promise<void> {
  if (!nodeId && !keyId) return;
  if (button) button.disabled = true;
  try {
    if (nodeId) await desktopApi().removeNode(nodeId);
    else if (keyId) await desktopApi().revokeToken(keyId);
    await populateDevicesTab();
    globals().showToast?.(translate('devices.removed'));
  } catch {
    if (button?.isConnected) button.disabled = false;
    globals().showToast?.(translate('devices.removeFailed'));
  }
}

export async function devicesCancelWait(
  waitId: string | null,
  button: HTMLButtonElement | null = null,
): Promise<void> {
  if (!waitId) return;
  if (button) button.disabled = true;
  try {
    await desktopApi().cancelDeviceWait(waitId);
    await populateDevicesTab();
    globals().showToast?.(translate('devices.waitCancelled'));
  } catch {
    if (button?.isConnected) button.disabled = false;
    globals().showToast?.(translate('devices.waitCancelFailed'));
  }
}

const bridge = globals();
bridge._populateDevicesTab = () => { void populateDevicesTab(); };
bridge._renderDevicesLoadFailed = renderDevicesLoadFailed;
bridge._renderDeviceNodes = renderDeviceNodes;
bridge._destroyDevicesTab = destroyDevicesTab;
