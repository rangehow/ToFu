/** Browser relay for nodes behind an SSO edge: this signed-in tab carries a desktop node's polls.
 *
 * A node that cannot pass the Home's login wall opens a loopback broker on 127.0.0.1:15180–15189
 * (lib/desktop_agent/_browser_relay.py). This page takes each job from the broker (`/v1/take`), sends it to
 * the Home with the browser's own cookies and hands the exact status and body back (`/v1/result`). Only the
 * Home's poll and pair paths are forwarded; cookies never leave the browser. The broker protocol and the
 * relayed bodies are the node owner's contract and are forwarded verbatim. Discovery runs only after an
 * explicit start (the Devices row action or the `#tofu-agent-relay` deep link the node opens), because
 * probing localhost can raise the browser's local-network permission prompt; it stops when the watch ends.
 * Entry points: ensureRelay, subscribeRelay, relayStatusText. Dependencies: api/transport, i18n.
 */
import { request, resolvePath } from '../../api/transport';
import { t } from '../../i18n';

export type RelayState = 'idle' | 'scanning' | 'notfound' | 'forwarding' | 'connected' | 'error';

/** The node's broker port range (lib/desktop_agent/_browser_relay.py). */
const BROKER_PORT_FIRST = 15180;
const BROKER_PORT_LAST = 15189;
/** Broker deadlines of HEAD's relay: status probe, one held take, a result post. */
const PROBE_MS = 650;
const TAKE_MS = 13000;
const RESULT_MS = 5000;
/** Rescan spacing while a started watch has found no broker. */
const RESCAN_MS = 1800;
export const DEEP_LINK_WATCH_MS = 30 * 60 * 1000;

const listeners = new Set<(state: RelayState) => void>();
let state: RelayState = 'idle';
let base = '';
let running = false;
let watchUntil = 0;
let watchTimer: ReturnType<typeof setTimeout> | null = null;

function setState(next: RelayState): void {
  if (state === next) return;
  state = next;
  for (const listener of [...listeners]) listener(state);
}

export function subscribeRelay(listener: (state: RelayState) => void): () => void {
  listeners.add(listener);
  listener(state);
  return () => { listeners.delete(listener); };
}

export function relayStatusText(value: RelayState): string {
  switch (value) {
    case 'scanning': return t('devicesPage.relay.scanning');
    case 'notfound': return t('devicesPage.relay.notFound');
    case 'forwarding': return t('devicesPage.relay.forwarding');
    case 'connected': return t('devicesPage.relay.connected');
    case 'error': return t('devicesPage.relay.error');
    case 'idle': return '';
  }
}

function localFetch(url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  // Chromium's Local Network Access names the target address space explicitly.
  const options = { mode: 'cors', credentials: 'omit', cache: 'no-store', targetAddressSpace: 'local', ...init,
    signal: controller.signal } as RequestInit;
  return fetch(url, options).finally(() => clearTimeout(timer));
}

function absolute(route: string): string {
  try { return new URL(resolvePath(route), window.location.href).href; } catch { return ''; }
}

async function discover(): Promise<string> {
  const probes: Array<Promise<string>> = [];
  for (let port = BROKER_PORT_FIRST; port <= BROKER_PORT_LAST; port++) {
    const candidate = `http://127.0.0.1:${port}`;
    probes.push(localFetch(`${candidate}/v1/status`, {}, PROBE_MS)
      .then(async response => {
        if (!response.ok) return '';
        const body = await response.json() as { kind?: unknown };
        return body?.kind === 'tofu-agent-browser-relay' ? candidate : '';
      })
      .catch(() => ''));
  }
  return (await Promise.all(probes)).find(Boolean) || '';
}

function returnResult(broker: string, id: unknown, status: number, body: string): Promise<Response> {
  return localFetch(`${broker}/v1/result`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, status, body }),
  }, RESULT_MS);
}

async function forward(job: { url?: string; payload?: unknown; headers?: Record<string, string> }, isPair: boolean) {
  const secret = job.headers?.['X-Bridge-Secret'];
  return request<Response>(isPair ? '/api/desktop/pair' : '/api/desktop/poll', {
    method: 'POST',
    json: job.payload || {},
    headers: { Accept: 'application/json', ...(!isPair && secret ? { 'X-Bridge-Secret': secret } : {}) },
    credentials: 'include',
    parse: 'response',
  });
}

async function loop(broker: string): Promise<void> {
  if (running) return;
  running = true;
  base = broker;
  setState('forwarding');
  try {
    while (base === broker) {
      const take = await localFetch(`${broker}/v1/take`, {}, TAKE_MS);
      if (take.status === 204) continue;
      if (!take.ok) throw new Error(`local relay HTTP ${take.status}`);
      const job = await take.json() as { id?: unknown; url?: string; payload?: unknown; headers?: Record<string, string> };
      let target = '';
      try { target = new URL(job.url || '', window.location.href).href; } catch { target = ''; }
      const isPair = !!target && target === absolute('/api/desktop/pair');
      if (!isPair && target !== absolute('/api/desktop/poll')) {
        await returnResult(broker, job.id, 409, JSON.stringify({ error: 'relay_target_mismatch' }));
        continue;
      }
      try {
        const remote = await forward(job, isPair);
        await returnResult(broker, job.id, remote.status, await remote.text());
        setState(remote.ok ? 'connected' : 'error');
      } catch {
        await returnResult(broker, job.id, 502, JSON.stringify({ error: 'browser_gateway_unreachable' }))
          .catch(() => undefined);
        setState('error');
      }
    }
  } catch {
    setState('error');
  } finally {
    running = false;
    if (base === broker) base = '';
    if (Date.now() < watchUntil) schedule(0);
  }
}

function schedule(delay: number): void {
  if (watchTimer) return;
  watchTimer = setTimeout(beat, delay);
}

function beat(): void {
  watchTimer = null;
  if (running || Date.now() >= watchUntil) return;
  void discover().then(found => {
    if (found) { void loop(found); return; }
    if (state === 'scanning') setState('notfound');
    if (Date.now() < watchUntil) schedule(RESCAN_MS);
  });
}

/** Start (or extend) the relay watch. An explicit start always answers with a visible state. */
export function ensureRelay(durationMs = DEEP_LINK_WATCH_MS): void {
  watchUntil = Math.max(watchUntil, Date.now() + durationMs);
  if (state === 'idle' || state === 'notfound' || state === 'error') setState('scanning');
  if (!running) schedule(0);
}
