/** Devices wire client: owner-scoped reads and writes of the Home's device registries.
 *
 * Paths and shapes belong to their contracts (contracts/desktop_node_v1.schema.json and
 * desktop_node_registry_v1, device_routing_v1, device_waits_v1, the browser status read and /api/health).
 * Reads settle as a `Source`: a 404/405/501 means this Home does not serve that read (an older Home, or a
 * phase that has not landed) and is reported as absent, never filled in. Writes throw the canonical ApiError.
 * Entry points: createDevicesClient. Dependencies: api/transport `request`.
 */
import { ApiError, request as transportRequest, type RequestOptions } from '../../api/transport';
import type {
  DefaultKind, DefaultsView, DefaultsWriteResponse, DevicePermissions, HomeFacts, PermissionsView, Source, Tiers,
  WireBrowserClient, WireNode, WireWait,
} from './model';

export type Request = <T = unknown>(path: string, options?: RequestOptions) => Promise<T>;

export interface PairCode {
  code: string;
  link: string;
  expires_at: number;
  ttl: number;
  command?: string;
  pending?: unknown[];
}

export interface DevicesClient {
  nodes(signal: AbortSignal): Promise<Source<WireNode[]>>;
  permissions(signal: AbortSignal): Promise<Source<PermissionsView>>;
  defaults(signal: AbortSignal): Promise<Source<DefaultsView>>;
  browsers(signal: AbortSignal): Promise<Source<WireBrowserClient[]>>;
  waits(signal: AbortSignal): Promise<Source<WireWait[]>>;
  home(signal: AbortSignal): Promise<Source<HomeFacts>>;
  pairCode(kind: 'computer' | 'server', base: string): Promise<PairCode>;
  confirm(nodeId: string, name: string): Promise<unknown>;
  reject(nodeId: string): Promise<unknown>;
  rename(nodeId: string, name: string): Promise<unknown>;
  remove(nodeId: string): Promise<unknown>;
  setCeiling(nodeId: string, tiers: Partial<Tiers>, expectedRevision: number): Promise<DevicePermissions>;
  setDefault(kind: DefaultKind, nodeId: string | null): Promise<DefaultsWriteResponse>;
}

const NOT_SERVED = new Set([404, 405, 501]);

function failure(error: unknown): string {
  if (error instanceof Error) return error.message || error.name;
  return String(error);
}

async function read<T>(load: () => Promise<T>): Promise<Source<T>> {
  try {
    return { state: 'ok', value: await load() };
  } catch (error) {
    if (error instanceof ApiError && NOT_SERVED.has(error.status)) return { state: 'absent' };
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    return { state: 'failed', message: failure(error) };
  }
}

function list<T>(value: unknown, key: string): T[] {
  const rows = value && typeof value === 'object' ? (value as Record<string, unknown>)[key] : null;
  return Array.isArray(rows) ? rows as T[] : [];
}

const path = (id: string) => encodeURIComponent(id);

export function createDevicesClient(request: Request = transportRequest): DevicesClient {
  const get = <T>(url: string, signal: AbortSignal, query?: Record<string, unknown>) =>
    request<T>(url, { method: 'GET', signal, query, onError: 'throw' });
  const send = <T>(url: string, method: string, json?: unknown) =>
    request<T>(url, { method, json: json ?? {}, onError: 'throw' });
  return {
    nodes: signal => read(async () => list<WireNode>(await get('/api/v4/desktop/nodes', signal), 'nodes')),
    permissions: signal => read(async () => {
      const value = await get<PermissionsView>('/api/v4/devices/permissions', signal);
      if (!value || !Number.isSafeInteger(value.rev) || value.rev < 0 || !value.permissions) {
        throw new TypeError('Invalid device permissions snapshot');
      }
      return value;
    }),
    defaults: signal => read(async () => {
      const value = await get<Partial<DefaultsView>>('/api/v4/devices/defaults', signal);
      return { ...value, defaults: value?.defaults || {} };
    }),
    browsers: signal => read(async () => list<WireBrowserClient>(await get('/api/v4/browser/status', signal), 'clients')),
    waits: signal => read(async () => {
      // Every pending wait, page by page: a page is bounded by the contract, the list is not truncated.
      const waits: WireWait[] = [];
      let cursor: string | null = null;
      do {
        const page: { waits?: WireWait[]; nextCursor?: string | null } = await get(
          '/api/v4/desktop/device-waits', signal, cursor ? { cursor } : undefined);
        waits.push(...list<WireWait>(page, 'waits'));
        cursor = page?.nextCursor || null;
      } while (cursor);
      return waits;
    }),
    home: signal => read(async () => {
      const health = await get<Record<string, any>>('/api/health', signal);
      return {
        host: window.location.host,
        version: typeof health?.version === 'string' ? health.version : undefined,
        bootId: typeof health?.bootId === 'string' ? health.bootId : undefined,
        bootedAt: typeof health?.bootProfile?.booted_at === 'number' ? health.bootProfile.booted_at * 1000 : null,
        storage: typeof health?.storage?.backend === 'string' ? health.storage.backend : undefined,
      };
    }),
    pairCode: (kind, base) => request<PairCode>('/api/v4/desktop/pair-code', {
      method: 'POST', json: { kind }, query: base ? { base } : {}, onError: 'throw',
    }),
    confirm: (nodeId, name) => send(`/api/v4/desktop/nodes/${path(nodeId)}/confirm`, 'POST', name ? { name } : {}),
    reject: nodeId => send(`/api/v4/desktop/nodes/${path(nodeId)}/reject`, 'POST'),
    rename: (nodeId, name) => send(`/api/v4/desktop/nodes/${path(nodeId)}`, 'PATCH', { name }),
    remove: nodeId => request(`/api/v4/desktop/nodes/${path(nodeId)}`, { method: 'DELETE', onError: 'throw' }),
    setCeiling: (nodeId, tiers, expectedRevision) => send<DevicePermissions>(
      `/api/v4/devices/${path(nodeId)}/ceiling`, 'PUT', { tiers, expected_revision: expectedRevision }),
    setDefault: (kind, nodeId) => send<DefaultsWriteResponse>('/api/v4/devices/defaults', 'PUT', { [kind]: nodeId }),
  };
}
