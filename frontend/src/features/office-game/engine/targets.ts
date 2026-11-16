/** Spatial keyboard navigation over a stage's target layer: from one target, the nearest target inside the
 * pressed direction's 90° cone, or, when the cone is empty, the nearest one anywhere on that side.
 * Pure geometry, no DOM. Entry: nearestInDirection.
 */
import type { Vec } from './camera';

export type NavDirection = 'left' | 'right' | 'up' | 'down';

export function nearestInDirection<T extends Vec>(from: Vec, targets: ReadonlyArray<T>, direction: NavDirection): T | null {
  let best: T | null = null;
  let bestScore = Infinity;
  let bestInCone = false;
  for (const target of targets) {
    const dx = target.x - from.x;
    const dy = target.y - from.y;
    const along = direction === 'left' ? -dx : direction === 'right' ? dx : direction === 'up' ? -dy : dy;
    const across = direction === 'left' || direction === 'right' ? Math.abs(dy) : Math.abs(dx);
    if (along <= 1) continue;
    const inCone = along >= across;
    const score = Math.hypot(along, across);
    if ((inCone && !bestInCone) || (inCone === bestInCone && score < bestScore)) { best = target; bestScore = score; bestInCone = inCone; }
  }
  return best;
}
