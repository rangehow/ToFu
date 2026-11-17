/** Live devices store: one owner-scoped fleet view shared by the sidebar pill and the Devices page.
 *
 * Reads every registry once when the first view subscribes, then again only when the Home says something
 * changed: the owner-scoped `notify` push frame `devices.changed` (task `__devices__`; published by the
 * registry on pair/confirm/reject/rename/revoke/conflict and by the bridge on connect and at liveness
 * expiry) and every push reconnect, which is how a restarted Home is noticed. There is no polling loop.
 * While the push channel is down the last read is kept and marked stale with its time; a failed read keeps
 * the last good value of that source and says so. A changed Home boot ID is surfaced as a restart.
 * Entry points: getDevicesStore, createDevicesStore. Dependencies: the push ports and the devices client.
 */
import { createDevicesClient, type DevicesClient } from './client';
import { projectFleet, type Fleet, type FleetSources, type Source } from './model';

export interface PushPorts {
  subscribe(handler: (frame: unknown) => void): () => void;
  onReconnect(listener: () => void): () => void;
  /** Connection changes; called at once with the current state. Absent: the state is unknown. */
  onConnection?(listener: (connected: boolean) => void): () => void;
  isOurs?(userId: unknown): boolean;
}

export interface DevicesSnapshot {
  fleet: Fleet | null;
  sources: FleetSources | null;
  /** First read still in flight. */
  loading: boolean;
  /** Push channel to the Home is down: the shown state may be out of date. */
  disconnected: boolean;
  /** Time of the last read that reached the Home (ms). */
  syncedAt: number | null;
  /** The Home restarted since this page first read it (boot ID changed). */
  restartedAt: number | null;
  /** Sources whose latest read failed while an earlier value is still shown. */
  staleSources: Array<keyof FleetSources>;
}

export interface DevicesStore {
  snapshot(): DevicesSnapshot;
  subscribe(listener: (snapshot: DevicesSnapshot) => void): () => void;
  refresh(): Promise<void>;
  readonly client: DevicesClient;
}

const SOURCE_KEYS: Array<keyof FleetSources> = ['nodes', 'permissions', 'defaults', 'browsers', 'waits', 'home'];

export function isDevicesChangedFrame(frame: unknown): frame is { type: 'devices.changed'; userId?: unknown } {
  return !!frame && typeof frame === 'object' && (frame as { type?: unknown }).type === 'devices.changed';
}

export function createDevicesStore(push: PushPorts | null, client: DevicesClient = createDevicesClient(),
  now: () => number = () => Date.now()): DevicesStore {
  const listeners = new Set<(snapshot: DevicesSnapshot) => void>();
  let state: DevicesSnapshot = {
    fleet: null, sources: null, loading: true, disconnected: false, syncedAt: null, restartedAt: null,
    staleSources: [],
  };
  let inFlight: Promise<void> | null = null;
  let again = false;
  let controller: AbortController | null = null;
  let releases: Array<() => void> = [];

  const publish = (next: Partial<DevicesSnapshot>) => {
    state = { ...state, ...next };
    for (const listener of [...listeners]) listener(state);
  };

  const readOnce = async () => {
    controller = new AbortController();
    const signal = controller.signal;
    const [nodes, permissions, defaults, browsers, waits, home] = await Promise.all([
      client.nodes(signal), client.permissions(signal), client.defaults(signal),
      client.browsers(signal), client.waits(signal), client.home(signal),
    ]);
    const fresh: FleetSources = { nodes, permissions, defaults, browsers, waits, home };
    const previous = state.sources;
    const merged = { ...fresh } as FleetSources;
    const stale: Array<keyof FleetSources> = [];
    for (const key of SOURCE_KEYS) {
      const value = fresh[key] as Source<unknown>;
      const before = previous?.[key] as Source<unknown> | undefined;
      if (value.state === 'failed' && before?.state === 'ok') {
        (merged as unknown as Record<string, Source<unknown>>)[key] = before;
        stale.push(key);
      }
    }
    const reachedHome = SOURCE_KEYS.some(key => fresh[key].state !== 'failed');
    let restartedAt = state.restartedAt;
    const oldBoot = previous?.home.state === 'ok' ? previous.home.value.bootId : undefined;
    const newBoot = home.state === 'ok' ? home.value.bootId : undefined;
    if (oldBoot && newBoot && oldBoot !== newBoot) restartedAt = now();
    publish({
      sources: merged, fleet: projectFleet(merged), loading: false, staleSources: stale, restartedAt,
      syncedAt: reachedHome ? now() : state.syncedAt,
    });
  };

  const refresh = (): Promise<void> => {
    if (inFlight) { again = true; return inFlight; }
    inFlight = (async () => {
      try {
        do {
          again = false;
          await readOnce();
        } while (again);
      } catch (error) {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          publish({ loading: false });
          console.warn('[devices] fleet read failed', error);
        }
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  };

  const start = () => {
    if (!push) return;
    releases.push(push.subscribe(frame => {
      if (!isDevicesChangedFrame(frame)) return;
      if (push.isOurs && !push.isOurs(frame.userId)) return;
      void refresh();
    }));
    releases.push(push.onReconnect(() => {
      publish({ disconnected: false });
      void refresh();
    }));
    if (push.onConnection) {
      releases.push(push.onConnection(connected => {
        if (connected === !state.disconnected) return;
        publish({ disconnected: !connected });
      }));
    }
  };

  const stop = () => {
    for (const release of releases) release();
    releases = [];
    controller?.abort();
  };

  return {
    client,
    snapshot: () => state,
    subscribe(listener) {
      const first = listeners.size === 0;
      listeners.add(listener);
      if (first) { start(); void refresh(); }
      listener(state);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) stop();
      };
    },
    refresh,
  };
}
