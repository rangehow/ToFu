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
import { request } from '../../api/transport';
import { createButton, createField, toast } from '../../ui';
import { createDevicesClient } from '../devices/client';
import { defaultKindLabel } from '../devices/labels';
import { DEFAULT_KINDS, projectFleet, type DefaultKind, type DeviceRow, type FleetSources } from '../devices/model';
import { el } from '../devices/dom';

type Bridge = { _populateDevicesTab?: () => void; _destroyDevicesTab?: () => void; closeSettings?: () => void };
const bridge = featureRegistry as unknown as Bridge;
let controller: AbortController | null = null;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function populate(): Promise<void> {
  const root = document.getElementById('devicesSettingsRoot');
  if (!root) return;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const client = createDevicesClient();
  const open = createButton({ label: t('settings.devices.open'), variant: 'primary', onClick: () => {
    bridge.closeSettings?.();
    window.location.hash = '/devices';
  } });
  root.replaceChildren(open, el('p', 'settings-desc', t('devicesPage.loading')));
  const [nodes, defaults, waits, browsers] = await Promise.all([
    client.nodes(signal), client.defaults(signal), client.waits(signal), client.browsers(signal),
  ]);
  if (signal.aborted) return;
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
      const field = createField({ label: t('settings.devices.defaultFor', { kind: defaultKindLabel(kind as DefaultKind) }),
        choices, value: current, size: 'sm' });
      const select = field.control as HTMLSelectElement;
      select.addEventListener('change', async () => {
        try {
          await client.setDefault(kind, select.value || null);
          toast(t('settings.devices.saved'), { variant: 'success' });
        } catch (error) {
          toast(t('devicesPage.error.save', { message: errorText(error) }), { variant: 'error' });
        }
      });
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
