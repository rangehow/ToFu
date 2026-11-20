/** Overlay history for the lazy Devices workspace. The shell still owns workspace dispatch. */
import { goToWorkspace, workspacePath } from '../../application-shell/page-host';

const LIST = 'devices';

export function openDevicesOverlay(path: string): void {
  const fromList = workspacePath() === LIST;
  goToWorkspace(path);
  if (fromList) {
    // The hash assignment makes the child current synchronously. Its native hashchange mounts the overlay.
    window.history.replaceState({ tofuDevicesListParent: true }, '');
  }
}

export function closeDevicesOverlay(path: string, showList: (path: string) => void): void {
  if (workspacePath() !== path) return;
  if (window.history.state?.tofuDevicesListParent === true) {
    window.history.back();
  } else {
    // A direct link has no known Devices entry behind it. Stay in the same workspace and update its route.
    window.history.replaceState(null, '', `#/${LIST}`);
    showList(LIST);
  }
}

/** The existing Search tab owns all grants; the section has no client/profile focus contract. */
export async function openBrowserAccessSettings(): Promise<void> {
  const modules = window.TofuModules;
  const jump = modules?.resolveAction('jumpToSetting');
  if (typeof jump !== 'function' || !modules?.invokeFeature) throw new Error('Settings navigation unavailable');
  await modules.invokeFeature('jumpToSetting', ['search'], jump);
  const access = document.getElementById('browserAccessList');
  if (!access) throw new Error('Browser access view unavailable');
  let disclosure = access.closest('details');
  while (disclosure) {
    disclosure.open = true;
    disclosure = disclosure.parentElement?.closest('details') ?? null;
  }
  access.tabIndex = -1;
  access.focus({ preventScroll: true });
  access.scrollIntoView({ block: 'center', behavior: 'auto' });
}
