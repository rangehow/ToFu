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
import { apiFailure, type ApiFailure } from '../api/errors';
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
  let alive = true, generation = 0;
  let pending: number | null = null;
  // Grouping keeps the last known identity; only current evidence enables its Open shortcut.
  let state: RouterWatchdogState | null = null;
  let availability: RouterWatchdogState['availability'] | null = null;
  let actionWithdrawn = false;
  let readError: ApiFailure | null = null;
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
  const error = document.createElement('p');
  error.setAttribute('role', 'alert');
  error.hidden = true;
  body.append(hint, open, error);
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
    availability = next.availability;
    reading = null;
    actionWithdrawn = availability === 'unavailable';
    readError = null;
    location = next.projectPath;
    render();
    if (identityChanged) ports.changed?.();
  }

  function render() {
    if (!alive) return;
    const denied = readError?.status === 403 || readError?.code === 'forbidden';
    if (availability === null || availability === 'unavailable') {
      delete summary.dataset.i18n;
      summary.textContent = `${t('routerWatchdog.entry')} · ${t(reading ? 'common.loading'
        : denied ? 'err.k.permission.chip' : 'rich.unknown')}`;
    } else localized(summary, !state ? 'routerWatchdog.summaryOff'
      : state.review.enabled ? 'routerWatchdog.summaryOn' : 'routerWatchdog.summaryFaultsOnly');
    open.hidden = actionWithdrawn;
    open.disabled = pending !== null || actionWithdrawn;
    const evidence = readError ? [readError.status ? `HTTP ${readError.status}` : '', readError.code]
      .filter(value => value !== null && value !== '').join(' · ') : '';
    const detail = readError ? `${readError.message}${evidence ? ` (${evidence})` : ''}`
      : availability === 'unavailable' ? 'unavailable' : '';
    error.textContent = detail ? denied ? `${t('err.k.permission.chip')}: ${detail}`
      : t('routerWatchdog.unavailable', { error: detail }) : '';
    error.hidden = !detail;
    refresh();
  }

  // Runs on every sidebar render: re-hiding still rewrites the attribute.
  function refresh() {
    if (!alive) return;
    setStableHidden(header, availability !== 'available' || !state || ports.activeConversationId() !== state.conversationId);
  }

  function beginRequest() {
    const own = ++generation;
    reading?.abort(); reading = null;
    pending = null;
    open.removeAttribute('aria-busy');
    return own;
  }

  /** Ensures health checks and adopts their state; a newer read or ensure
   * supersedes the result and rejects with AbortError instead of a stale ID. */
  async function ensure(): Promise<string> {
    const own = beginRequest();
    render();
    const assertCurrent = () => {
      if (!alive || own !== generation) throw new DOMException('Superseded health-check request', 'AbortError');
    };
    assertCurrent();
    let ensured: RouterWatchdogState;
    try {
      ensured = await ports.ensure();
    } catch (error) {
      assertCurrent();
      throw new Error(t('routerWatchdog.unavailable', { error: failure(error) }));
    }
    assertCurrent();
    adopt(ensured);
    assertCurrent();
    if (ensured.availability !== 'available') throw new Error(t('routerWatchdog.unavailable', { error: ensured.availability }));
    return ensured.conversationId;
  }

  // A known Watchdog opens at once; an unknown one is ensured first, and its
  // answer opens it only if the owner is still where they clicked.
  open.addEventListener('click', () => {
    if (!alive || open.disabled || open.hidden) return;
    if (state) { ports.open(state.conversationId); return; }
    if (pending !== null) return;
    const expected = ports.activeConversationId();
    const own = generation + 1, request = ensure();
    if (alive && own === generation) {
      pending = own;
      open.disabled = true; open.setAttribute('aria-busy', 'true');
    }
    request.then((id) => {
      if (alive && own === generation && ports.activeConversationId() === expected) ports.open(id);
    }, (error) => {
      if (alive && own === generation) ports.notifyError(failure(error));
    }).finally(() => {
      if (pending !== own) return;
      pending = null;
      if (alive) { open.removeAttribute('aria-busy'); render(); }
    });
  });
  back.addEventListener('click', () => ports.back());

  function read() {
    if (!alive) return;
    const own = beginRequest();
    const current = reading = new AbortController();
    actionWithdrawn ||= state !== null;
    availability = null; readError = null;
    render();
    ports.read(current.signal).then((next) => {
      if (alive && own === generation && reading === current) adopt(next);
    }, (reason) => {
      if (!alive || own !== generation || reading !== current) return;
      reading = null; readError = apiFailure(reason);
      actionWithdrawn ||= readError.status === 403 || readError.code === 'forbidden';
      render();
    });
  }
  window.addEventListener('tofu:language-change', render);
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
    /** Last known bound identity for grouping, not the current Open availability. */
    conversationId: () => state?.conversationId ?? '',
    destroy() {
      alive = false; generation++; reading?.abort();
      window.removeEventListener('tofu:language-change', render);
      settings.remove(); header.remove();
    },
  };
}
