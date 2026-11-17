/** Devices fleet projection: pure functions from the Home's read models to the rows the page draws.
 *
 * Every machine that is not the Home is an equal node (docs/MULTI_DEVICE_NETWORK_DESIGN.md §5). The page
 * reads several owner-scoped registries and this module joins them, keyed by node ID:
 * - nodes: GET /api/v4/desktop/nodes (contracts/desktop_node_v1.schema.json `Node`, registry fields from
 *   contracts/desktop_node_registry_v1.schema.json);
 * - permissions + liveness window: GET /api/v4/devices/permissions (contracts/device_routing_v1.schema.json);
 * - defaults per kind: GET /api/v4/devices/defaults (same contract);
 * - browsers: GET /api/v4/browser/status `clients` (connected extensions only);
 * - waits: GET /api/v4/desktop/device-waits (contracts/device_waits_v1.schema.json);
 * - Home facts: GET /api/health.
 * A source the Home does not serve or that failed is carried as such; nothing is guessed in its place.
 * Entry points: projectFleet, tierChipState, summarizeFleet. Dependencies: none.
 */

export const TIERS = ['read', 'write', 'exec', 'gui', 'egress', 'codex_login'] as const;
export type Tier = typeof TIERS[number];
export type Tiers = Record<Tier, boolean>;

export type DeviceKind = 'self' | 'computer' | 'server' | 'browser';
export const KIND_ORDER: readonly DeviceKind[] = ['self', 'computer', 'server', 'browser'];
/** Kinds the Home can mark a default for (owner decision 7). */
export const DEFAULT_KINDS = ['computer', 'server', 'browser'] as const;
export type DefaultKind = typeof DEFAULT_KINDS[number];

export interface WireSystem {
  os?: string; release?: string; version?: string; arch?: string;
  shells?: { available?: string[]; default?: string };
}

export interface WireNode {
  node_id: string | null;
  name: string;
  online: boolean;
  last_seen?: number | null;
  platform?: string;
  version?: string;
  outdated?: boolean;
  system?: WireSystem | null;
  share_roots?: Array<string | { name?: string; path?: string }>;
  tiers: Partial<Tiers>;
  credential: { id: string; name: string | null; created_at?: unknown } | null;
  attachment?: 'paired' | 'process';
  state?: 'pending' | 'active' | 'revoked' | 'conflict';
  kind?: 'computer' | 'server';
  fingerprint?: string;
  paired_at?: number | null;
  confirmed_at?: number | null;
  route?: string;
  rtt_ms?: number | null;
  running?: number;
}

export interface DevicePermissions {
  node_id: string;
  home: Tiers;
  device: Tiers | null;
  effective: Tiers;
  rev?: number;
}

export interface PermissionsView {
  permissions: Record<string, DevicePermissions>;
  rev?: number;
  liveness_window_s?: number;
}

export interface DefaultsView {
  defaults: Partial<Record<DefaultKind, string | null>>;
  rev?: number;
}

export interface WireBrowserClient {
  client_id: string;
  name?: string;
  profile?: string;
  ext_version?: string;
  chrome_major?: number;
  last_poll?: number;
  seconds_ago?: number;
  capabilities?: string[];
}

export interface WireWait {
  waitId?: string;
  conversationId?: string;
  device?: { selector?: string; agentId?: string | null; name?: string | null };
}

export interface HomeFacts {
  version?: string;
  bootId?: string;
  bootedAt?: number | null;
  storage?: string;
  host: string;
}

/** One read: served, not served by this Home (an older Home or a phase not landed), or failed. */
export type Source<T> =
  | { state: 'ok'; value: T }
  | { state: 'absent' }
  | { state: 'failed'; message: string };

export interface FleetSources {
  nodes: Source<WireNode[]>;
  permissions: Source<PermissionsView>;
  defaults: Source<DefaultsView>;
  browsers: Source<WireBrowserClient[]>;
  waits: Source<WireWait[]>;
  home: Source<HomeFacts>;
}

export type DeviceState = 'online' | 'offline' | 'never' | 'pending' | 'conflict';

/** How a capability chip reads: effective on, allowed here but off on the device, or off on this Home. */
export type ChipState = 'on' | 'device-off' | 'home-off' | 'unknown-home';

export interface DeviceRow {
  key: string;
  nodeId: string | null;
  credentialId: string | null;
  kind: DeviceKind;
  name: string;
  state: DeviceState;
  lastSeenMs: number | null;
  rttMs: number | null;
  version: string;
  outdated: boolean;
  system: string;
  route: string;
  fingerprint: string;
  shareRoots: string[];
  running: number;
  waiting: number;
  declared: Partial<Tiers>;
  permissions: DevicePermissions | null;
  isDefault: boolean;
  capabilities: string[];
  inProcess: boolean;
}

export interface Fleet {
  rows: DeviceRow[];
  groups: Array<{ kind: DeviceKind; rows: DeviceRow[] }>;
  livenessWindowS: number | null;
  defaults: Partial<Record<DefaultKind, string | null>> | null;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function describeSystem(system: WireSystem | null | undefined, platform = ''): string {
  if (!system) return platform;
  const os = [text(system.os), text(system.release)].filter(Boolean).join(' ');
  return [os || platform, text(system.arch)].filter(Boolean).join(' · ');
}

function shareRootLabel(root: string | { name?: string; path?: string }): string {
  if (typeof root === 'string') return root;
  return text(root.path) || text(root.name);
}

function nodeKind(node: WireNode): DeviceKind {
  // A node that predates the declared kind ran the desktop node program, which is a computer.
  return node.kind === 'server' ? 'server' : 'computer';
}

function nodeState(node: WireNode): DeviceState {
  if (node.state === 'pending') return 'pending';
  if (node.state === 'conflict') return 'conflict';
  if (node.online) return 'online';
  return node.last_seen ? 'offline' : 'never';
}

/** Project every source into grouped rows. Revoked nodes are gone from the fleet. */
export function projectFleet(sources: FleetSources): Fleet {
  const permissions = sources.permissions.state === 'ok' ? sources.permissions.value : null;
  const defaults = sources.defaults.state === 'ok' ? sources.defaults.value.defaults : null;
  const waitsByNode = new Map<string, number>();
  if (sources.waits.state === 'ok') {
    for (const wait of sources.waits.value) {
      const id = wait.device?.agentId;
      if (id) waitsByNode.set(id, (waitsByNode.get(id) || 0) + 1);
    }
  }
  const rows: DeviceRow[] = [];
  if (sources.home.state === 'ok') {
    rows.push({
      key: 'self', nodeId: null, credentialId: null, kind: 'self', name: sources.home.value.host,
      state: 'online', lastSeenMs: null, rttMs: null, version: text(sources.home.value.version),
      outdated: false, system: '', route: 'local', fingerprint: '', shareRoots: [], running: 0, waiting: 0,
      declared: {}, permissions: null, isDefault: false, capabilities: [], inProcess: true,
    });
  }
  if (sources.nodes.state === 'ok') {
    for (const node of sources.nodes.value) {
      if (node.state === 'revoked') continue;
      const kind = nodeKind(node);
      const nodeId = node.node_id || null;
      const permission = nodeId && permissions ? permissions.permissions[nodeId] || null : null;
      rows.push({
        key: nodeId ? `node:${nodeId}` : `credential:${node.credential?.id || node.name}`,
        nodeId,
        credentialId: node.credential?.id || null,
        kind,
        name: node.name || nodeId || '',
        state: nodeState(node),
        lastSeenMs: typeof node.last_seen === 'number' ? node.last_seen * 1000 : null,
        rttMs: typeof node.rtt_ms === 'number' ? node.rtt_ms : null,
        version: text(node.version),
        outdated: node.outdated === true,
        system: describeSystem(node.system, text(node.platform)),
        route: node.attachment === 'process' ? 'local' : text(node.route),
        fingerprint: text(node.fingerprint),
        shareRoots: (node.share_roots || []).map(shareRootLabel).filter(Boolean),
        running: typeof node.running === 'number' ? node.running : 0,
        waiting: nodeId ? waitsByNode.get(nodeId) || 0 : 0,
        declared: node.tiers || {},
        permissions: permission,
        isDefault: !!(nodeId && defaults && kind !== 'self' && defaults[kind as DefaultKind] === nodeId),
        capabilities: [],
        inProcess: node.attachment === 'process',
      });
    }
  }
  if (sources.browsers.state === 'ok') {
    for (const client of sources.browsers.value) {
      const label = [text(client.name), text(client.profile)].filter(Boolean).join(' · ');
      rows.push({
        key: `browser:${client.client_id}`, nodeId: client.client_id, credentialId: null, kind: 'browser',
        name: label || `Chromium ${client.chrome_major || ''}`.trim(),
        state: 'online',
        lastSeenMs: typeof client.last_poll === 'number' ? client.last_poll * 1000 : null,
        rttMs: null, version: text(client.ext_version), outdated: false,
        system: client.chrome_major ? `Chromium ${client.chrome_major}` : '', route: 'extension',
        fingerprint: '', shareRoots: [], running: 0, waiting: 0, declared: {}, permissions: null,
        isDefault: !!(defaults && defaults.browser === client.client_id),
        capabilities: Array.isArray(client.capabilities) ? client.capabilities : [], inProcess: false,
      });
    }
  }
  const groups = KIND_ORDER
    .map(kind => ({ kind, rows: rows.filter(row => row.kind === kind) }))
    .filter(group => group.rows.length > 0);
  return {
    rows,
    groups,
    livenessWindowS: permissions && typeof permissions.liveness_window_s === 'number'
      ? permissions.liveness_window_s : null,
    defaults,
  };
}

/** Chip for one tier. Without the Home ceiling read the device's own declaration is all that is known. */
export function tierChipState(row: DeviceRow, tier: Tier): ChipState | null {
  if (row.permissions) {
    const home = tier === 'read' || row.permissions.home[tier];
    const device = row.permissions.device ? row.permissions.device[tier] : row.declared[tier] === true;
    if (!home) return 'home-off';
    return device ? 'on' : 'device-off';
  }
  if (row.declared[tier] === undefined) return null;
  return row.declared[tier] ? 'unknown-home' : 'device-off';
}

export interface FleetSummary {
  online: number;
  total: number;
  attention: number;
}

/** Counts for the sidebar pill. The Home itself is not counted; pending and conflicting nodes need attention. */
export function summarizeFleet(fleet: Fleet): FleetSummary {
  const nodes = fleet.rows.filter(row => row.kind !== 'self');
  return {
    online: nodes.filter(row => row.state === 'online').length,
    total: nodes.length,
    attention: nodes.filter(row => row.state === 'pending' || row.state === 'conflict').length,
  };
}

/** Coarse relative age for "last seen" labels; the exact time is the label's title. */
export function ageParts(thenMs: number, nowMs: number): { unit: 's' | 'm' | 'h' | 'd'; value: number } {
  const seconds = Math.max(0, Math.round((nowMs - thenMs) / 1000));
  if (seconds < 60) return { unit: 's', value: seconds };
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return { unit: 'm', value: minutes };
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return { unit: 'h', value: hours };
  return { unit: 'd', value: Math.floor(hours / 24) };
}

/** Presets offered when confirming a node: the Home ceiling only; the device keeps its own switch. */
export const PRESETS = {
  read: { read: true, write: false, exec: false, gui: false, egress: false, codex_login: false },
  developer: { read: true, write: true, exec: true, gui: false, egress: false, codex_login: false },
  full: { read: true, write: true, exec: true, gui: true, egress: true, codex_login: false },
} as const satisfies Record<string, Tiers>;
export type PresetName = keyof typeof PRESETS;
