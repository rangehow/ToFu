/** Topology explainer: the Home on top, every node below with its own outbound connection drawn upward.
 * It answers "which machine is primary" on screen: one Home holds state and decides; nodes only execute and
 * never connect to each other; phones and browser tabs are clients. Entry point: renderTopology.
 */
import { t } from '../../i18n';
import { el, svg } from './dom';
import { kindShortLabel, routeLabel, stateLabel } from './labels';
import type { DeviceRow, Fleet } from './model';

const WIDTH = 960;
const MARGIN = 16;
const GAP = 16;
const PER_ROW = 5;
const HOME_W = 340;
const HOME_H = 104;
const BOX_H = 76;
const ROW_GAP = 64;
const FIRST_ROW_Y = HOME_H + MARGIN + 72;
/** Characters that fit one SVG text line of a box; the full text is the box's title. */
const LINE_CHARS = 26;

function clip(text: string): string {
  return text.length > LINE_CHARS ? `${text.slice(0, LINE_CHARS - 1)}…` : text;
}

interface Box { lines: string[]; title: string; tone: string; line: string }

function nodeBox(row: DeviceRow): Box {
  const kind = row.kind === 'computer' ? t('devicesPage.topology.desktopNode')
    : row.kind === 'server' ? t('devicesPage.topology.headlessNode')
      : row.kind === 'browser' ? t('devicesPage.topology.browserNode') : kindShortLabel(row.kind);
  const how = row.state === 'online'
    ? t('devicesPage.topology.dialsVia', { route: routeLabel(row.route) })
    : row.state === 'offline' || row.state === 'never'
      ? t('devicesPage.topology.offline')
      : stateLabel(row.state);
  const line = row.state === 'online' ? 'live' : row.state === 'pending' || row.state === 'conflict' ? 'attention' : 'down';
  return { lines: [row.name, kind, how], title: `${row.name} — ${kind} — ${how}`, tone: row.state, line };
}

export function renderTopology(fleet: Fleet, host: string): HTMLElement {
  const wrap = el('figure', 'devices-topology__figure');
  const boxes: Box[] = fleet.rows.filter(row => row.kind !== 'self').map(nodeBox);
  boxes.push({
    lines: [t('devicesPage.topology.client'), t('devicesPage.topology.clientSub'), t('devicesPage.topology.clientHow')],
    title: t('devicesPage.topology.client'), tone: 'client', line: 'client',
  });
  const rows = Math.ceil(boxes.length / PER_ROW);
  const height = FIRST_ROW_Y + rows * BOX_H + (rows - 1) * ROW_GAP + MARGIN;
  const root = svg('svg', { viewBox: `0 0 ${WIDTH} ${height}`, role: 'img' }, 'devices-topology__svg');
  const label = svg('title');
  label.textContent = t('devicesPage.topology.title');
  root.append(label);
  const defs = svg('defs');
  const marker = svg('marker', { id: 'devicesArrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7,
    markerHeight: 7, orient: 'auto-start-reverse' });
  marker.append(svg('path', { d: 'M0,0 L10,5 L0,10 z' }, 'devices-topology__arrow'));
  defs.append(marker);
  root.append(defs);

  const homeX = (WIDTH - HOME_W) / 2;
  const homeBottom = MARGIN + HOME_H;
  const lines = svg('g');
  const nodes = svg('g');
  boxes.forEach((box, index) => {
    const row = Math.floor(index / PER_ROW);
    const inRow = Math.min(PER_ROW, boxes.length - row * PER_ROW);
    const boxW = (WIDTH - 2 * MARGIN - (PER_ROW - 1) * GAP) / PER_ROW;
    const rowWidth = inRow * boxW + (inRow - 1) * GAP;
    const x = (WIDTH - rowWidth) / 2 + (index % PER_ROW) * (boxW + GAP);
    const y = FIRST_ROW_Y + row * (BOX_H + ROW_GAP);
    const cx = x + boxW / 2;
    const targetX = homeX + HOME_W * ((index + 1) / (boxes.length + 1));
    lines.append(svg('line', { x1: cx, y1: y, x2: targetX, y2: homeBottom, 'marker-end': 'url(#devicesArrow)' },
      `devices-topology__link devices-topology__link--${box.line}`));
    const group = svg('g', {}, `devices-topology__node devices-topology__node--${box.tone}`);
    const title = svg('title');
    title.textContent = box.title;
    group.append(title, svg('rect', { x, y, width: boxW, height: BOX_H, rx: 8 }));
    box.lines.forEach((text, line) => {
      const node = svg('text', { x: cx, y: y + 24 + line * 19, 'text-anchor': 'middle' },
        line === 0 ? 'devices-topology__name' : line === 2 ? 'devices-topology__sub' : '');
      node.textContent = clip(text);
      group.append(node);
    });
    nodes.append(group);
  });

  const home = svg('g', {}, 'devices-topology__home');
  home.append(svg('rect', { x: homeX, y: MARGIN, width: HOME_W, height: HOME_H, rx: 10 }));
  [t('devicesPage.topology.home', { host }), t('devicesPage.topology.homeState'),
    t('devicesPage.topology.homeControl')].forEach((text, line) => {
    const node = svg('text', { x: WIDTH / 2, y: MARGIN + 30 + line * 24, 'text-anchor': 'middle' },
      line === 0 ? 'devices-topology__name' : '');
    node.textContent = text;
    home.append(node);
  });
  root.append(lines, home, nodes);
  const key = el('ul', 'devices-topology__key');
  for (const [kind, label] of [['live', t('devicesPage.topology.keyLive')], ['down', t('devicesPage.topology.keyDown')],
    ['attention', t('devicesPage.topology.keyAttention')], ['client', t('devicesPage.topology.keyClient')]] as const) {
    const item = el('li');
    const sample = svg('svg', { viewBox: '0 0 32 8', 'aria-hidden': 'true' });
    sample.append(svg('line', { x1: 0, y1: 4, x2: 32, y2: 4 }, `devices-topology__link devices-topology__link--${kind}`));
    item.append(sample, el('span', '', label));
    key.append(item);
  }
  wrap.append(root, key, el('figcaption', 'devices-muted', t('devicesPage.topology.legend')));
  return wrap;
}
