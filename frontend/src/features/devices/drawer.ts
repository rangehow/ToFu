/** Device drawer: health, the two-ended capability table, shared folders, default-per-kind and removal.
 *
 * The Home column is this Home's ceiling (PUT /api/v4/devices/{id}/ceiling); the Device column is what the
 * device itself allows and is read-only here; Effective is their intersection as the Home reports it. A
 * switch is disabled while its write is in flight and returns to the Home's value if the write fails. The
 * drawer follows the live store, so a node going offline or a change from another tab shows at once.
 * Entry point: openDeviceDrawer. Dependencies: the devices store, ui kit, i18n.
 */
import { t } from '../../i18n';
import { createButton, createChoice, createDialog, createDrawer, createField, toast } from '../../ui';
import { el, fullTime } from './dom';
import { ageLabel, defaultKindLabel, routeLabel, tierLongLabel } from './labels';
import type { DevicesSnapshot, DevicesStore } from './live';
import { DEFAULT_KINDS, TIERS, type DefaultKind, type DeviceRow, type Tiers } from './model';
import { statusCell } from './page';

export interface DrawerPorts {
  ensureRelay(): void;
  now?(): number;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function definition(list: HTMLElement, term: string, value: Node | string): void {
  const row = el('div', 'devices-drawer__fact');
  const dt = el('dt', '', term);
  const dd = el('dd');
  dd.append(value);
  row.append(dt, dd);
  list.append(row);
}

export function openDeviceDrawer(store: DevicesStore, key: string, ports: DrawerPorts,
  onClosed?: () => void): () => void {
  const now = ports.now || (() => Date.now());
  const body = el('div', 'devices-drawer');
  const pending = new Set<string>();
  let renaming = false;
  let lastRow: DeviceRow | null = null;
  const drawer = createDrawer({ title: '', body, closeLabel: t('devicesPage.drawer.close') });
  drawer.el.classList.add('devices-drawer-shell');
  const titleNode = drawer.el.querySelector('.ui-dialog__title');

  const write = async (id: string, action: () => Promise<unknown>, done?: string) => {
    pending.add(id);
    render(store.snapshot());
    try {
      await action();
      if (done) toast(done, { variant: 'success' });
    } catch (error) {
      toast(t('devicesPage.error.save', { message: errorText(error) }), { variant: 'error' });
    } finally {
      pending.delete(id);
      await store.refresh();
    }
  };

  const header = (row: DeviceRow) => {
    const wrap = el('div', 'devices-drawer__head ui-row ui-row--between ui-row--wrap');
    if (renaming && row.nodeId) {
      const nodeId = row.nodeId;
      const field = createField({ label: t('devicesPage.drawer.renameLabel'), value: row.name, size: 'sm' });
      const input = field.control as HTMLInputElement;
      const save = createButton({ label: t('devicesPage.drawer.renameSave'), variant: 'primary', size: 'sm',
        onClick: () => {
          const name = input.value.trim();
          renaming = false;
          if (name && name !== row.name) void write('rename', () => store.client.rename(nodeId, name));
          else render(store.snapshot());
        } });
      const cancel = createButton({ label: t('devicesPage.cancel'), variant: 'ghost', size: 'sm',
        onClick: () => { renaming = false; render(store.snapshot()); } });
      wrap.append(field.el, el('div', 'ui-row'));
      wrap.lastElementChild?.append(save, cancel);
      queueMicrotask(() => input.focus());
      return wrap;
    }
    const lead = el('div', 'devices-drawer__lead ui-row');
    const dot = el('span', `devices-dot devices-dot--${store.snapshot().disconnected ? 'stale' : row.state}`);
    dot.setAttribute('aria-hidden', 'true');
    lead.append(dot, statusCell(row, now(), store.snapshot().disconnected));
    wrap.append(lead);
    const actions = el('div', 'ui-row devices-drawer__actions');
    if (row.nodeId && (row.kind === 'computer' || row.kind === 'server')) {
      actions.append(createButton({ label: t('devicesPage.drawer.rename'), variant: 'secondary', size: 'sm',
        disabled: pending.has('rename'), onClick: () => { renaming = true; render(store.snapshot()); } }));
      const remove = removeButton(row);
      if (remove) actions.append(remove);
    }
    wrap.append(actions);
    return wrap;
  };

  const health = (row: DeviceRow) => {
    const section = el('section', 'devices-drawer__section');
    section.append(el('h3', '', t('devicesPage.drawer.health')));
    const list = el('dl', 'devices-drawer__facts');
    if (row.lastSeenMs !== null && row.kind !== 'browser') {
      const seen = el('span', '', t('devicesPage.drawer.ago', { age: ageLabel(row.lastSeenMs, now()) }));
      seen.title = fullTime(row.lastSeenMs);
      definition(list, t('devicesPage.drawer.lastSeen'), seen);
    }
    if (row.kind !== 'self') {
      definition(list, t('devicesPage.drawer.rtt'), row.rttMs === null ? '—' : t('devicesPage.status.rtt', { ms: String(row.rttMs) }));
    }
    const route = el('span', '', routeLabel(row.route));
    if (row.route === 'browser_relay') {
      route.append(el('span', 'devices-muted', ` — ${t('devicesPage.route.browserRelayHint')}`));
    }
    definition(list, t('devicesPage.drawer.route'), route);
    if (row.version) {
      definition(list, t('devicesPage.drawer.version'), `${row.version} · ${row.outdated
        ? t('devicesPage.updateAvailable') : t('devicesPage.drawer.versionCurrent')}`);
    }
    if (row.system) definition(list, t('devicesPage.drawer.system'), row.system);
    if (row.fingerprint) definition(list, t('devicesPage.drawer.fingerprint'), row.fingerprint);
    if (row.kind === 'computer' || row.kind === 'server') {
      definition(list, t('devicesPage.drawer.running'), String(row.running));
      definition(list, t('devicesPage.drawer.waiting'), String(row.waiting));
    }
    section.append(list);
    if (row.route === 'browser_relay' && row.state !== 'online') {
      section.append(createButton({ label: t('devicesPage.relay.start'), variant: 'secondary', size: 'sm',
        onClick: () => ports.ensureRelay() }));
    }
    return section;
  };

  const capabilities = (row: DeviceRow) => {
    const section = el('section', 'devices-drawer__section');
    section.append(el('h3', '', t('devicesPage.drawer.caps')));
    if (row.kind === 'self') {
      section.append(el('p', 'devices-muted', t('devicesPage.drawer.selfBody')));
      return section;
    }
    if (row.kind === 'browser') {
      section.append(el('p', '', t('devicesPage.drawer.browserBody')));
      section.append(el('p', 'devices-muted', row.capabilities.join(' · ')));
      return section;
    }
    const permissions = row.permissions;
    const nodeId = row.nodeId;
    const table = el('table', 'ui-table devices-caps');
    const head = el('tr');
    for (const label of [t('devicesPage.drawer.colCapability'), t('devicesPage.drawer.colHome'),
      t('devicesPage.drawer.colDevice'), t('devicesPage.drawer.colEffective')]) {
      const th = el('th', '', label);
      th.scope = 'col';
      head.append(th);
    }
    const thead = el('thead');
    thead.append(head);
    const tbody = el('tbody');
    for (const tier of TIERS) {
      const tr = el('tr');
      const name = el('th', '', tierLongLabel(tier));
      name.scope = 'row';
      const homeCell = el('td');
      const homeOn = tier === 'read' || !!permissions?.home[tier];
      const device = permissions?.device ?? null;
      const deviceOn = device ? device[tier] : row.declared[tier] === true;
      if (tier === 'read' && permissions) {
        const floor = el('span', 'ui-badge ui-badge--info devices-chip', t('devicesPage.drawer.always'));
        floor.title = t('devicesPage.drawer.readFloor');
        homeCell.append(floor);
      } else if (permissions && nodeId) {
        const id = `ceiling:${tier}`;
        const choice = createChoice({
          type: 'switch', label: t('devicesPage.drawer.homeSwitch', { capability: tierLongLabel(tier) }),
          checked: homeOn, disabled: pending.has(id) || row.state === 'pending',
          onChange: checked => {
            const next: Partial<Tiers> = {};
            for (const key of TIERS) if (key !== 'read') next[key] = permissions.home[key];
            next[tier] = checked;
            void write(id, () => store.client.setCeiling(nodeId, next));
          },
        });
        choice.el.classList.add('devices-switch');
        homeCell.append(choice.el);
      } else {
        homeCell.append(el('span', 'devices-muted', '—'));
      }
      const deviceCell = el('td');
      // The device's own switch is read-only here: a labelled state, not a disabled control.
      const deviceState = el('span', `ui-badge devices-chip ${deviceOn ? 'ui-badge--success' : 'devices-chip--off'}`,
        deviceOn ? t('devicesPage.drawer.effOn') : t('devicesPage.drawer.deviceOffShort'));
      deviceState.setAttribute('aria-label', t('devicesPage.drawer.deviceSwitch', { capability: tierLongLabel(tier) }));
      deviceCell.append(deviceState);
      const effective = el('td');
      const effectiveOn = permissions ? permissions.effective[tier] : null;
      if (effectiveOn === true) effective.append(el('span', 'devices-on', t('devicesPage.drawer.effOn')));
      else if (effectiveOn === false) {
        effective.append(homeOn
          ? el('span', 'devices-warn', t('devicesPage.drawer.effDeviceOff'))
          : el('span', 'devices-muted', t('devicesPage.drawer.effHomeOff')));
      } else effective.append(el('span', 'devices-muted', '—'));
      tr.append(name, homeCell, deviceCell, effective);
      tbody.append(tr);
    }
    table.append(thead, tbody);
    const scroller = el('div', 'devices-caps__scroll');
    scroller.append(table);
    section.append(scroller);
    section.append(el('p', 'devices-note', permissions ? t('devicesPage.drawer.deviceNote') : t('devicesPage.drawer.homeUnavailable')));
    return section;
  };

  const folders = (row: DeviceRow) => {
    const section = el('section', 'devices-drawer__section');
    section.append(el('h3', '', t('devicesPage.drawer.shared')));
    if (!row.shareRoots.length) section.append(el('p', 'devices-muted', t('devicesPage.drawer.sharedNone')));
    else {
      const list = el('ul', 'devices-drawer__roots');
      for (const root of row.shareRoots) list.append(el('li', '', root));
      section.append(list, el('p', 'devices-muted', t('devicesPage.drawer.sharedNote')));
    }
    return section;
  };

  const routing = (row: DeviceRow, snapshot: DevicesSnapshot) => {
    const kind = row.kind as DefaultKind;
    if (!row.nodeId || !(DEFAULT_KINDS as readonly string[]).includes(kind)) return null;
    if (snapshot.sources?.defaults.state !== 'ok') return null;
    const nodeId = row.nodeId;
    const section = el('section', 'devices-drawer__section');
    section.append(el('h3', '', t('devicesPage.drawer.routing')));
    const choice = createChoice({
      type: 'switch', label: t('devicesPage.drawer.default', { kind: defaultKindLabel(kind) }),
      checked: row.isDefault, disabled: pending.has('default') || row.state === 'pending',
      onChange: checked => void write('default', () => store.client.setDefault(kind, checked ? nodeId : null)),
    });
    section.append(choice.el, el('p', 'devices-muted', t('devicesPage.drawer.defaultHint')));
    return section;
  };

  const removeButton = (row: DeviceRow) => {
    if (!row.nodeId || row.inProcess || (row.kind !== 'computer' && row.kind !== 'server')) return null;
    const nodeId = row.nodeId;
    return createButton({ label: t('devicesPage.drawer.remove'), variant: 'danger', size: 'sm', disabled: pending.has('remove'),
      onClick: async () => {
        const answer = await createDialog({
          title: t('devicesPage.drawer.removeTitle', { name: row.name }),
          body: t('devicesPage.drawer.removeBody'),
          actions: [{ id: 'cancel', label: t('devicesPage.cancel') },
            { id: 'remove', label: t('devicesPage.drawer.remove'), variant: 'danger' }],
        }).open();
        if (answer !== 'remove') return;
        await write('remove', () => store.client.remove(nodeId), t('devicesPage.drawer.removed', { name: row.name }));
        drawer.close();
      } });
  };

  const removal = (row: DeviceRow) => {
    if (!row.inProcess) return null;
    return el('p', 'devices-muted', t('devicesPage.drawer.removeInProcess'));
  };

  const render = (snapshot: DevicesSnapshot) => {
    const row = snapshot.fleet?.rows.find(candidate => candidate.key === key) || null;
    const focusLabel = (document.activeElement as HTMLElement | null)?.closest('label')?.textContent || '';
    if (!row) {
      if (titleNode) titleNode.textContent = lastRow?.name || t('devicesPage.title');
      body.replaceChildren(el('p', 'devices-muted', snapshot.fleet ? t('devicesPage.drawer.gone') : t('devicesPage.loading')));
      return;
    }
    lastRow = row;
    if (titleNode) titleNode.textContent = row.name;
    body.replaceChildren(...[header(row), health(row), capabilities(row), folders(row), routing(row, snapshot),
      removal(row)].filter((node): node is HTMLElement => !!node));
    if (focusLabel) {
      const match = [...body.querySelectorAll('label')].find(label => label.textContent === focusLabel);
      match?.querySelector('input')?.focus();
    }
  };

  const unsubscribe = store.subscribe(render);
  const onLanguage = () => render(store.snapshot());
  window.addEventListener('tofu:language-change', onLanguage);
  document.body.append(drawer.el);
  const closed = drawer.open();
  void closed.then(() => {
    unsubscribe();
    window.removeEventListener('tofu:language-change', onLanguage);
    drawer.el.remove();
    onClosed?.();
  });
  return () => drawer.close();
}

