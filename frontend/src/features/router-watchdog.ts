/** Router Watchdog (docs/ROUTER.md R7), shown to the owner as health checks
 * (故障巡检). Health checks are on by default: the server ensures them with the
 * main coordinator (owner decision 2026-11-17), so the fold the main
 * coordinator's header hosts shows their state, one sentence on what they do
 * and a way into the Watchdog conversation, with nothing to switch or
 * configure; the owner changes or stops the periodic checks by asking that
 * conversation. The module also owns a one-line header on the Watchdog
 * conversation with a way back. Other surfaces call the same `ensure` before opening the Watchdog
 * conversation (e.g. the studio's compute-stall dialog). The Watchdog has no
 * sidebar entry: the owner hears from it through the main coordinator. The
 * server binding and the scheduler task are the only authorities; this module
 * keeps the last read state for presentation, starts no polling or timers and
 * reads passively when created and on `reload` (after the main coordinator's
 * ensure). Static labels carry data-i18n attributes, so the i18n runtime
 * translates them once the catalog is ready and on every language switch.
 */
import type { RouterWatchdogState } from '../api/router.generated';
import { createButton, createDisclosureSummary } from '../ui';
import { t } from '../i18n';
import { setStableHidden } from '../conversation/ui/renderers/stable-dom';

export interface RouterWatchdogPorts {
  /** GET /api/v4/router/watchdog: passive, never creates the Watchdog. */
  read(signal?: AbortSignal): Promise<RouterWatchdogState>;
  /** POST /api/v4/router/watchdog: ensures health checks (the Watchdog and its review). */
  ensure(): Promise<RouterWatchdogState>;
  open(conversationId: string): void;
  /** Returns to the main coordinator conversation. */
  back(): void;
  activeConversationId(): string;
  notifyError(message: string): void;
  /** The Watchdog path or its conversation became known or changed. */
  changed?(): void;
}

export interface RouterWatchdogHosts {
  /** Receives the compact header of the Watchdog conversation. */
  header: HTMLElement;
}

// Placeholder-free labels only: the runtime re-applies data-i18n keys without parameters.
type StaticKey = 'routerWatchdog.entry' | 'routerWatchdog.summaryOff' | 'routerWatchdog.summaryOn'
  | 'routerWatchdog.summaryFaultsOnly' | 'routerWatchdog.hint' | 'routerWatchdog.open'
  | 'routerWatchdog.headerSubtitle' | 'routerWatchdog.back';

/** Current text now; the i18n runtime re-applies the key on ready() and on switches. */
function localized<E extends HTMLElement>(element: E, key: StaticKey): E {
  element.dataset.i18n = key;
  element.textContent = t(key);
  return element;
}

const failure = (error: unknown) => (error instanceof Error ? error.message : String(error));

export function createRouterWatchdogEntry(ports: RouterWatchdogPorts, hosts: RouterWatchdogHosts) {
  let alive = true, pending = false;
  let state: RouterWatchdogState | null = null;
  // The Watchdog path, known from any read (also while unbound).
  let location = '';
  let reading: AbortController | null = null;

  // A fold like its neighbours in the main coordinator's header: the state, what it does, a way in.
  const settings = document.createElement('details');
  settings.className = 'router-watchdog';
  const summary = localized(createDisclosureSummary(document), 'routerWatchdog.entry');
  const body = document.createElement('div');
  body.className = 'router-watchdog-body';
  const hint = localized(document.createElement('p'), 'routerWatchdog.hint');
  const open = localized(createButton({ label: '', size: 'sm', variant: 'ghost' }), 'routerWatchdog.open');
  body.append(hint, open);
  settings.append(summary, body);

  const header = document.createElement('header');
  header.className = 'router-watchdog-header';
  header.hidden = true;
  const subtitle = localized(document.createElement('span'), 'routerWatchdog.headerSubtitle');
  subtitle.className = 'router-watchdog-header-subtitle';
  const back = localized(createButton({ label: '', size: 'sm', variant: 'ghost' }), 'routerWatchdog.back');
  header.append(subtitle, back);
  hosts.header.append(header);

  function adopt(next: RouterWatchdogState) {
    if (!alive) return;
    const identityChanged = next.projectPath !== location
      || (next.availability === 'available' ? next.conversationId : '') !== (state?.conversationId ?? '');
    state = next.availability === 'available' ? next : null;
    location = next.projectPath;
    localized(summary, !state ? 'routerWatchdog.summaryOff'
      : state.review.enabled ? 'routerWatchdog.summaryOn' : 'routerWatchdog.summaryFaultsOnly');
    refresh();
    if (identityChanged) ports.changed?.();
  }

  // Runs on every sidebar render: re-hiding still rewrites the attribute.
  function refresh() {
    if (!alive) return;
    setStableHidden(header, !state || ports.activeConversationId() !== state.conversationId);
  }

  /** Ensures health checks and adopts their state; resolves the Watchdog
   * conversation ID or rejects with the localized reason it is unavailable. */
  async function ensure(): Promise<string> {
    let ensured: RouterWatchdogState;
    try {
      ensured = await ports.ensure();
    } catch (error) {
      throw new Error(t('routerWatchdog.unavailable', { error: failure(error) }));
    }
    if (alive) {
      // The ensure's answer is newer than a read still in flight.
      reading?.abort(); reading = null;
      adopt(ensured);
    }
    if (ensured.availability !== 'available') throw new Error(t('routerWatchdog.unavailable', { error: ensured.availability }));
    return ensured.conversationId;
  }

  // A known Watchdog opens at once; an unknown one is ensured first, and its
  // answer opens it only if the owner is still where they clicked.
  open.addEventListener('click', () => {
    if (state) { ports.open(state.conversationId); return; }
    if (pending) return;
    pending = true;
    open.disabled = true; open.setAttribute('aria-busy', 'true');
    const expected = ports.activeConversationId();
    ensure().then((id) => {
      if (alive && ports.activeConversationId() === expected) ports.open(id);
    }, (error) => {
      if (alive) ports.notifyError(failure(error));
    }).finally(() => {
      pending = false;
      if (alive) { open.disabled = false; open.removeAttribute('aria-busy'); }
    });
  });
  back.addEventListener('click', () => ports.back());

  // A passive read; a failure keeps the last known state until the owner acts.
  function read() {
    reading?.abort();
    const current = reading = new AbortController();
    ports.read(current.signal).then((next) => { if (reading === current) adopt(next); }, () => undefined);
  }
  read();

  return {
    /** The fold the main coordinator's header hosts. */
    settings,
    refresh,
    ensure,
    /** Reads again: ensuring the main coordinator turns health checks on. */
    reload: read,
    /** The Watchdog path ('' until a read answers); a coordinator there is the Watchdog. */
    location: () => location,
    /** The bound Watchdog conversation the header marks ('' unless available). */
    conversationId: () => state?.conversationId ?? '',
    destroy() {
      alive = false; reading?.abort(); settings.remove(); header.remove();
    },
  };
}
