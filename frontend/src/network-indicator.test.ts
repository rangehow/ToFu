// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createBackendAvailabilityMonitor } from './backend-availability-monitor';
import type { BackendAvailabilityPhase } from './backend-availability-monitor';
import {
  createNetworkIndicator,
  projectNetworkIndicator,
  type NetworkIndicatorInput,
  type PushSocketReading,
} from './network-indicator';

const keys = (key: string, params?: Record<string, unknown>): string => (
  params ? `#${key}${JSON.stringify(params)}` : `#${key}`
);

const input = (patch: Partial<NetworkIndicatorInput>): NetworkIndicatorInput => ({
  push: { connected: true, ms: 40, state: 'good', downReason: null },
  backendPhase: 'online',
  browserOnline: true,
  chatStreamDegraded: false,
  ...patch,
});

describe('network indicator state machine', () => {
  it('is online with the measured RTT when the push socket works', () => {
    expect(projectNetworkIndicator(input({}), keys)).toEqual({
      state: 'good', bars: 4, label: '40ms', title: '#net.title: 40ms (#net.state.good)',
    });
    expect(projectNetworkIndicator(input({
      push: { connected: true, ms: 250 },
    }), keys).state).toBe('ok');
    expect(projectNetworkIndicator(input({
      push: { connected: true, ms: 900 },
    }), keys).state).toBe('poor');
  });

  it.each([
    ['handshake', '#net.pushReason.handshake'],
    ['dropped', '#net.pushReason.dropped'],
    ['timeout', '#net.pushReason.timeout'],
    ['connecting', '#net.pushReason.connecting'],
  ])('is reconnecting, never offline, while the server answers (%s)', (reason, reasonKey) => {
    for (const backendPhase of ['online', 'suspect'] as BackendAvailabilityPhase[]) {
      const view = projectNetworkIndicator(input({
        backendPhase,
        push: { connected: false, ms: null, state: 'offline', downReason: reason },
      }), keys);
      expect(view.state).toBe('reconnecting');
      expect(view.label).toBe('#net.reconnecting');
      expect(view.title).toBe(
        `#net.title: #net.pushReconnectingDesc${JSON.stringify({ reason: reasonKey })}`);
    }
  });

  it('names a policy refusal instead of promising a reconnect', () => {
    const view = projectNetworkIndicator(input({
      push: { connected: false, downReason: 'refused' },
    }), keys);
    expect(view).toMatchObject({
      state: 'reconnecting', label: '#net.pushUnavailable',
      title: '#net.title: #net.pushRefusedDesc',
    });
  });

  it('is offline only when the availability owner says the server is unreachable', () => {
    for (const push of [
      { connected: false, downReason: 'dropped' },
      { connected: true, ms: 40 },
      null,
    ] as Array<PushSocketReading | null>) {
      expect(projectNetworkIndicator(input({ push, backendPhase: 'offline' }), keys))
        .toEqual({ state: 'offline', bars: 0, label: '#net.offline', title: '#net.title: #net.offlineDesc' });
    }
    expect(projectNetworkIndicator(input({
      backendPhase: 'offline', browserOnline: false,
    }), keys).title).toBe('#net.title: #net.offlineBrowserDesc');
  });

  it('warns about a stalled chat stream without calling the network offline', () => {
    expect(projectNetworkIndicator(input({ chatStreamDegraded: true }), keys)).toEqual({
      state: 'poor', bars: 2, label: '#net.reconnecting',
      title: '#net.title: 40ms — #net.reconnectingDesc',
    });
  });

  it('falls back to built-in copy when a key is missing', () => {
    const view = projectNetworkIndicator(input({
      push: { connected: false, downReason: 'handshake' },
    }), (key) => key);
    expect(view.label).toBe('重连中');
    expect(view.title).toContain('握手未成功');
  });
});

function badgeDocument(): Document {
  document.body.innerHTML = '<span id="netLatencyBadge" data-state="unknown">'
    + '<span class="net-bars"></span><span class="net-ms"></span></span>';
  return document;
}

function scheduler() {
  let now = 1_000_000;
  const timers = new Map<number, { callback: () => void; delayMs: number; active: boolean }>();
  let nextId = 1;
  const create = (callback: () => void, delayMs: number) => {
    const id = nextId++;
    timers.set(id, { callback, delayMs, active: true });
    return id;
  };
  const clear = (id: number) => { const timer = timers.get(id); if (timer) timer.active = false; };
  return {
    port: {
      now: () => now,
      setTimeout: create, clearTimeout: clear, setInterval: create, clearInterval: clear,
    },
    fire(delayMs: number) {
      const timer = [...timers.values()].reverse().find(
        (candidate) => candidate.active && candidate.delayMs === delayMs);
      if (!timer) return false;
      timer.active = false;
      timer.callback();
      return true;
    },
    advance(milliseconds: number) { now += milliseconds; },
  };
}

const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };

afterEach(() => {
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

describe('network indicator wired to the real availability monitor', () => {
  function environment() {
    const doc = badgeDocument();
    const clock = scheduler();
    const pushListeners = new Set<(reading: PushSocketReading | null) => void>();
    let healthOk = true;
    const quiet = { debug() {}, info() {}, warn() {}, error() {} } as unknown as Console;
    const monitor = createBackendAvailabilityMonitor({
      document: doc,
      browserEvents: window,
      schedule: clock.port as never,
      log: quiet,
      offlineIconHtml: () => '',
      isVisible: () => true,
      isNetworkOnline: () => true,
      probeHealth: async () => ({ ok: healthOk, status: healthOk ? 200 : 503 }) as never,
      subscribePushReading: (listener) => {
        pushListeners.add(listener as never);
        return () => pushListeners.delete(listener as never);
      },
      subscribePushReconnect: () => () => {},
      nudgePushConnection: () => {},
      probeStuckStreams: () => {},
      recoverOfflineConversations: () => {},
      revalidateOnResume: () => {},
      notifyRecovery: () => {},
      copy: new Proxy({}, { get: () => () => '' }) as never,
    });
    const connectPush = vi.fn();
    const indicator = createNetworkIndicator({
      document: doc,
      browserEvents: window,
      translate: keys,
      isBrowserOnline: () => true,
      subscribePush: (listener) => {
        pushListeners.add(listener);
        return () => pushListeners.delete(listener);
      },
      subscribeChatStreams: (listener) => { listener({ degraded: false }); },
      subscribeBackend: (listener) => monitor.subscribe(listener),
      connectPush,
      log: quiet,
    });
    monitor.start();
    indicator.start();
    const badge = doc.getElementById('netLatencyBadge') as HTMLElement;
    return {
      badge, monitor, indicator, connectPush, clock,
      push(reading: PushSocketReading) { for (const fn of [...pushListeners]) fn(reading); },
      setHealth(ok: boolean) { healthOk = ok; },
      label: () => badge.querySelector('.net-ms')?.textContent,
      lit: () => badge.querySelectorAll('.net-bar.lit').length,
    };
  }

  it('shows online when push works, reconnecting when only push is down, offline only when HTTP fails', async () => {
    const env = environment();
    expect(env.connectPush).toHaveBeenCalledTimes(1);
    expect(env.badge.querySelectorAll('.net-bar')).toHaveLength(4);

    env.push({ connected: true, ms: 60, state: 'good', downReason: null });
    expect(env.badge.dataset.state).toBe('good');
    expect(env.label()).toBe('60ms');
    expect(env.lit()).toBe(4);

    // The push handshake fails (e.g. a gateway refuses the upgrade) while
    // HTTP still answers: the server is reachable, so never "offline".
    env.push({ connected: false, ms: null, state: 'offline', downReason: 'handshake' });
    await flush();
    expect(env.monitor.snapshot().phase).toBe('online');
    expect(env.badge.dataset.state).toBe('reconnecting');
    expect(env.label()).toBe('#net.reconnecting');
    expect(env.badge.title).toContain('#net.pushReconnectingDesc');

    // The server really goes away: two failed health probes confirm it.
    env.setHealth(false);
    env.push({ connected: false, ms: null, state: 'offline', downReason: 'dropped' });
    await flush();
    expect(env.badge.dataset.state).toBe('reconnecting');
    expect(env.clock.fire(4_000)).toBe(true);
    await flush();
    expect(env.monitor.snapshot().phase).toBe('offline');
    expect(env.badge.dataset.state).toBe('offline');
    expect(env.label()).toBe('#net.offline');
    expect(env.badge.title).toBe('#net.title: #net.offlineDesc');
    expect(env.lit()).toBe(0);

    // Recovery: the server answers and the push socket reopens.
    env.setHealth(true);
    expect(env.clock.fire(5_000)).toBe(true);
    await flush();
    expect(env.monitor.snapshot().phase).toBe('online');
    expect(env.badge.dataset.state).toBe('reconnecting');
    env.push({ connected: true, ms: 80, state: 'good', downReason: null });
    expect(env.badge.dataset.state).toBe('good');

    env.indicator.destroy();
    env.monitor.destroy();
  });

  it('releases every subscription on destroy', () => {
    const env = environment();
    env.push({ connected: true, ms: 60 });
    env.indicator.destroy();
    env.push({ connected: false, downReason: 'dropped' });
    expect(env.badge.dataset.state).toBe('good');
    env.monitor.destroy();
  });
});
