/** Devices page: the Home card, the fleet grouped by kind with live health and effective capabilities, and
 * the topology explainer. Detail and onboarding open as a drawer and a wizard over the list.
 *
 * Everything shown is the shared store's projection; the page never fetches by itself, so it updates exactly
 * when the Home reports a change or the push channel reconnects. Stable control keys let focus survive
 * an update. Entry point: mountDevicesPage. Dependencies: the devices store, ui kit, i18n.
 */
import { t } from '../../i18n';
import {
  createAlert, createButton, createDisclosureSummary, createDropdown, createEmptyState, createSpinner,
} from '../../ui';
import { clockTime, el, fullTime, svg } from './dom';
import {
  ageLabel, kindGroupLabel, kindShortLabel, routeLabel, stateLabel, tierLabel,
} from './labels';
import type { DevicesSnapshot, DevicesStore } from './live';
import {
  KIND_ORDER, TIERS, summarizeFleet, tierChipState, type DeviceKind, type DeviceRow, type FleetSources,
} from './model';
import { renderTopology } from './topology';
import { relayStatusText, subscribeRelay, type RelayState } from './relay';

export interface DevicesPagePorts {
  backToChat(): void;
  openDevice(key: string): void;
  openWizard(confirmNodeId: string | undefined, focusKey: string): void;
  ensureRelay(): void;
  now?(): number;
}

export interface DevicesPage {
  root: HTMLElement;
  focus(key: string): void;
  dispose(): void;
}

function sourceName(key: keyof FleetSources): string {
  switch (key) {
    case 'nodes': return t('devicesPage.source.nodes');
    case 'permissions': return t('devicesPage.source.permissions');
    case 'defaults': return t('devicesPage.source.defaults');
    case 'browsers': return t('devicesPage.source.browsers');
    case 'waits': return t('devicesPage.source.waits');
    case 'home': return t('devicesPage.source.home');
  }
}

const DEVICE_ICONS: Record<DeviceKind, string> = {
  self: 'M3 10l9-7 9 7v10H3z M9 20v-6h6v6',
  computer: 'M3 4h18v12H3z M8 20h8 M12 16v4',
  server: 'M3 4h18v6H3z M3 14h18v6H3z M7 7h.01 M7 17h.01',
  browser: 'M3 4h18v16H3z M3 9h18 M7 6.5h.01',
};

function glyph(path: string): SVGSVGElement {
  const icon = svg('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    'stroke-width': 1.75, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, 'devices-glyph');
  icon.append(svg('path', { d: path }));
  return icon;
}

function deviceIcon(kind: DeviceKind): HTMLElement {
  const wrap = el('span', `devices-icon devices-icon--${kind}`);
  wrap.append(glyph(DEVICE_ICONS[kind]));
  return wrap;
}

function statusDot(row: DeviceRow, stale: boolean): HTMLElement {
  const dot = el('span', `devices-dot devices-dot--${stale ? 'stale' : row.state}`);
  dot.setAttribute('aria-hidden', 'true');
  return dot;
}

export function capabilityChips(row: DeviceRow): HTMLElement {
  const list = el('span', 'devices-chips');
  if (row.kind === 'browser') {
    list.append(el('span', 'ui-badge ui-badge--success devices-chip', t('devicesPage.chip.readPages')));
    if (row.capabilities.some(cap => cap === 'click' || cap === 'fill')) {
      list.append(el('span', 'ui-badge ui-badge--success devices-chip', t('devicesPage.chip.actPages')));
    }
    return list;
  }
  for (const tier of TIERS) {
    const state = tierChipState(row, tier);
    if (!state || (state !== 'on' && tier === 'codex_login')) continue;
    const chip = el('span', 'ui-badge devices-chip', tierLabel(tier));
    if (state === 'on') {
      chip.classList.add('ui-badge--success');
      chip.title = t('devicesPage.chip.on');
    } else if (state === 'unknown-home') {
      chip.classList.add('ui-badge--info');
      chip.title = t('devicesPage.chip.unknownHome');
    } else if (state === 'device-off') {
      chip.classList.add('ui-badge--warning');
      chip.title = t('devicesPage.chip.deviceOff');
    } else {
      chip.classList.add('devices-chip--off');
      chip.title = t('devicesPage.chip.homeOff');
    }
    chip.setAttribute('aria-label', `${tierLabel(tier)}: ${chip.title}`);
    list.append(chip);
  }
  return list;
}

export function statusCell(row: DeviceRow, now: number, stale: boolean): HTMLElement {
  const cell = el('div', 'devices-status');
  const main = el('div', `devices-status__main devices-status__main--${stale ? 'stale' : row.state}`);
  const parts = [stateLabel(row.state)];
  if (row.state === 'online' && row.rttMs !== null) parts.push(t('devicesPage.status.rtt', { ms: String(row.rttMs) }));
  main.append(statusDot(row, stale), el('span', '', (stale ? `${t('devicesPage.status.lastKnown')}: ` : '') + parts.join(' · ')));
  cell.append(main);
  const details: string[] = [];
  if (row.state === 'offline' && row.lastSeenMs !== null) {
    details.push(t('devicesPage.status.lastSeen', { age: ageLabel(row.lastSeenMs, now) }));
    cell.title = fullTime(row.lastSeenMs);
  }
  if (row.state === 'conflict') details.push(t('devicesPage.status.conflictHint'));
  if (row.state === 'pending' && row.fingerprint) {
    details.push(t('devicesPage.status.fingerprint', { fingerprint: row.fingerprint }));
  }
  if (row.running > 0) details.push(t('devicesPage.status.running', { count: String(row.running) }));
  if (row.waiting > 0) details.push(t('devicesPage.status.waiting', { count: String(row.waiting) }));
  if (details.length) cell.append(el('div', 'devices-muted', details.join(' · ')));
  return cell;
}

export function mountDevicesPage(root: HTMLElement, store: DevicesStore, ports: DevicesPagePorts): DevicesPage {
  const now = ports.now || (() => Date.now());
  let filter: DeviceKind | 'all' = 'all';
  let relay: RelayState = 'idle';
  let dismissedRestart: number | null = null;
  let closeFilter: (() => void) | undefined;

  const page = el('section', 'devices-page');
  page.setAttribute('aria-labelledby', 'devicesPageTitle');
  root.replaceChildren(page);

  const focus = (key: string) => {
    const target = page.querySelector<HTMLElement>(`[data-focus-key="${CSS.escape(key)}"]`);
    (target ?? page.querySelector<HTMLElement>('[data-focus-key="back"]'))?.focus({ preventScroll: true });
  };

  const render = (snapshot: DevicesSnapshot) => {
    const active = document.activeElement;
    const focusOwner = page.contains(active) ? active?.closest<HTMLElement>('[data-focus-key], [data-filter]') : null;
    const focusKey = focusOwner?.hasAttribute('data-filter') ? 'filter' : focusOwner?.dataset.focusKey;
    const topologyOpen = page.querySelector<HTMLDetailsElement>('.devices-topology')?.open ?? false;
    closeFilter?.();
    closeFilter = undefined;
    const title = el('h1', 'devices-page__title', t('devicesPage.title'));
    title.id = 'devicesPageTitle';
    page.replaceChildren(nav(), title, ...banners(snapshot), homeCard(snapshot), fleetSection(snapshot), topology(snapshot, topologyOpen));
    if (focusKey) focus(focusKey);
  };

  const nav = () => {
    const bar = el('header', 'devices-page__nav ui-toolbar ui-toolbar--between');
    const back = createButton({ label: t('topbar.backToChat'), variant: 'ghost', onClick: ports.backToChat });
    back.prepend(glyph('M19 12H5m7-7-7 7 7 7'));
    back.dataset.focusKey = 'back';
    const add = createButton({ label: t('devicesPage.add'), variant: 'primary',
      onClick: () => ports.openWizard(undefined, 'add') });
    add.prepend(glyph('M12 5v14M5 12h14'));
    add.dataset.focusKey = 'add';
    bar.append(back, add);
    return bar;
  };

  const banners = (snapshot: DevicesSnapshot): HTMLElement[] => {
    const out: HTMLElement[] = [];
    if (snapshot.disconnected) {
      out.push(createAlert({
        variant: 'warning',
        title: snapshot.syncedAt
          ? t('devicesPage.banner.disconnected', { time: clockTime(snapshot.syncedAt) })
          : t('devicesPage.banner.disconnectedNever'),
      }).el);
    }
    if (snapshot.restartedAt && snapshot.restartedAt !== dismissedRestart) {
      const restartedAt = snapshot.restartedAt;
      out.push(createAlert({
        variant: 'info', dismissible: true, dismissLabel: t('devicesPage.dismiss'),
        title: t('devicesPage.banner.restarted', { time: clockTime(restartedAt) }),
        onDismiss: () => { dismissedRestart = restartedAt; },
      }).el);
    }
    if (snapshot.staleSources.length) {
      out.push(createAlert({
        variant: 'warning',
        title: t('devicesPage.banner.stale', { sources: snapshot.staleSources.map(sourceName).join(', ') }),
      }).el);
    }
    const sources = snapshot.sources;
    const failed = sources ? (Object.keys(sources) as Array<keyof FleetSources>)
      .filter(key => sources[key].state === 'failed') : [];
    if (failed.length) {
      const alert = createAlert({
        variant: 'error', title: t('devicesPage.banner.failed', { sources: failed.map(sourceName).join(', ') }),
      });
      const retry = createButton({ label: t('devicesPage.retry'), variant: 'secondary', size: 'sm',
        onClick: () => void store.refresh() });
      retry.dataset.focusKey = 'retry';
      alert.el.append(retry);
      out.push(alert.el);
    }
    if (sources?.permissions.state === 'absent' && sources.nodes.state === 'ok') {
      out.push(createAlert({ variant: 'info', title: t('devicesPage.absent.permissions') }).el);
    }
    return out;
  };

  const homeCard = (snapshot: DevicesSnapshot) => {
    const card = el('section', 'ui-card ui-stack devices-home');
    card.setAttribute('aria-labelledby', 'devicesHomeTitle');
    const home = snapshot.sources?.home.state === 'ok' ? snapshot.sources.home.value : null;
    const self = snapshot.fleet?.rows.find(row => row.kind === 'self');
    if (self) card.dataset.deviceKey = self.key;
    const title = el('h2', 'devices-home__title', t('devicesPage.home.label'));
    title.id = 'devicesHomeTitle';
    const host = home?.host || window.location.host;
    const identity = el('div', 'devices-home__identity');
    identity.append(title, el('p', 'devices-home__host', host));
    const header = el('div', 'devices-home__header');
    header.append(deviceIcon('self'), identity);
    if (self) header.append(detailsButton(self));
    const meta = el('div', 'devices-home__meta ui-row ui-row--wrap');
    if (self) meta.append(statusCell(self, now(), snapshot.disconnected));
    if (home?.storage) meta.append(el('span', '', t('devicesPage.home.storage', { backend: home.storage })));
    if (home?.version) meta.append(el('span', '', t('devicesPage.home.version', { version: home.version })));
    if (home?.bootedAt) meta.append(el('span', '', t('devicesPage.home.uptime', { age: ageLabel(home.bootedAt, now()) })));
    card.append(header, el('p', 'devices-home__body devices-muted', t('devicesPage.home.body')));
    if (meta.childElementCount) card.append(meta);
    if (relay !== 'idle') {
      const line = el('p', `devices-home__relay devices-home__relay--${relay}`, relayStatusText(relay));
      line.setAttribute('role', 'status');
      card.append(line);
    }
    return card;
  };

  const fleetSection = (snapshot: DevicesSnapshot) => {
    const section = el('section', 'devices-fleet');
    section.setAttribute('aria-labelledby', 'devicesFleetTitle');
    const fleet = snapshot.fleet;
    const heading = el('div', 'devices-fleet__header ui-row ui-row--between ui-row--wrap');
    const lead = el('div', 'devices-fleet__lead');
    const title = el('h2', '', t('devicesPage.fleet.label'));
    title.id = 'devicesFleetTitle';
    lead.append(title);
    heading.append(lead);
    section.append(heading);
    if (!fleet) {
      const loading = el('div', 'devices-loading ui-row');
      loading.append(createSpinner({ size: 'sm', label: t('devicesPage.loading') }), el('span', '', t('devicesPage.loading')));
      section.append(loading);
      return section;
    }
    const summary = summarizeFleet(fleet);
    if (!snapshot.disconnected && summary.total) {
      const counts = el('div', 'devices-fleet__counts ui-row ui-row--wrap');
      counts.append(el('span', 'ui-badge ui-badge--success',
        t('devicesPage.pill.online', { online: String(summary.online), total: String(summary.total) })));
      if (summary.attention) counts.append(el('span', 'ui-badge ui-badge--warning',
        t('devicesPage.pill.attention', { count: String(summary.attention) })));
      lead.append(counts);
    }
    const groups = fleet.groups.filter(group => group.kind !== 'self');
    if (groups.length || filter !== 'all') heading.append(filterMenu());
    const filtered = groups.filter(group => filter === 'all' || group.kind === filter);
    if (!filtered.length) {
      const inventories: Array<'nodes' | 'browsers'> = filter === 'all' ? ['nodes', 'browsers']
        : [filter === 'browser' ? 'browsers' : 'nodes'];
      const unknown = inventories.filter(key => snapshot.sources?.[key].state !== 'ok');
      section.append(createEmptyState({ title: unknown.length
        ? t('devicesPage.banner.failed', { sources: unknown.map(sourceName).join(', ') })
        : t('devicesPage.empty.title'),
      description: !unknown.length && filter === 'all' ? t('devicesPage.empty.body') : undefined }));
      return section;
    }
    for (const group of filtered) {
      const label = el('h3', 'devices-group__title', kindGroupLabel(group.kind));
      label.id = `devicesGroup-${group.kind}`;
      label.append(el('span', 'devices-group__count', String(group.rows.length)));
      const list = el('ul', 'devices-group__list');
      list.setAttribute('aria-labelledby', label.id);
      for (const row of group.rows) list.append(fleetRow(row, snapshot.disconnected));
      const box = el('section', 'devices-group');
      box.append(label, list);
      section.append(box);
    }
    section.append(el('p', 'devices-muted devices-legend', t('devicesPage.legend')));
    return section;
  };

  const filterMenu = () => {
    const kinds: Array<DeviceKind | 'all'> = ['all', ...KIND_ORDER.filter(kind => kind !== 'self')];
    const label = (kind: DeviceKind | 'all') => kind === 'all' ? t('devicesPage.filter.all') : kindShortLabel(kind);
    const dropdown = createDropdown({
      label: t('devicesPage.filter.label', { kind: label(filter) }), variant: 'secondary', size: 'sm', align: 'end',
      items: kinds.map(kind => ({ id: kind, label: label(kind) })),
      onSelect: id => { filter = id as DeviceKind | 'all'; render(store.snapshot()); focus('filter'); },
    });
    const trigger = dropdown.el.querySelector('button')!;
    trigger.dataset.focusKey = 'filter';
    trigger.append(glyph('m6 9 6 6 6-6'));
    dropdown.el.dataset.filter = '';
    closeFilter = dropdown.close;
    return dropdown.el;
  };

  const fleetRow = (row: DeviceRow, stale: boolean) => {
    const item = el('li', `devices-row devices-row--${row.state}`);
    item.dataset.deviceKey = row.key;
    const name = el('div', 'devices-row__identity');
    const text = el('div', 'devices-row__text');
    const title = el('div', 'devices-row__title ui-row ui-row--wrap');
    title.append(el('strong', '', row.name));
    if (row.isDefault) title.append(el('span', 'ui-badge devices-chip', t('devicesPage.defaultBadge')));
    const sub = [row.system !== row.name ? row.system : '', row.version ? `v${row.version}` : '',
      routeLabel(row.route)].filter(Boolean);
    text.append(title, el('div', 'devices-muted', sub.join(' · ')));
    if (row.outdated) text.append(el('div', 'devices-muted devices-warn', t('devicesPage.updateAvailable')));
    name.append(deviceIcon(row.kind), text);
    const status = el('div', 'devices-row__status');
    status.append(statusCell(row, now(), stale));
    const allowed = el('div', 'devices-row__caps');
    allowed.setAttribute('role', 'group');
    allowed.setAttribute('aria-label', t('devicesPage.col.allowed'));
    allowed.append(capabilityChips(row));
    const actions = el('div', 'devices-row__actions ui-row ui-row--wrap');
    if (row.state === 'pending' && row.nodeId) {
      const nodeId = row.nodeId;
      const focusKey = `review:${row.key}`;
      const review = createButton({ label: t('devicesPage.action.review'), variant: 'primary', size: 'sm',
        onClick: () => ports.openWizard(nodeId, focusKey) });
      review.dataset.focusKey = focusKey;
      actions.append(review);
    }
    if (row.route === 'browser_relay' && row.state !== 'online' && relay === 'idle') {
      const start = createButton({ label: t('devicesPage.relay.start'), variant: 'secondary', size: 'sm',
        onClick: () => ports.ensureRelay() });
      start.dataset.focusKey = `relay:${row.key}`;
      actions.append(start);
    }
    item.append(name, status, allowed, detailsButton(row));
    if (actions.childElementCount) item.append(actions);
    return item;
  };

  const detailsButton = (row: DeviceRow) => {
    const open = createButton({ label: t('devicesPage.action.details'), variant: 'ghost', size: 'sm',
      onClick: () => ports.openDevice(row.key) });
    open.classList.add('devices-details-button');
    open.append(glyph('m9 6 6 6-6 6'));
    open.setAttribute('aria-label', t('devicesPage.action.detailsFor', { name: row.name }));
    open.dataset.focusKey = `open:${row.key}`;
    return open;
  };

  const topology = (snapshot: DevicesSnapshot, open: boolean) => {
    const details = el('details', 'devices-topology');
    const summary = createDisclosureSummary(document, t('devicesPage.topology.title'));
    summary.classList.add('devices-topology__summary');
    summary.dataset.focusKey = 'topology';
    const content = el('div');
    const sync = () => {
      if (details.open && snapshot.fleet) {
        const host = snapshot.sources?.home.state === 'ok' ? snapshot.sources.home.value.host : window.location.host;
        if (!content.firstChild) content.append(renderTopology(snapshot.fleet, host, snapshot.disconnected));
      } else content.replaceChildren();
    };
    details.append(summary, content);
    details.open = open;
    sync();
    details.addEventListener('toggle', sync);
    return details;
  };

  const unsubscribe = store.subscribe(render);
  const unsubscribeRelay = subscribeRelay(state => { relay = state; render(store.snapshot()); });
  const onLanguage = () => render(store.snapshot());
  window.addEventListener('tofu:language-change', onLanguage);
  return {
    root: page,
    focus,
    dispose() {
      closeFilter?.();
      unsubscribe();
      unsubscribeRelay();
      window.removeEventListener('tofu:language-change', onLanguage);
      page.remove();
    },
  };
}

