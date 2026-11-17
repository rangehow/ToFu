/** Devices page: the Home card, the fleet grouped by kind with live health and effective capabilities, and
 * the topology explainer. Detail and onboarding open as a drawer and a wizard over the list.
 *
 * Everything shown is the shared store's projection; the page never fetches by itself, so it updates exactly
 * when the Home reports a change or the push channel reconnects. Rows keep their DOM keys, so focus survives
 * an update. Entry point: mountDevicesPage. Dependencies: the devices store, ui kit, i18n.
 */
import { t } from '../../i18n';
import {
  createAlert, createButton, createDisclosureSummary, createDropdown, createEmptyState, createSpinner,
} from '../../ui';
import { clockTime, el, fullTime } from './dom';
import {
  ageLabel, kindGroupLabel, kindShortLabel, routeLabel, stateLabel, tierLabel,
} from './labels';
import type { DevicesSnapshot, DevicesStore } from './live';
import {
  KIND_ORDER, TIERS, tierChipState, type DeviceKind, type DeviceRow, type FleetSources,
} from './model';
import { renderTopology } from './topology';
import { relayStatusText, subscribeRelay, type RelayState } from './relay';

export interface DevicesPagePorts {
  openDevice(key: string): void;
  openWizard(confirmNodeId?: string): void;
  ensureRelay(): void;
  now?(): number;
  /** First-visit memory for the topology explainer. */
  readTopologySeen?(): boolean;
  writeTopologySeen?(): void;
}

export interface DevicesPage {
  root: HTMLElement;
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

function statusDot(row: DeviceRow, stale: boolean): HTMLElement {
  const dot = el('span', `devices-dot devices-dot--${stale ? 'stale' : row.state}`);
  dot.setAttribute('aria-hidden', 'true');
  return dot;
}

export function capabilityChips(row: DeviceRow): HTMLElement {
  const list = el('span', 'devices-chips');
  if (row.kind === 'self') {
    list.append(el('span', 'devices-muted', t('devicesPage.status.inProcess')));
    return list;
  }
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
  const main = el('div', `devices-status__main devices-status__main--${row.state}`);
  const parts = [stateLabel(row.state)];
  if (row.state === 'online' && row.rttMs !== null) parts.push(t('devicesPage.status.rtt', { ms: String(row.rttMs) }));
  main.textContent = (stale ? `${t('devicesPage.status.lastKnown')}: ` : '') + parts.join(' · ');
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
  if (row.kind === 'self') details.push(t('devicesPage.status.inProcess'));
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
  let topologyOpen = !(ports.readTopologySeen?.() ?? false);

  const page = el('section', 'devices-page');
  page.setAttribute('aria-labelledby', 'devicesPageTitle');
  root.replaceChildren(page);

  const render = (snapshot: DevicesSnapshot) => {
    const focusKey = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('[data-focus-key]')
      ?.dataset.focusKey;
    page.replaceChildren(header(), ...banners(snapshot), homeCard(snapshot), fleetSection(snapshot), topology(snapshot));
    if (focusKey) page.querySelector<HTMLElement>(`[data-focus-key="${CSS.escape(focusKey)}"]`)?.focus();
  };

  const header = () => {
    const bar = el('header', 'devices-page__header ui-row ui-row--between ui-row--wrap');
    const title = el('h1', 'devices-page__title', t('devicesPage.title'));
    title.id = 'devicesPageTitle';
    const tools = el('div', 'ui-row ui-row--wrap devices-page__tools');
    const kinds: Array<DeviceKind | 'all'> = ['all', ...KIND_ORDER];
    const filterLabel = filter === 'all' ? t('devicesPage.filter.all') : kindShortLabel(filter);
    const dropdown = createDropdown({
      label: t('devicesPage.filter.label', { kind: filterLabel }),
      variant: 'secondary',
      align: 'end',
      items: kinds.map(kind => ({ id: kind, label: kind === 'all' ? t('devicesPage.filter.all') : kindShortLabel(kind) })),
      onSelect: id => { filter = id as DeviceKind | 'all'; render(store.snapshot()); },
    });
    dropdown.el.dataset.focusKey = 'filter';
    const add = createButton({ label: t('devicesPage.add'), variant: 'primary', onClick: () => ports.openWizard() });
    add.dataset.focusKey = 'add';
    tools.append(dropdown.el, add);
    bar.append(title, tools);
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
    const card = el('section', 'ui-card devices-home');
    card.setAttribute('aria-label', t('devicesPage.home.label'));
    const home = snapshot.sources?.home.state === 'ok' ? snapshot.sources.home.value : null;
    const title = el('h2', 'devices-home__title', t('devicesPage.home.title', { host: home?.host || window.location.host }));
    const body = el('p', 'devices-home__body', t('devicesPage.home.body'));
    const meta: string[] = [];
    if (home?.storage) meta.push(t('devicesPage.home.storage', { backend: home.storage }));
    if (home?.bootedAt) meta.push(t('devicesPage.home.uptime', { age: ageLabel(home.bootedAt, now()) }));
    if (home?.version) meta.push(t('devicesPage.home.version', { version: home.version }));
    card.append(title, body);
    if (meta.length) card.append(el('p', 'devices-muted devices-home__meta', meta.join(' · ')));
    if (relay !== 'idle') {
      const line = el('p', `devices-home__relay devices-home__relay--${relay}`, relayStatusText(relay));
      line.setAttribute('role', 'status');
      card.append(line);
    }
    return card;
  };

  const fleetSection = (snapshot: DevicesSnapshot) => {
    const section = el('section', 'devices-fleet');
    section.setAttribute('aria-label', t('devicesPage.fleet.label'));
    const fleet = snapshot.fleet;
    if (!fleet) {
      const loading = el('div', 'devices-loading ui-row');
      loading.append(createSpinner({ size: 'sm', label: t('devicesPage.loading') }), el('span', '', t('devicesPage.loading')));
      section.append(loading);
      return section;
    }
    const groups = fleet.groups.filter(group => filter === 'all' || group.kind === filter);
    const nodes = fleet.rows.filter(row => row.kind !== 'self');
    if (!nodes.length && filter === 'all') {
      const empty = createEmptyState({
        title: t('devicesPage.empty.title'), description: t('devicesPage.empty.body'),
        action: { label: t('devicesPage.add'), variant: 'primary', onClick: () => ports.openWizard() },
      });
      section.append(empty);
    }
    if (!groups.length) return section;
    const table = el('table', 'ui-table devices-table');
    const head = el('thead');
    const headRow = el('tr');
    for (const label of [
      t('devicesPage.col.device'), t('devicesPage.col.kind'), t('devicesPage.col.status'),
      t('devicesPage.col.route'), t('devicesPage.col.allowed'),
    ]) {
      const th = el('th', '', label);
      th.scope = 'col';
      headRow.append(th);
    }
    const actionsHead = el('th', 'devices-table__actions-head');
    actionsHead.append(el('span', 'devices-visually-hidden', t('devicesPage.col.actions')));
    headRow.append(actionsHead);
    head.append(headRow);
    table.append(head);
    const stale = snapshot.disconnected;
    for (const group of groups) {
      const body = el('tbody', 'devices-table__group');
      const groupRow = el('tr', 'devices-table__group-row');
      const groupCell = el('th', '', kindGroupLabel(group.kind));
      groupCell.colSpan = 6;
      groupCell.scope = 'rowgroup';
      groupRow.append(groupCell);
      body.append(groupRow);
      for (const row of group.rows) body.append(fleetRow(row, stale));
      table.append(body);
    }
    section.append(table, el('p', 'devices-muted devices-legend', t('devicesPage.legend')));
    return section;
  };

  const fleetRow = (row: DeviceRow, stale: boolean) => {
    const tr = el('tr', `devices-row devices-row--${row.state}`);
    tr.dataset.deviceKey = row.key;
    const name = el('td', 'devices-row__name');
    name.dataset.label = t('devicesPage.col.device');
    const nameLine = el('div', 'devices-row__title');
    const nameText = el('span', '', row.name);
    nameLine.append(statusDot(row, stale), nameText);
    if (row.isDefault) nameLine.append(el('span', 'ui-badge ui-badge--info devices-chip', t('devicesPage.defaultBadge')));
    name.append(nameLine);
    const sub: string[] = [];
    if (row.version) sub.push(`v${row.version}`);
    if (row.kind === 'self') sub.push(t('devicesPage.selfNode'));
    const subLine = el('div', 'devices-muted', sub.join(' · '));
    if (row.outdated) subLine.append(' · ', el('span', 'devices-warn', t('devicesPage.updateAvailable')));
    if (sub.length || row.outdated) name.append(subLine);
    const kind = el('td', '', row.system || kindShortLabel(row.kind));
    kind.dataset.label = t('devicesPage.col.kind');
    const status = el('td');
    status.dataset.label = t('devicesPage.col.status');
    status.append(statusCell(row, now(), stale));
    const route = el('td');
    route.dataset.label = t('devicesPage.col.route');
    route.append(el('div', '', routeLabel(row.route)));
    if (row.route === 'browser_relay') route.append(el('div', 'devices-muted', t('devicesPage.route.browserRelayHint')));
    const allowed = el('td');
    allowed.dataset.label = t('devicesPage.col.allowed');
    allowed.append(capabilityChips(row));
    const actions = el('td', 'devices-row__actions');
    if (row.state === 'pending' && row.nodeId) {
      const nodeId = row.nodeId;
      const review = createButton({ label: t('devicesPage.action.review'), variant: 'primary', size: 'sm',
        onClick: () => ports.openWizard(nodeId) });
      review.dataset.focusKey = `review:${row.key}`;
      actions.append(review);
    }
    if (row.route === 'browser_relay' && row.state !== 'online' && relay === 'idle') {
      const start = createButton({ label: t('devicesPage.relay.start'), variant: 'secondary', size: 'sm',
        onClick: () => ports.ensureRelay() });
      start.dataset.focusKey = `relay:${row.key}`;
      actions.append(start);
    }
    const open = createButton({ label: t('devicesPage.action.details'), variant: 'ghost', size: 'sm',
      onClick: () => ports.openDevice(row.key) });
    open.setAttribute('aria-label', t('devicesPage.action.detailsFor', { name: row.name }));
    open.dataset.focusKey = `open:${row.key}`;
    actions.append(open);
    tr.append(name, kind, status, route, allowed, actions);
    return tr;
  };

  const topology = (snapshot: DevicesSnapshot) => {
    const details = el('details', 'devices-topology');
    details.open = topologyOpen;
    details.addEventListener('toggle', () => {
      topologyOpen = details.open;
      if (!details.open) ports.writeTopologySeen?.();
    });
    const summary = createDisclosureSummary(document, t('devicesPage.topology.title'));
    summary.classList.add('devices-topology__summary');
    summary.dataset.focusKey = 'topology';
    details.append(summary);
    if (snapshot.fleet) details.append(renderTopology(snapshot.fleet, snapshot.sources?.home.state === 'ok'
      ? snapshot.sources.home.value.host : window.location.host));
    return details;
  };

  const unsubscribe = store.subscribe(render);
  const unsubscribeRelay = subscribeRelay(state => { relay = state; render(store.snapshot()); });
  const onLanguage = () => render(store.snapshot());
  window.addEventListener('tofu:language-change', onLanguage);
  return {
    root: page,
    dispose() {
      unsubscribe();
      unsubscribeRelay();
      window.removeEventListener('tofu:language-change', onLanguage);
      page.remove();
    },
  };
}

