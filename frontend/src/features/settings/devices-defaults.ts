/** Settings → Devices: only the defaults and a link to the Devices page (docs/MULTI_DEVICE_NETWORK_DESIGN.md §9).
 *
 * The fleet, onboarding, health and permissions live on the Devices page. This tab keeps the default device per
 * kind (owner decision 7; GET/PUT /api/v4/devices/defaults) and the conversations waiting for a device to come
 * back (contracts/device_waits_v1.schema.json), each of which can be cancelled.
 * Entry points: _populateDevicesTab, _destroyDevicesTab (settings tab activation).
 * Dependencies: the devices client, ui kit, i18n.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { ApiError, request } from '../../api/transport';
import { createButton, createField, setFieldError, toast, type FieldHandle } from '../../ui';
import { createDevicesClient, type DevicesClient } from '../devices/client';
import { defaultKindLabel } from '../devices/labels';
import { DEFAULT_KINDS, projectFleet, type DefaultKind, type DeviceRow, type FleetSources } from '../devices/model';
import { el } from '../devices/dom';

type Bridge = { _populateDevicesTab?: () => void; _destroyDevicesTab?: () => void; closeSettings?: () => void };
const bridge = featureRegistry as unknown as Bridge;
let controller: AbortController | null = null;
// Accepted saves outlive the render so a replacement visit can read after they settle.
const pendingDefaultSaves = new Map<DefaultKind, Promise<void>>();

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function bindDefaultSave(client: DevicesClient, kind: DefaultKind, field: FieldHandle,
  initialValue: string, signal: AbortSignal, canPublish: () => boolean): void {
  const select = field.control as HTMLSelectElement;
  let acknowledged = initialValue;
  let needsAuthority = false;

  const adopt = (value: string) => {
    acknowledged = value;
    if (![...select.options].some(option => option.value === value)) {
      select.add(new Option(value, value));
    }
    select.value = value;
  };

  const reread = async () => {
    if (!canPublish() || !needsAuthority || retry.disabled) return;
    retry.disabled = true;
    try {
      const defaults = await client.defaults(signal);
      if (!canPublish() || defaults.state !== 'ok') return;
      const current = defaults.value.defaults[kind];
      // A malformed success without this kind is not evidence that it was cleared.
      if (current !== null && (typeof current !== 'string' || !current)) return;
      adopt(current ?? '');
      needsAuthority = false;
      setFieldError(field, null);
      select.disabled = false;
      if (document.activeElement === retry) select.focus();
      retry.hidden = true;
    } catch (error) {
      if (!signal.aborted) throw error;
    } finally {
      if (canPublish()) {
        retry.disabled = false;
        retry.hidden = !needsAuthority;
      }
    }
  };
  const retry = createButton({ label: t('settings.devices.retryDefaultRead'), variant: 'ghost', size: 'sm',
    onClick: () => { void reread(); } });
  retry.hidden = true;
  field.el.append(retry);
  select.addEventListener('change', () => {
    if (!canPublish() || needsAuthority || pendingDefaultSaves.has(kind)) return;
    const selected = select.value;
    if (selected === acknowledged) return;
    select.disabled = true;
    setFieldError(field, null);

    const save = client.setDefault(kind, selected || null).then(result => {
      if (!canPublish()) return;
      // Only this kind belongs to this write; other kinds can save independently.
      adopt(result.defaults[kind] ?? '');
      toast(t('settings.devices.saved'), { variant: 'success' });
    }).catch(async error => {
      if (!canPublish()) return;
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408) {
        select.value = acknowledged;
        setFieldError(field, t('devicesPage.error.save', { message: errorText(error) }));
        return;
      }
      // The write may have committed without an acknowledgement. Only a fresh read can unlock this kind.
      needsAuthority = true;
      select.selectedIndex = -1;
      setFieldError(field, t('settings.devices.defaultUnknown'));
      await reread();
    }).finally(() => {
      pendingDefaultSaves.delete(kind);
      if (canPublish()) select.disabled = needsAuthority;
    });
    pendingDefaultSaves.set(kind, save);
  });
}

async function populate(): Promise<void> {
  const root = document.getElementById('devicesSettingsRoot');
  if (!root) return;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const isCurrent = () => !signal.aborted && document.getElementById('devicesSettingsRoot') === root;
  const client = createDevicesClient();
  const open = createButton({ label: t('settings.devices.open'), variant: 'primary', onClick: () => {
    bridge.closeSettings?.();
    window.location.hash = '/devices';
  } });
  root.replaceChildren(open, el('p', 'settings-desc', t('devicesPage.loading')));
  if (pendingDefaultSaves.size) await Promise.all(pendingDefaultSaves.values());
  if (!isCurrent()) return;
  const reads = await Promise.all([
    client.nodes(signal), client.defaults(signal), client.waits(signal), client.browsers(signal),
  ]).catch(error => {
    if (!signal.aborted) throw error;
    return null;
  });
  if (!reads || !isCurrent()) return;
  const [nodes, defaults, waits, browsers] = reads;
  const sources: FleetSources = { nodes, defaults, waits, browsers, permissions: { state: 'absent' }, home: { state: 'absent' } };
  const fleet = projectFleet(sources);
  const parts: HTMLElement[] = [open];

  const section = el('div', 'stg-section');
  section.append(el('div', 'settings-section-title', t('settings.devices.defaultsTitle')),
    el('p', 'settings-desc', t('devicesPage.drawer.defaultHint')));
  if (defaults.state !== 'ok') {
    section.append(el('p', 'settings-desc', defaults.state === 'absent'
      ? t('settings.devices.defaultsAbsent') : t('devicesPage.banner.failed', { sources: t('devicesPage.source.defaults') })));
  } else {
    for (const kind of DEFAULT_KINDS) {
      const candidates = fleet.rows.filter((row: DeviceRow) => row.kind === kind && row.nodeId && row.state !== 'pending');
      const current = defaults.value.defaults[kind] || '';
      const choices = [{ value: '', label: t('settings.devices.noDefault') },
        ...candidates.map(row => ({ value: row.nodeId as string, label: row.name }))];
      if (current && !candidates.some(row => row.nodeId === current)) choices.push({ value: current, label: current });
      const field = createField({ label: t('settings.devices.defaultFor', { kind: defaultKindLabel(kind) }),
        choices, value: current, size: 'sm' });
      bindDefaultSave(client, kind, field, current, signal, () => isCurrent() && root.contains(field.el));
      section.append(field.el);
    }
  }
  parts.push(section);

  const waitSection = el('div', 'stg-section');
  waitSection.append(el('div', 'settings-section-title', t('settings.devices.waitsTitle')),
    el('p', 'settings-desc', t('settings.devices.waitsDesc')));
  if (waits.state !== 'ok') {
    waitSection.append(el('p', 'settings-desc', t('devicesPage.banner.failed', { sources: t('devicesPage.source.waits') })));
  } else if (!waits.value.length) {
    waitSection.append(el('p', 'settings-desc', t('settings.devices.waitsNone')));
  } else {
    const list = el('ul', 'devices-settings__waits');
    for (const wait of waits.value) {
      const item = el('li', 'ui-row ui-row--between');
      const device = wait.device?.name || wait.device?.selector || '';
      item.append(el('span', '', t('settings.devices.waitRow', { device, conversation: wait.conversationId || '' })));
      if (wait.waitId) {
        const id = wait.waitId;
        item.append(createButton({ label: t('settings.devices.waitCancel'), variant: 'ghost', size: 'sm', onClick: async () => {
          try {
            await request(`/api/v4/desktop/device-waits/${encodeURIComponent(id)}/cancel`, { method: 'POST', json: {}, onError: 'throw' });
            item.remove();
          } catch (error) {
            toast(t('devicesPage.error.save', { message: errorText(error) }), { variant: 'error' });
          }
        } }));
      }
      list.append(item);
    }
    waitSection.append(list);
  }
  parts.push(waitSection);
  root.replaceChildren(...parts);
}

bridge._populateDevicesTab = () => { void populate(); };
bridge._destroyDevicesTab = () => { controller?.abort(); controller = null; };
