// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/transport';
import { createDevicesClient } from './client';
import { createDevicesStore, type PushPorts } from './live';
import {
  projectFleet, summarizeFleet, tierChipState, type DevicePermissions, type FleetSources, type Tiers, type WireNode,
} from './model';

const tiers = (on: Array<keyof Tiers>): Tiers => ({
  read: on.includes('read'), write: on.includes('write'), exec: on.includes('exec'), gui: on.includes('gui'),
  egress: on.includes('egress'), codex_login: on.includes('codex_login'),
});

function node(overrides: Partial<WireNode>): WireNode {
  return {
    node_id: 'n1', name: 'gpu-a100', online: true, last_seen: 1000, platform: 'linux', version: '2026.11.15',
    tiers: tiers(['read', 'write', 'exec']), credential: { id: 'k1', name: 'gpu-a100' }, attachment: 'paired',
    state: 'active', kind: 'server', route: 'internal', rtt_ms: 4, running: 1, ...overrides,
  };
}

function permissions(nodeId: string, home: Tiers, device: Tiers | null): DevicePermissions {
  const effective = Object.fromEntries(Object.entries(home)
    .map(([key, value]) => [key, value && !!device?.[key as keyof Tiers]])) as Tiers;
  return { node_id: nodeId, home, device, effective };
}

function sources(overrides: Partial<FleetSources> = {}): FleetSources {
  return {
    nodes: { state: 'ok', value: [node({})] },
    permissions: { state: 'absent' },
    defaults: { state: 'absent' },
    browsers: { state: 'ok', value: [] },
    waits: { state: 'ok', value: [] },
    home: { state: 'ok', value: { host: 'codelab-main', version: '0.19.2', bootId: 'b1', bootedAt: 0, storage: 'tofudb' } },
    ...overrides,
  };
}

describe('projectFleet', () => {
  it('groups by kind in a fixed order, lists the Home first and drops revoked nodes', () => {
    const fleet = projectFleet(sources({
      nodes: { state: 'ok', value: [
        node({ node_id: 'srv', kind: 'server' }),
        node({ node_id: 'mac', kind: 'computer', name: 'ruan-macbook' }),
        node({ node_id: 'old', state: 'revoked' }),
      ] },
      browsers: { state: 'ok', value: [{ client_id: 'c1', name: 'Chrome', profile: 'Work', ext_version: '5.9.0', capabilities: ['read', 'click'] }] },
    }));
    expect(fleet.groups.map(group => group.kind)).toEqual(['self', 'computer', 'server', 'browser']);
    expect(fleet.rows.map(row => row.key)).toEqual(['self', 'node:srv', 'node:mac', 'browser:c1']);
    expect(fleet.rows.find(row => row.key === 'browser:c1')?.name).toBe('Chrome · Work');
  });

  it('carries pending, conflict, never-connected and offline states and per-node waits', () => {
    const fleet = projectFleet(sources({
      nodes: { state: 'ok', value: [
        node({ node_id: 'p', state: 'pending', online: false, fingerprint: '7F3A-91C2' }),
        node({ node_id: 'c', state: 'conflict' }),
        node({ node_id: 'n', online: false, last_seen: null }),
        node({ node_id: 'o', online: false, last_seen: 50 }),
      ] },
      waits: { state: 'ok', value: [{ device: { agentId: 'o' } }, { device: { agentId: 'o' } }, { device: { agentId: null } }] },
    }));
    const byId = Object.fromEntries(fleet.rows.map(row => [row.nodeId, row]));
    expect(byId.p.state).toBe('pending');
    expect(byId.p.fingerprint).toBe('7F3A-91C2');
    expect(byId.c.state).toBe('conflict');
    expect(byId.n.state).toBe('never');
    expect(byId.o.state).toBe('offline');
    expect(byId.o.lastSeenMs).toBe(50_000);
    expect(byId.o.waiting).toBe(2);
  });

  it('joins Home permissions and per-kind defaults by node ID', () => {
    const fleet = projectFleet(sources({
      permissions: { state: 'ok', value: { permissions: {
        n1: permissions('n1', tiers(['read', 'write']), tiers(['read', 'write', 'exec'])),
      }, liveness_window_s: 15 } },
      defaults: { state: 'ok', value: { defaults: { server: 'n1', computer: null } } },
    }));
    const row = fleet.rows.find(candidate => candidate.nodeId === 'n1')!;
    expect(row.permissions?.effective.write).toBe(true);
    expect(row.isDefault).toBe(true);
    expect(fleet.livenessWindowS).toBe(15);
  });

  it('shows no Home row and no nodes when those reads failed, instead of inventing them', () => {
    const fleet = projectFleet(sources({ home: { state: 'failed', message: 'x' }, nodes: { state: 'absent' } }));
    expect(fleet.rows).toEqual([]);
  });
});

describe('tierChipState', () => {
  const base = projectFleet(sources()).rows.find(row => row.nodeId === 'n1')!;
  it('is the two-ended intersection when the Home limit is known', () => {
    const row = { ...base, permissions: permissions('n1', tiers(['read', 'exec', 'gui']), tiers(['read', 'exec'])) };
    expect(tierChipState(row, 'exec')).toBe('on');
    expect(tierChipState(row, 'gui')).toBe('device-off');
    expect(tierChipState(row, 'write')).toBe('home-off');
    // read is the floor of a confirmed device
    expect(tierChipState({ ...row, permissions: permissions('n1', tiers([]), tiers(['read'])) }, 'read')).toBe('on');
  });
  it('falls back to what the device declared without claiming the Home allows it', () => {
    expect(tierChipState(base, 'write')).toBe('unknown-home');
    expect(tierChipState(base, 'gui')).toBe('device-off');
  });
});

it('summarizes nodes for the pill without counting the Home', () => {
  const fleet = projectFleet(sources({ nodes: { state: 'ok', value: [
    node({ node_id: 'a' }), node({ node_id: 'b', online: false }), node({ node_id: 'c', state: 'pending', online: false }),
  ] } }));
  expect(summarizeFleet(fleet)).toEqual({ online: 1, total: 3, attention: 1 });
});

describe('devices client', () => {
  it('maps a route this Home does not serve to absent and other failures to failed', async () => {
    const request = vi.fn(async (path: string) => {
      if (path.includes('permissions')) throw new ApiError('nope', { status: 404 });
      if (path.includes('defaults')) throw new ApiError('boom', { status: 500 });
      return { nodes: [node({})] };
    });
    const client = createDevicesClient(request as never);
    const signal = new AbortController().signal;
    expect((await client.permissions(signal)).state).toBe('absent');
    expect(await client.defaults(signal)).toEqual({ state: 'failed', message: 'boom' });
    expect((await client.nodes(signal)).state).toBe('ok');
  });

  it('reads every page of pending waits', async () => {
    const request = vi.fn(async (_path: string, options?: { query?: Record<string, unknown> }) => (
      options?.query?.cursor ? { waits: [{ waitId: 'b' }], nextCursor: null } : { waits: [{ waitId: 'a' }], nextCursor: 'c1' }));
    const result = await createDevicesClient(request as never).waits(new AbortController().signal);
    expect(result).toEqual({ state: 'ok', value: [{ waitId: 'a' }, { waitId: 'b' }] });
  });
});

function fakePush() {
  const handlers = new Set<(frame: unknown) => void>();
  const reconnects = new Set<() => void>();
  const connections = new Set<(connected: boolean) => void>();
  const ports: PushPorts = {
    subscribe: handler => { handlers.add(handler); return () => handlers.delete(handler); },
    onReconnect: listener => { reconnects.add(listener); return () => reconnects.delete(listener); },
    onConnection: listener => { connections.add(listener); listener(true); return () => connections.delete(listener); },
    isOurs: userId => userId === 'owner',
  };
  return {
    ports,
    frame: (frame: unknown) => handlers.forEach(handler => handler(frame)),
    reconnect: () => reconnects.forEach(listener => listener()),
    connection: (value: boolean) => connections.forEach(listener => listener(value)),
    subscribers: () => handlers.size,
  };
}

function fakeClient(state: { nodes: WireNode[]; failNodes?: boolean; bootId?: string }) {
  const calls = { nodes: 0 };
  const ok = <T>(value: T) => Promise.resolve({ state: 'ok' as const, value });
  const client = {
    nodes: () => { calls.nodes += 1; return state.failNodes ? Promise.resolve({ state: 'failed' as const, message: 'down' }) : ok(state.nodes); },
    permissions: () => Promise.resolve({ state: 'absent' as const }),
    defaults: () => Promise.resolve({ state: 'absent' as const }),
    browsers: () => ok([]),
    waits: () => ok([]),
    home: () => ok({ host: 'h', bootId: state.bootId || 'b1' }),
  };
  return { client: client as never, calls };
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

describe('live devices store', () => {
  it('reads on first subscribe and re-reads only on our devices.changed hints and on reconnect', async () => {
    const push = fakePush();
    const fake = fakeClient({ nodes: [node({})] });
    const store = createDevicesStore(push.ports, fake.client, () => 1000);
    const seen: number[] = [];
    store.subscribe(snapshot => seen.push(snapshot.fleet?.rows.length ?? -1));
    await flush();
    expect(fake.calls.nodes).toBe(1);
    push.frame({ type: 'conv_changed', userId: 'owner' });
    push.frame({ type: 'devices.changed', userId: 'someone-else' });
    await flush();
    expect(fake.calls.nodes).toBe(1);
    push.frame({ type: 'devices.changed', userId: 'owner' });
    await flush();
    expect(fake.calls.nodes).toBe(2);
    push.reconnect();
    await flush();
    expect(fake.calls.nodes).toBe(3);
  });

  it('coalesces hints that arrive during a read into one more read', async () => {
    const push = fakePush();
    const fake = fakeClient({ nodes: [] });
    const store = createDevicesStore(push.ports, fake.client);
    store.subscribe(() => {});
    push.frame({ type: 'devices.changed', userId: 'owner' });
    push.frame({ type: 'devices.changed', userId: 'owner' });
    push.frame({ type: 'devices.changed', userId: 'owner' });
    await flush(); await flush();
    expect(fake.calls.nodes).toBe(2);
  });

  it('marks state stale while disconnected, keeps last good values, and reports a Home restart', async () => {
    const push = fakePush();
    const state: { nodes: WireNode[]; failNodes?: boolean; bootId?: string } = { nodes: [node({})] };
    const fake = fakeClient(state);
    let now = 1000;
    const store = createDevicesStore(push.ports, fake.client, () => now);
    store.subscribe(() => {});
    await flush();
    expect(store.snapshot().syncedAt).toBe(1000);
    push.connection(false);
    expect(store.snapshot().disconnected).toBe(true);
    expect(store.snapshot().fleet?.rows.some(row => row.nodeId === 'n1')).toBe(true);
    state.failNodes = true;
    state.bootId = 'b2';
    now = 5000;
    push.reconnect();
    await flush();
    const snapshot = store.snapshot();
    expect(snapshot.disconnected).toBe(false);
    expect(snapshot.staleSources).toEqual(['nodes']);
    expect(snapshot.fleet?.rows.some(row => row.nodeId === 'n1')).toBe(true);
    expect(snapshot.restartedAt).toBe(5000);
  });

  it('releases its push subscription when the last view leaves', async () => {
    const push = fakePush();
    const store = createDevicesStore(push.ports, fakeClient({ nodes: [] }).client);
    const release = store.subscribe(() => {});
    expect(push.subscribers()).toBe(1);
    release();
    expect(push.subscribers()).toBe(0);
  });
});
