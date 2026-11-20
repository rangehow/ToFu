/** Responsive topology disclosure: one Home, outbound-only nodes, and UI clients. */
import { t } from '../../i18n';
import { el } from './dom';
import { kindShortLabel, routeLabel, stateLabel } from './labels';
import type { DeviceRow, Fleet } from './model';

function topologyNode(row: DeviceRow, stale: boolean): HTMLLIElement {
  const kind = row.kind === 'computer' ? t('devicesPage.topology.desktopNode')
    : row.kind === 'server' ? t('devicesPage.topology.headlessNode')
      : row.kind === 'browser' ? t('devicesPage.topology.browserNode') : kindShortLabel(row.kind);
  const how = row.state === 'online'
    ? t('devicesPage.topology.dialsVia', { route: routeLabel(row.route) })
    : row.state === 'offline' || row.state === 'never'
      ? t('devicesPage.topology.offline') : stateLabel(row.state);
  const node = el('li', 'devices-topology__node');
  const tone = stale ? '' : row.state === 'online' ? 'ui-badge--success'
    : row.state === 'pending' || row.state === 'conflict' ? 'ui-badge--warning' : '';
  const link = el('span', `ui-badge ${tone}`, stale ? `${t('devicesPage.status.lastKnown')}: ${how}` : how);
  node.append(el('strong', '', row.name), el('span', 'devices-muted', kind), link);
  return node;
}

export function renderTopology(fleet: Fleet, host: string, stale: boolean): HTMLElement {
  const figure = el('figure', 'devices-topology__figure');
  const home = el('div', 'devices-topology__home');
  home.append(el('strong', '', t('devicesPage.topology.home', { host })),
    el('span', 'devices-muted', t('devicesPage.topology.homeState')),
    el('span', 'devices-muted', t('devicesPage.topology.homeControl')));
  const connections = el('ul', 'devices-topology__nodes');
  for (const row of fleet.rows) if (row.kind !== 'self') connections.append(topologyNode(row, stale));
  const client = el('li', 'devices-topology__node');
  client.append(el('strong', '', t('devicesPage.topology.client')),
    el('span', 'devices-muted', t('devicesPage.topology.clientSub')),
    el('span', 'ui-badge', t('devicesPage.topology.clientHow')));
  connections.append(client);
  figure.append(home, connections, el('figcaption', 'devices-muted', t('devicesPage.topology.legend')));
  return figure;
}
