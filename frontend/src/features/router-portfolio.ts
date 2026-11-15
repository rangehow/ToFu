/** Portfolio ownership (docs/ROUTER.md R10), the visible stop/pause switch for
 * proactive work: a compact disclosure the main coordinator's header hosts,
 * showing whether the daily review starts owner-authorized initiatives, how the
 * promised -> delivered ledger stands, and a switch to pause new starts. The
 * server's scheduler task is the only authority; this module reads once when
 * created and again when opened, starts no polling or timers, and never writes
 * the registry. Static labels carry data-i18n keys; the counts and last-review
 * lines repaint on `tofu:language-change`.
 */
import type { RouterPortfolioState } from '../api/router.generated';
import { createChoice } from '../ui';
import { t } from '../i18n';

export interface RouterPortfolioPorts {
  /** GET /api/v4/router/portfolio: passive. */
  read(signal?: AbortSignal): Promise<RouterPortfolioState>;
  /** POST /api/v4/router/portfolio/review: the owner's pause/resume choice. */
  setEnabled(enabled: boolean): Promise<RouterPortfolioState>;
}

type StaticKey = 'routerPortfolio.summaryOff' | 'routerPortfolio.summaryOn' | 'routerPortfolio.hint'
  | 'routerPortfolio.switch';

function localized<E extends HTMLElement>(element: E, key: StaticKey): E {
  element.dataset.i18n = key;
  element.textContent = t(key);
  return element;
}

const failure = (error: unknown) => (error instanceof Error ? error.message : String(error));

export function createRouterPortfolio(ports: RouterPortfolioPorts) {
  let alive = true, pending = false;
  let state: RouterPortfolioState | null = null;
  let reading: AbortController | null = null;

  const settings = document.createElement('details');
  settings.className = 'router-portfolio';
  const summary = localized(document.createElement('summary'), 'routerPortfolio.summaryOff');
  const body = document.createElement('div');
  body.className = 'router-portfolio-body';
  const hint = localized(document.createElement('p'), 'routerPortfolio.hint');
  const toggle = createChoice({ type: 'switch', label: '' });
  toggle.el.classList.add('router-portfolio-switch');
  localized(toggle.el.querySelector('span')!, 'routerPortfolio.switch');
  const counts = document.createElement('p');
  counts.className = 'router-portfolio-counts';
  const lastReview = document.createElement('p');
  lastReview.className = 'router-portfolio-last';
  const error = document.createElement('p');
  error.className = 'router-portfolio-error';
  error.setAttribute('role', 'alert');
  error.hidden = true;
  body.append(hint, toggle.el, counts, lastReview, error);
  settings.append(summary, body);

  function repaint() {
    if (!state) return;
    const numbers = state.counts;
    counts.textContent = t('routerPortfolio.counts', {
      proposed: numbers.proposed + numbers.awaitingOwner,
      running: numbers.started, delivered: numbers.delivered, negative: numbers.negative,
    });
    lastReview.textContent = state.review.lastRun
      ? t('routerPortfolio.lastReview', { time: state.review.lastRun, result: state.review.lastResult ?? '' })
      : t('routerPortfolio.noActivity');
  }

  function adopt(next: RouterPortfolioState) {
    if (!alive) return;
    state = next;
    localized(summary, next.review.enabled ? 'routerPortfolio.summaryOn' : 'routerPortfolio.summaryOff');
    toggle.input.checked = next.review.enabled;
    error.hidden = true;
    repaint();
  }

  async function read() {
    reading?.abort();
    reading = new AbortController();
    try {
      adopt(await ports.read(reading.signal));
    } catch {
      // A failed read leaves the switch showing the last known state; no retry loop.
    }
  }

  toggle.input.addEventListener('change', async () => {
    if (pending) {
      if (state) toggle.input.checked = state.review.enabled;
      return;
    }
    const wanted = toggle.input.checked;
    pending = true;
    toggle.input.disabled = true;
    try {
      adopt(await ports.setEnabled(wanted));
    } catch (cause) {
      if (!alive) return;
      if (state) toggle.input.checked = state.review.enabled;
      error.textContent = t('routerPortfolio.failed', { error: failure(cause) });
      error.hidden = false;
    } finally {
      pending = false;
      if (alive) toggle.input.disabled = false;
    }
  });
  settings.addEventListener('toggle', () => { if (settings.open) void read(); });
  const languageChange = () => { if (state) repaint(); };
  document.addEventListener('tofu:language-change', languageChange);
  void read();

  return {
    settings,
    destroy() {
      alive = false;
      reading?.abort();
      document.removeEventListener('tofu:language-change', languageChange);
      settings.remove();
    },
  };
}
