/** Settings subscription overview → detail navigation. Provider presenters still own all status and writes. */
import {brandIconHtml} from '../../core/model-brand-icons';
import {t} from '../../i18n';
import {createButton} from '../../ui';
import {setCodexAccountDetailActive} from './oauth-accounts';
import {prepareAntigravityDetail} from './antigravity-card';
import {currentSettingsReadSession} from './panel-read-session';

const byId = (id: string) => document.getElementById(id);
let unmount: (() => void) | undefined;

export function syncSubscriptionTab(tab: string): void {
  if (tab !== 'oauth') { unmount?.(); return; }
  if (unmount) return;
  const session = currentSettingsReadSession();
  const overview = byId('oauthSubscriptionOverview'), detail = byId('oauthSubscriptionDetail');
  const signInTiles = byId('oauthSignInTiles'), planTiles = byId('oauthPlanTiles');
  const planCards = byId('oauthPlanKeyCards'), backSlot = byId('oauthSubscriptionBack');
  if (!session?.isCurrent() || !overview || !detail || !signInTiles || !planTiles || !planCards || !backSlot) return;
  const staticCards = [...detail.querySelectorAll<HTMLElement>(':scope > [data-subscription-brand]')];
  mount({overview, detail, signInTiles, planTiles, planCards, backSlot, staticCards}, session.scope.signal);
}

interface Surface {
  overview: HTMLElement;
  detail: HTMLElement;
  signInTiles: HTMLElement;
  planTiles: HTMLElement;
  planCards: HTMLElement;
  backSlot: HTMLElement;
  staticCards: HTMLElement[];
}

type OpenDetail = (card: HTMLElement, returnTo: HTMLButtonElement) => void;

function tile(card: HTMLElement, open: OpenDetail, observer: ReturnType<typeof createMirrors>): HTMLButtonElement {
  const name = card.querySelector<HTMLElement>('.oauth-provider-name')!;
  const status = card.querySelector<HTMLElement>('.oauth-status-badge')!;
  const button = createButton({label: name.textContent?.trim() ?? '', variant: 'ghost', wrap: true,
    onClick: () => open(card, button)});
  button.classList.add('oauth-subscription-tile');
  button.setAttribute('aria-controls', card.id);
  const icon = document.createElement('span');
  icon.className = 'oauth-subscription-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.innerHTML = brandIconHtml(card.dataset.subscriptionBrand, 24);
  const title = document.createElement('span');
  title.className = 'oauth-subscription-name';
  title.id = `${card.id}-tile-name`;
  const badge = document.createElement('span');
  badge.id = `${card.id}-tile-status`;
  button.setAttribute('aria-labelledby', title.id);
  button.setAttribute('aria-describedby', badge.id);
  const arrow = document.createElement('span');
  arrow.className = 'oauth-subscription-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  button.replaceChildren(icon, title, badge, arrow);
  const sync = () => {
    title.textContent = name.textContent;
    badge.className = status.className;
    badge.textContent = status.textContent;
  };
  sync();
  observer.watch(status, sync);
  observer.watch(name, sync);
  return button;
}

// Subscribe only to the existing rendered name and status, never to credentials,
// details, network state, or the provider commands that publish them.
function createMirrors() {
  const copies = new WeakMap<Node, () => void>();
  const observer = new MutationObserver(mutations => {
    const affected = new Set<() => void>();
    for (const {target, type} of mutations) {
      const node = type === 'characterData' ? target.parentNode : target;
      const sync = node && copies.get(node);
      if (sync) affected.add(sync);
    }
    for (const sync of affected) sync();
  });
  return {
    watch(node: HTMLElement, sync: () => void) {
      copies.set(node, sync);
      observer.observe(node, {attributes: true, attributeFilter: ['class'], childList: true,
        characterData: true, subtree: true});
    },
    disconnect: () => observer.disconnect(),
  };
}

function hydrateProviderActions(detail: HTMLElement): void {
  // Keep the retained nodes, IDs and bound OAuth handlers. Only migrate the
  // hero buttons to the shared control styles; inline auth flows stay intact.
  for (const button of detail.querySelectorAll<HTMLButtonElement>('.oauth-provider-actions button.btn-small')) {
    const variant = button.classList.contains('btn-danger') ? 'danger'
      : button.classList.contains('btn-primary') ? 'primary' : 'secondary';
    button.className = createButton({label: '', variant, size: 'sm', wrap: true}).className;
  }
}

function mount(surface: Surface, signal: AbortSignal): void {
  const {overview, detail, signInTiles, planTiles, planCards, backSlot, staticCards} = surface;
  let selected: HTMLElement | null = null;
  let returnTo: HTMLButtonElement | null = null;
  const staticBadges = createMirrors();
  const planBadges = createMirrors();
  const scroller = overview.closest<HTMLElement>('.settings-content');
  let overviewScroll = 0;

  function showOverview(focus: boolean) {
    setCodexAccountDetailActive(false);
    if (selected) selected.hidden = true;
    selected = null;
    detail.hidden = true;
    overview.hidden = false;
    if (focus && scroller) scroller.scrollTop = overviewScroll;
    if (focus) {
      const target = returnTo?.isConnected ? returnTo : signInTiles.querySelector('button');
      target?.focus({preventScroll: true});
    }
    returnTo = null;
  }

  function open(card: HTMLElement, button: HTMLButtonElement) {
    if (!button.isConnected || !card.isConnected || overview.hidden || signal.aborted) return;
    overviewScroll = scroller?.scrollTop ?? 0;
    returnTo = button;
    selected = card;
    overview.hidden = true;
    detail.hidden = false;
    card.hidden = false;
    if (card.id === 'oauthDetailCodex') setCodexAccountDetailActive(true);
    if (card.id === 'oauthDetailAntigravity') prepareAntigravityDetail();
    if (scroller) scroller.scrollTop = 0;
    card.querySelector<HTMLElement>('.oauth-provider-name')?.focus({preventScroll: true});
  }

  const back = createButton({label: t('settings.oauthTitle'), variant: 'ghost', onClick: () => showOverview(true)});
  const backward = document.createElement('span');
  backward.setAttribute('aria-hidden', 'true');
  backward.textContent = '←';
  back.prepend(backward);
  backSlot.replaceChildren(back);
  hydrateProviderActions(detail);
  for (const card of staticCards) {
    const icon = card.querySelector<HTMLElement>('[data-subscription-detail-icon]');
    if (icon) icon.innerHTML = brandIconHtml(card.dataset.subscriptionBrand, 20);
  }
  signInTiles.replaceChildren(...staticCards.map(card => tile(card, open, staticBadges)));

  function syncPlans() {
    const cards = [...planCards.querySelectorAll<HTMLElement>(':scope > [data-subscription-brand]')];
    const oldTile = document.activeElement;
    const focusedPlan = oldTile && planTiles.contains(oldTile) ? oldTile.getAttribute('aria-controls') : null;
    planBadges.disconnect();
    const buttons = cards.map(card => tile(card, open, planBadges));
    planTiles.replaceChildren(...buttons);
    const replacement = buttons.find(button => button.getAttribute('aria-controls') === (selected?.id ?? focusedPlan));
    if (selected && !selected.isConnected) {
      selected = cards.find(card => card.id === selected?.id) ?? null;
      returnTo = replacement ?? null;
      if (!selected) showOverview(true);
      else {
        selected.hidden = false;
        if (document.activeElement === document.body) {
          selected.querySelector<HTMLElement>('.oauth-provider-name')?.focus({preventScroll: true});
        }
      }
    } else if (focusedPlan) replacement?.focus({preventScroll: true});
  }
  const plans = new MutationObserver(syncPlans);
  plans.observe(planCards, {childList: true});
  syncPlans();
  unmount = () => {
    showOverview(false);
    staticBadges.disconnect();
    planBadges.disconnect();
    plans.disconnect();
    signInTiles.replaceChildren();
    planTiles.replaceChildren();
    backSlot.replaceChildren();
    signal.removeEventListener('abort', unmount!);
    unmount = undefined;
  };
  signal.addEventListener('abort', unmount, {once: true});
}
