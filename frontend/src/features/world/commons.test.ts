import { describe, expect, it } from 'vitest';
import { COMMONS_MIN_H, COMMONS_MIN_W, furnishArea } from './commons';

/** Each group placed in an area: its band, name and horizontal extent. */
function groups(area: { x: number; y: number; w: number; h: number }, seed: number) {
  const byGroup = new Map<string, { band: number; name: string; left: number; right: number }>();
  for (const piece of furnishArea(area, seed)) {
    const [band, index, name] = piece.slot.split(':');
    const key = `${band}:${index}`;
    const group = byGroup.get(key) ?? { band: Number(band), name, left: Infinity, right: -Infinity };
    group.left = Math.min(group.left, piece.x);
    group.right = Math.max(group.right, piece.x + piece.w);
    byGroup.set(key, group);
  }
  return [...byGroup.values()];
}

describe('office commons', () => {
  it('never stacks a group over its twin in the band below', () => {
    // A wide area with two bands, each holding the whole cycle of groups (the live office's lone-team row).
    for (const seed of [0, 1, 2, 3, 4, 5, 17]) {
      const placed = groups({ x: 0, y: 0, w: 1400, h: 330 }, seed);
      const upper = placed.filter(group => group.band === 0), lower = placed.filter(group => group.band === 1);
      expect(upper.length).toBeGreaterThan(3);
      expect(lower.length).toBeGreaterThan(3);
      for (const group of upper) {
        const centre = (group.left + group.right) / 2;
        const below = lower.find(each => each.left <= centre && centre <= each.right);
        if (below) expect(below.name).not.toBe(group.name);
      }
    }
  });

  it('furnishes the smallest area it admits and keeps every piece inside it', () => {
    const area = { x: 10, y: 20, w: COMMONS_MIN_W, h: COMMONS_MIN_H };
    const pieces = furnishArea(area, 3);
    expect(pieces.length).toBeGreaterThan(0);
    for (const piece of pieces) {
      expect(piece.x).toBeGreaterThanOrEqual(area.x);
      expect(piece.y).toBeGreaterThanOrEqual(area.y);
      expect(piece.x + piece.w).toBeLessThanOrEqual(area.x + area.w);
      expect(piece.y + piece.h).toBeLessThanOrEqual(area.y + area.h);
    }
  });
});
