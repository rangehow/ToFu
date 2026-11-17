/** Sidebar Devices pill: "Devices · 5/7 online · 1 needs attention" at the foot of the sidebar.
 *
 * The anchor (`#sidebarDevicesPill`, index.html) is a plain link to `#/devices`, so it works before this
 * module loads; this idle-loaded module fills in the live status from the shared store and keeps it current
 * from the same push hints as the page. Entry point: mountDevicesPill. Dependencies: the devices store, i18n.
 */
import { t } from '../i18n';
import type { DevicesSnapshot } from './devices/live';
import { summarizeFleet } from './devices/model';
import { getDevicesStore } from './devices/shell';

export function pillStatus(snapshot: DevicesSnapshot): { text: string; tone: 'ok' | 'warn' | 'off' | 'idle' } {
  if (snapshot.disconnected) return { text: t('devicesPage.pill.unreachable'), tone: 'off' };
  if (!snapshot.fleet) return { text: t('devicesPage.pill.loading'), tone: 'idle' };
  const summary = summarizeFleet(snapshot.fleet);
  const parts = [t('devicesPage.pill.online', { online: String(summary.online), total: String(summary.total) })];
  if (summary.attention) parts.push(t('devicesPage.pill.attention', { count: String(summary.attention) }));
  return { text: parts.join(' · '), tone: summary.attention ? 'warn' : summary.online ? 'ok' : 'idle' };
}

let mounted = false;

export function mountDevicesPill(): void {
  const anchor = document.getElementById('sidebarDevicesPill');
  if (!anchor || mounted) return;
  mounted = true;
  const status = anchor.querySelector<HTMLElement>('[data-devices-pill-status]');
  const paint = (snapshot: DevicesSnapshot) => {
    const { text, tone } = pillStatus(snapshot);
    anchor.dataset.tone = tone;
    if (status) status.textContent = text;
    anchor.setAttribute('aria-label', `${t('devicesPage.pill.label')} · ${text}`);
  };
  getDevicesStore().subscribe(paint);
  // On a drawer-sized viewport the open sidebar covers the page it just opened; close it like a conversation does.
  anchor.addEventListener('click', () => {
    const sidebar = document.getElementById('sidebar');
    const drawer = window.matchMedia('(max-width: 768px), (max-width: 1024px) and (pointer: coarse)').matches;
    if (!drawer || !sidebar || sidebar.classList.contains('collapsed')) return;
    const toggle = window.TofuModules?.resolveAction('toggleSidebar');
    if (typeof toggle === 'function') void toggle();
  });
  window.addEventListener('tofu:language-change', () => paint(getDevicesStore().snapshot()));
}
