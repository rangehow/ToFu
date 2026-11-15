/**
 * Responsibility: the topbar network badge (#netLatencyBadge). A pure
 * projection (projectNetworkIndicator) of three existing verdict owners plus
 * a DOM controller (createNetworkIndicator) that subscribes to them.
 * Entry points: projectNetworkIndicator, createNetworkIndicator.
 * Dependencies (injected): the Push socket reading (push.js owns RTT, the
 * per-ping timeout and close verdicts), the conversation-stream aggregate
 * (core/connection-health.ts) and the backend-availability phase
 * (backend-availability-monitor.ts owns HTTP reachability). The badge owns no
 * clock, connection, endpoint or probe of its own.
 *
 * "Offline" means the server cannot be reached: only the availability owner
 * decides that. A Push socket that is down while HTTP still answers is a
 * reconnecting push channel, shown with the socket's real reason.
 */

import type { BackendAvailabilityPhase } from './backend-availability-monitor';

export type PushDownReason =
  | 'connecting' | 'handshake' | 'dropped' | 'timeout' | 'refused';

export interface PushSocketReading {
  readonly connected?: boolean;
  readonly ms?: number | null;
  readonly state?: string;
  readonly downReason?: PushDownReason | string | null;
}

export interface NetworkIndicatorInput {
  readonly push: PushSocketReading | null;
  readonly backendPhase: BackendAvailabilityPhase;
  readonly browserOnline: boolean;
  readonly chatStreamDegraded: boolean;
}

export type NetworkIndicatorState =
  | 'good' | 'ok' | 'poor' | 'reconnecting' | 'offline' | 'unknown';

export interface NetworkIndicatorView {
  readonly state: NetworkIndicatorState;
  readonly bars: number;
  readonly label: string;
  readonly title: string;
}

export type Translate = (key: string, params?: Record<string, unknown>) => string;

const BARS: Readonly<Record<NetworkIndicatorState, number>> = Object.freeze({
  good: 4, ok: 3, poor: 2, reconnecting: 1, offline: 0, unknown: 0,
});

const PUSH_REASONS: ReadonlySet<string> = new Set(
  ['connecting', 'handshake', 'dropped', 'timeout', 'refused']);

const FALLBACK: Readonly<Record<string, string>> = Object.freeze({
  'net.title': '网络延迟',
  'net.offline': '离线',
  'net.offlineDesc': '无法连接服务器，正在重试…',
  'net.offlineBrowserDesc': '本机网络已断开',
  'net.reconnecting': '重连中',
  'net.reconnectingDesc': '聊天连接正在重连，回复可能暂停',
  'net.pushReconnectingDesc': '服务器可达；实时推送通道正在重连（{reason}）',
  'net.pushUnavailable': '推送受阻',
  'net.pushRefusedDesc': '服务器可达，但拒绝了实时推送通道，不会自动重连',
  'net.pushReason.connecting': '正在建立连接',
  'net.pushReason.handshake': '握手未成功，可能被代理或鉴权拒绝',
  'net.pushReason.dropped': '连接中断',
  'net.pushReason.timeout': '心跳超时',
  'net.state.good': '良好',
  'net.state.ok': '一般',
  'net.state.poor': '较差',
});

function text(translate: Translate, key: string, params?: Record<string, unknown>): string {
  let value = '';
  try {
    value = translate(key, params);
  } catch {
    value = '';
  }
  if (value && value !== key) return value;
  let fallback = FALLBACK[key] ?? key;
  for (const [name, param] of Object.entries(params ?? {})) {
    fallback = fallback.replace(`{${name}}`, String(param));
  }
  return fallback;
}

function classify(ms: number | null | undefined): NetworkIndicatorState {
  if (ms == null || !Number.isFinite(ms)) return 'unknown';
  if (ms < 150) return 'good';
  if (ms < 400) return 'ok';
  return 'poor';
}

/** Pure state machine for the badge; see the module header for its rules. */
export function projectNetworkIndicator(
  input: NetworkIndicatorInput,
  translate: Translate,
): NetworkIndicatorView {
  const head = text(translate, 'net.title');
  const view = (state: NetworkIndicatorState, label: string, detail: string) => ({
    state, bars: BARS[state], label, title: `${head}: ${detail}`,
  });

  if (input.backendPhase === 'offline') {
    return view('offline', text(translate, 'net.offline'), text(
      translate, input.browserOnline ? 'net.offlineDesc' : 'net.offlineBrowserDesc'));
  }

  const push = input.push;
  if (!push || push.connected !== true) {
    const raw = String(push?.downReason ?? (push?.state === 'timeout' ? 'timeout' : 'connecting'));
    const reason = PUSH_REASONS.has(raw) ? raw : 'connecting';
    if (reason === 'refused') {
      return view('reconnecting', text(translate, 'net.pushUnavailable'),
        text(translate, 'net.pushRefusedDesc'));
    }
    return view('reconnecting', text(translate, 'net.reconnecting'), text(
      translate, 'net.pushReconnectingDesc',
      { reason: text(translate, `net.pushReason.${reason}`) }));
  }

  const ms = typeof push.ms === 'number' ? push.ms : null;
  if (input.chatStreamDegraded) {
    const base = ms == null ? '' : `${ms}ms — `;
    return view('poor', text(translate, 'net.reconnecting'),
      base + text(translate, 'net.reconnectingDesc'));
  }
  const state = classify(ms);
  if (ms == null) return view(state, '—', '—');
  return view(state, `${ms}ms`, `${ms}ms (${text(translate, `net.state.${state}`)})`);
}

type Unsubscribe = (() => void) | void;

export interface NetworkIndicatorPorts {
  readonly document: Document;
  readonly browserEvents: EventTarget;
  readonly translate: Translate;
  isBrowserOnline(): boolean;
  subscribePush(listener: (reading: PushSocketReading | null) => void): Unsubscribe;
  subscribeChatStreams(listener: (health: { degraded?: boolean } | null) => void): Unsubscribe;
  subscribeBackend(listener: (phase: BackendAvailabilityPhase) => void): Unsubscribe;
  /** Ask the Push owner to open its socket so readings can flow. */
  connectPush(): unknown;
  log: Pick<Console, 'warn' | 'debug'>;
}

export interface NetworkIndicator {
  start(): void;
  destroy(): void;
}

const BAR_COUNT = 4;

export function createNetworkIndicator(ports: NetworkIndicatorPorts): NetworkIndicator {
  const input = {
    push: null as PushSocketReading | null,
    backendPhase: 'online' as BackendAvailabilityPhase,
    browserOnline: true,
    chatStreamDegraded: false,
  };
  let element: HTMLElement | null = null;
  let bars: HTMLElement | null = null;
  let label: HTMLElement | null = null;
  let cleanups: Array<() => void> = [];

  const render = (): void => {
    if (!element || !bars || !label) return;
    input.browserOnline = ports.isBrowserOnline();
    const view = projectNetworkIndicator(input, ports.translate);
    element.dataset.state = view.state;
    Array.from(bars.children).forEach((bar, index) => {
      bar.classList.toggle('lit', index < view.bars);
    });
    label.textContent = view.label;
    element.title = view.title;
  };

  const own = (unsubscribe: Unsubscribe): void => {
    if (typeof unsubscribe === 'function') cleanups.push(unsubscribe);
  };

  const start = (): void => {
    if (element) return;
    element = ports.document.getElementById('netLatencyBadge');
    bars = element?.querySelector<HTMLElement>('.net-bars') ?? null;
    label = element?.querySelector<HTMLElement>('.net-ms') ?? null;
    if (!element || !bars || !label) {
      element = null;
      ports.log.debug('[NetworkIndicator] badge markup absent; indicator inert');
      return;
    }
    bars.replaceChildren(...Array.from({ length: BAR_COUNT }, () => {
      const bar = ports.document.createElement('span');
      bar.className = 'net-bar';
      return bar;
    }));
    own(ports.subscribePush((reading) => { input.push = reading; render(); }));
    own(ports.subscribeChatStreams((health) => {
      input.chatStreamDegraded = health?.degraded === true;
      render();
    }));
    own(ports.subscribeBackend((phase) => { input.backendPhase = phase; render(); }));
    for (const type of ['online', 'offline']) {
      ports.browserEvents.addEventListener(type, render);
      cleanups.push(() => ports.browserEvents.removeEventListener(type, render));
    }
    render();
    try {
      ports.connectPush();
    } catch (error: unknown) {
      ports.log.warn('[NetworkIndicator] push connect request failed', error);
    }
  };

  const destroy = (): void => {
    for (const cleanup of cleanups.splice(0).reverse()) {
      try {
        cleanup();
      } catch (error: unknown) {
        ports.log.debug('[NetworkIndicator] subscription cleanup failed', error);
      }
    }
    cleanups = [];
    element = null;
    bars = null;
    label = null;
  };

  return Object.freeze({ start, destroy });
}
