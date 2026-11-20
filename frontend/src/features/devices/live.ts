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
import { t } from '../../i18n';
import { createDevicesClient, type DevicesClient } from './client';
import { projectFleet, TIERS, type DevicePermissions, type Fleet, type FleetSources, type Source, type Tier, type Tiers } from './model';

export interface PushPorts {
  subscribe(handler: (frame: unknown) => void): () => void;
  onReconnect(listener: () => void): () => void;
  /** Connection changes; called at once with the current state. Absent: the state is unknown. */
  onConnection?(listener: (connected: boolean) => void): () => void;
  isOurs?(userId: unknown): boolean;
}

type CeilingTier = Exclude<Tier, 'read'>;
export type CeilingIntent = Partial<Record<CeilingTier, boolean>>;

export interface CeilingReview {
  message: string;
  unsent: CeilingIntent;
  reading?: boolean;
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
  ceilingReviews: ReadonlyMap<string, CeilingReview>;
}

export interface DevicesStore {
  snapshot(): DevicesSnapshot;
  subscribe(listener: (snapshot: DevicesSnapshot) => void): () => void;
  refresh(): Promise<void>;
  setCeilingTier(nodeId: string, tier: CeilingTier, enabled: boolean, reviewedRevision: number): Promise<void>;
  /** An explicit submission can replace the exact pending review it was shown. */
  setCeiling(nodeId: string, intent: CeilingIntent, reviewedRevision: number, review?: CeilingReview): Promise<void>;
  confirmCeilingReview(nodeId: string, review: CeilingReview, reviewedRevision: number): Promise<void>;
  discardCeilingReview(nodeId: string, review: CeilingReview): void;
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
    staleSources: [], ceilingReviews: new Map(),
  };
  let inFlight: Promise<void> | null = null;
  let again = false;
  let controller: AbortController | null = null;
  let releases: Array<() => void> = [];
  let ceilingWrite: Promise<void> | null = null;
  let ceilingAcknowledgements = 0;
  // A different owner snapshot or an unacknowledged PUT invalidates already queued edits.
  let ceilingEpoch = 0;
  let firstCompatibleEpoch = 0;
  let readAdvance: { from: number; to: number; previousCompatibleEpoch: number; epoch: number } | undefined;
  let lastSaveError = '';

  const revision = (source: FleetSources['permissions'] | undefined) => {
    if (source?.state !== 'ok') return undefined;
    return source.value.rev;
  };
  const baseFor = (nodeId: string) => {
    const source = state.sources?.permissions;
    if (source?.state !== 'ok' || state.staleSources.includes('permissions')) return undefined;
    const row = source.value.permissions[nodeId];
    return row && Number.isSafeInteger(row.rev) && row.rev >= 0 && row.rev === revision(source) ? row : undefined;
  };

  const publish = (next: Partial<DevicesSnapshot>) => {
    state = { ...state, ...next };
    for (const listener of [...listeners]) listener(state);
  };

  const readOnce = async () => {
    controller = new AbortController();
    const signal = controller.signal;
    const beforeCeilingWrites = ceilingAcknowledgements;
    const [nodes, permissions, defaults, browsers, waits, home] = await Promise.all([
      client.nodes(signal), client.permissions(signal), client.defaults(signal),
      client.browsers(signal), client.waits(signal), client.home(signal),
    ]);
    if (signal.aborted) return;
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
    const restarted = !!(oldBoot && newBoot && oldBoot !== newBoot);
    if (restarted) restartedAt = now();
    const previousRev = revision(previous?.permissions);
    const nextRev = revision(permissions);
    // A GET started before a PUT can finish after its acknowledgement. Keep a
    // genuinely newer snapshot, but don't roll back to an older owner's policy.
    if (previous && ceilingAcknowledgements !== beforeCeilingWrites &&
        (nextRev === undefined || (previousRev !== undefined && nextRev < previousRev))) {
      merged.permissions = previous.permissions;
      again = true;
    } else if (previous && (restarted || revision(merged.permissions) !== previousRev)) {
      ceilingEpoch += 1;
      const mergedRev = revision(merged.permissions);
      readAdvance = !restarted && previousRev !== undefined && mergedRev !== undefined && mergedRev > previousRev
        ? { from: previousRev, to: mergedRev, previousCompatibleEpoch: firstCompatibleEpoch, epoch: ceilingEpoch }
        : undefined;
      firstCompatibleEpoch = ceilingEpoch;
      lastSaveError = '';
    }
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
          try {
            await readOnce();
          } catch (error) {
            if (!(error instanceof DOMException && error.name === 'AbortError')) {
              publish({ loading: false, staleSources: [...new Set([...state.staleSources, 'permissions' as const])] });
              console.warn('[devices] fleet read failed', error);
            }
          }
        } while (again);
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  };

  const updateReview = (nodeId: string, review: CeilingReview | null) => {
    const ceilingReviews = new Map(state.ceilingReviews);
    if (review) ceilingReviews.set(nodeId, review);
    else ceilingReviews.delete(nodeId);
    publish({ ceilingReviews });
  };

  const unavailable = () => new Error(t('devicesPage.drawer.homeUnavailable'));
  const editable = TIERS.filter(key => key !== 'read');
  const pendingAgainst = (intent: CeilingIntent, home?: Tiers): CeilingIntent => Object.fromEntries(
    editable.filter(key => intent[key] !== undefined && intent[key] !== home?.[key]).map(key => [key, intent[key]]));
  const saveCeiling = (nodeId: string, intent: CeilingIntent, reviewedRevision: number,
    review?: CeilingReview): Promise<void> => {
    const seen = baseFor(nodeId);
    if (!seen) return Promise.reject(unavailable());
    const epoch = ceilingEpoch;
    const staleControl = reviewedRevision !== seen.rev;
    const prior = ceilingWrite;
    // The revision is owner-wide, so serialize across devices as well as across tiers.
    const write = (prior ? prior.catch(() => {}) : Promise.resolve()).then(async () => {
      const base = baseFor(nodeId);
      const existing = state.ceilingReviews.get(nodeId);
      if (review && (review.reading || review !== existing || staleControl || epoch !== ceilingEpoch
          || !base || base.rev !== reviewedRevision)) {
        throw unavailable();
      }
      if (!base || staleControl || epoch < firstCompatibleEpoch || (existing && !review)) {
        const message = existing?.message || lastSaveError || unavailable().message;
        // Retain explicit values even if they currently match: the Home may
        // change again before the owner opens or confirms this pending review.
        updateReview(nodeId, { message, unsent: { ...existing?.unsent, ...intent } });
        throw new Error(message);
      }

      // Filter against the latest base, never replay a previously submitted intent.
      if (review) updateReview(nodeId, null);
      if (!Object.keys(pendingAgainst(intent, base.home)).length) return;
      const next: Partial<Tiers> = Object.fromEntries(editable.map(key => [key, intent[key] ?? base.home[key]]));
      const writeEpoch = ceilingEpoch;
      let acknowledgement: DevicePermissions;
      try {
        acknowledgement = await client.setCeiling(nodeId, next, base.rev);
        if (!Number.isSafeInteger(acknowledgement.rev) || acknowledgement.rev <= base.rev || acknowledgement.node_id !== nodeId) {
          throw unavailable();
        }
      } catch (error) {
        // A 409 means the base was stale; a lost response may have committed.
        // In either case read after the failure and invalidate all queued writes.
        firstCompatibleEpoch = ++ceilingEpoch;
        readAdvance = undefined;
        const message = error instanceof Error ? error.message : String(error);
        updateReview(nodeId, { message, unsent: {}, reading: true });
        // If a pre-failure GET was in flight, refresh schedules a second read.
        await refresh();
        lastSaveError = message;
        updateReview(nodeId, { message, unsent: {} });
        throw error;
      }
      const sources = state.sources;
      if (sources?.permissions.state !== 'ok' || writeEpoch !== ceilingEpoch || revision(sources.permissions) !== base.rev) {
        // Never publish a superseded acknowledgement. Only the current Home
        // ceiling can establish that the submitted intent is still satisfied.
        const current = baseFor(nodeId);
        if (!current || Object.keys(pendingAgainst(intent, current.home)).length) throw unavailable();
        // An exact echo of this CAS (one read advance, no restart or newer
        // owner mutation) is safe for edits queued before that read as well.
        if (readAdvance?.epoch === ceilingEpoch && ceilingEpoch === writeEpoch + 1
            && readAdvance.from === base.rev && readAdvance.to === acknowledgement.rev
            && current.rev === acknowledgement.rev
            && editable.every(key => current.home[key] === next[key] && acknowledgement.home[key] === next[key])) {
          firstCompatibleEpoch = readAdvance.previousCompatibleEpoch;
          ceilingAcknowledgements += 1;
        }
        return;
      }
      // A successful CAS proves that the base policy was current for all devices.
      // Only the target's ceiling changed, but all rows share the new revision.
      const value = sources.permissions.value;
      const permissions = Object.fromEntries(Object.entries(value.permissions)
        .map(([id, row]) => [id, { ...row, rev: acknowledgement.rev }]));
      permissions[nodeId] = acknowledgement;
      const updated: FleetSources = { ...sources, permissions: { state: 'ok', value: {
        ...value, rev: acknowledgement.rev, permissions,
      } } };
      ceilingAcknowledgements += 1;
      publish({ sources: updated, fleet: projectFleet(updated) });
    });
    ceilingWrite = write;
    const release = () => { if (ceilingWrite === write) ceilingWrite = null; };
    void write.then(release, release);
    return write;
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
    if (!ceilingWrite) controller?.abort();
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
    setCeilingTier: (nodeId, tier, enabled, reviewedRevision) => saveCeiling(nodeId, { [tier]: enabled }, reviewedRevision),
    setCeiling: saveCeiling,
    confirmCeilingReview: (nodeId, review, reviewedRevision) => saveCeiling(nodeId, review.unsent, reviewedRevision, review),
    discardCeilingReview(nodeId, review) {
      if (!review.reading && state.ceilingReviews.get(nodeId) === review) updateReview(nodeId, null);
    },
  };
}
