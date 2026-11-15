/** Landing workspace: the office world is the default first screen, and the owner's last explicit choice
 * between it and the classic Session UI persists in local storage under LANDING_STORAGE_KEY.
 * index.html's pre-paint script applies it before any application code loads: an address without a route
 * becomes #/world unless the stored choice is the classic view, and html[data-landing="world"] keeps the
 * classic chrome hidden until the application names the workspace (01-workspace-dock.css). An address that
 * already names a route is honoured as given. The world feature writes the choice.
 * The same choice is the presentation mode everywhere: 'world' is the studio, 'chat' the classic view,
 * whose sidebar shows a lead's work as a plain list instead of the animated lead office.
 * Entry points: rememberLanding, readLanding, subscribeLanding, LANDING_STORAGE_KEY. */

export const LANDING_STORAGE_KEY = 'tofu_landing_workspace';
export type LandingWorkspace = 'world' | 'chat';

const listeners = new Set<(workspace: LandingWorkspace) => void>();

function stored(): string | null {
  try {
    return window.localStorage.getItem(LANDING_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** The pre-paint script's rule: only a stored classic choice leaves the studio. */
export function readLanding(): LandingWorkspace {
  return stored() === 'chat' ? 'chat' : 'world';
}

function announce(): void {
  const workspace = readLanding();
  for (const listener of [...listeners]) listener(workspace);
}

function storageChanged(event: StorageEvent): void {
  if (event.key === LANDING_STORAGE_KEY || event.key === null) announce();
}

export function rememberLanding(workspace: LandingWorkspace): void {
  const before = readLanding();
  try {
    window.localStorage.setItem(LANDING_STORAGE_KEY, workspace);
  } catch {
    // An unavailable store keeps the default landing; nothing else depends on the write.
  }
  if (readLanding() !== before) announce();
}

/** Calls `listener` with the new mode whenever this tab or another one changes the choice. */
export function subscribeLanding(listener: (workspace: LandingWorkspace) => void): () => void {
  if (listeners.size === 0) window.addEventListener('storage', storageChanged);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener('storage', storageChanged);
  };
}
