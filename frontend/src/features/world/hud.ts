/** Office HUD: one quiet top bar over the floor. The brand with the push connection state, the digest of
 * everything routine (who is at work, reports waiting, people off desk) beside the few emphasized counts
 * (decisions, exceptions, deliverables), the clock, and the always-visible switch to the classic Session UI.
 * Entry point: createHud. Dependencies: i18n, ui button factory. */
import { t } from '../../i18n';
import { createButton } from '../../ui';

export type PushState = 'live' | 'connecting' | 'offline';
export interface HudView {
  push: PushState; now: number; working: number; routine: number; reports: number;
  decisions: number; exceptions: number; deliverables: number;
  /** The office's first reads have not all answered, so what it shows may still change. */
  loading: boolean;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, parent?: HTMLElement): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  parent?.append(node);
  return node;
}
/** The clock speaks the interface language, not the browser's. */
const clock = (ms: number) => new Date(ms).toLocaleTimeString(document.documentElement.lang || [], { hour: '2-digit', minute: '2-digit' });

export function createHud(ports: { classic(): void }) {
  const bar = el('header', 'w-hud');
  const brand = el('div', 'w-brand', bar);
  const dot = el('i', 'w-live', brand);
  el('b', '', brand).textContent = 'Tofu';
  const digest = el('p', 'w-digest', bar);
  digest.setAttribute('aria-live', 'polite');
  const chips = {
    loading: el('span', 'w-chip', digest),
    decisions: el('span', 'w-chip', digest), exceptions: el('span', 'w-chip', digest),
    deliverables: el('span', 'w-chip', digest), working: el('span', 'w-chip', digest),
    reports: el('span', 'w-chip', digest), routine: el('span', 'w-chip', digest),
  };
  chips.loading.dataset.tone = 'loading';
  chips.decisions.dataset.tone = 'decision';
  chips.exceptions.dataset.tone = 'exception';
  chips.deliverables.dataset.tone = 'deliverable';
  const time = el('time', 'w-clock', bar);
  const classic = createButton({ label: t('world.hud.classic'), size: 'sm', onClick: () => ports.classic() });
  classic.classList.add('w-classic');
  classic.dataset.worldClassic = '';
  classic.title = t('world.hud.classicTitle');
  bar.append(classic);

  return {
    element: bar,
    update(view: HudView) {
      dot.dataset.state = view.push;
      dot.title = t(view.push === 'live' ? 'world.sync.live' : view.push === 'connecting' ? 'world.sync.connecting' : 'world.sync.offline');
      const set = (node: HTMLElement, count: number, text: string) => {
        node.hidden = count === 0;
        if (node.textContent !== text) node.textContent = text;
      };
      set(chips.loading, view.loading ? 1 : 0, t('common.loading'));
      set(chips.decisions, view.decisions, t('world.hud.decisions', { count: view.decisions }));
      set(chips.exceptions, view.exceptions, t('world.hud.exceptions', { count: view.exceptions }));
      set(chips.deliverables, view.deliverables, t('world.hud.deliverables', { count: view.deliverables }));
      set(chips.working, 1, t('world.hud.working', { count: view.working }));
      set(chips.reports, view.reports, t('world.hud.reports', { count: view.reports }));
      set(chips.routine, view.routine, t('world.hud.routine', { count: view.routine }));
      const text = clock(view.now);
      if (time.textContent !== text) time.textContent = text;
      time.dateTime = new Date(view.now).toISOString();
    },
  };
}
