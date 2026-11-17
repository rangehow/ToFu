/** Landing workspace: the office world is the default first screen, and the owner's last explicit choice
 * between it and the classic Session UI persists in local storage under LANDING_STORAGE_KEY.
 * index.html's pre-paint script applies it before any application code loads: an address without a route
 * becomes #/world unless the stored choice is the classic view, and html[data-landing="world"] keeps the
 * classic chrome hidden until the application names the workspace (01-workspace-dock.css). An address that
 * already names a route is honoured as given. The world feature writes the choice.
 * The choice only picks the first screen: the classic view (including its sidebar) looks the same whatever
 * is stored, and animated scenes live in the studio alone.
 * Entry points: rememberLanding, LANDING_STORAGE_KEY. */

export const LANDING_STORAGE_KEY = 'tofu_landing_workspace';
export type LandingWorkspace = 'world' | 'chat';

export function rememberLanding(workspace: LandingWorkspace): void {
  try {
    window.localStorage.setItem(LANDING_STORAGE_KEY, workspace);
  } catch {
    // An unavailable store keeps the default landing; nothing else depends on the write.
  }
}
