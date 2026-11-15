import { describe, expect, it } from 'vitest';
import { FLOOR_CARD, FLOOR_HEIGHT, FLOOR_SPOTS, FLOOR_STRIP, LEAD_SPOTS, OFFICE_HEIGHT, SHAFT, SLAB, TOWER_WIDTH, feetOf, officeTower, roomAt, roomRect } from './layout';

describe('officeTower', () => {
  it('stacks the lead office above one floor per teammate at a fixed width', () => {
    expect(officeTower(1)).toEqual({ rooms: 1, width: TOWER_WIDTH, height: OFFICE_HEIGHT });
    expect(officeTower(4)).toEqual({ rooms: 4, width: TOWER_WIDTH, height: OFFICE_HEIGHT + 3 * FLOOR_HEIGHT });
  });

  it('treats a missing or zero count as the lead office alone', () => {
    expect(officeTower(0)).toEqual(officeTower(1));
    expect(officeTower(Number.NaN)).toEqual(officeTower(1));
  });

  it('keeps every station on the floor, clear of the shaft and the name card', () => {
    const feet = feetOf(FLOOR_HEIGHT);
    expect(feet).toBeGreaterThan(FLOOR_HEIGHT - FLOOR_STRIP);
    // The door fits its room under the slab.
    expect(feet - SHAFT.door.height).toBeGreaterThan(SLAB);
    // People stand left of the shaft; the desk ends before it.
    expect(FLOOR_SPOTS.door).toBeLessThan(SHAFT.x);
    expect(FLOOR_SPOTS.desk + FLOOR_SPOTS.deskWidth).toBeLessThan(SHAFT.x);
    expect(LEAD_SPOTS.front).toBeLessThan(SHAFT.x);
    // The card on the wall ends before the seated Worker and the person beside the desk.
    expect(FLOOR_CARD.x + FLOOR_CARD.width).toBeLessThan(FLOOR_SPOTS.beside);
    expect(FLOOR_CARD.y + FLOOR_CARD.height).toBeLessThan(feet);
  });
});

describe('roomRect / roomAt', () => {
  it('lays rooms top to bottom with the lead office first', () => {
    expect(roomRect(0)).toEqual({ x: 0, y: 0, width: TOWER_WIDTH, height: OFFICE_HEIGHT });
    expect(roomRect(2)).toEqual({ x: 0, y: OFFICE_HEIGHT + FLOOR_HEIGHT, width: TOWER_WIDTH, height: FLOOR_HEIGHT });
  });

  it('finds the room under a point and nothing outside the tower', () => {
    const tower = officeTower(3);
    expect(roomAt({ x: 10, y: 10 }, tower)).toBe(0);
    expect(roomAt({ x: 10, y: OFFICE_HEIGHT }, tower)).toBe(1);
    expect(roomAt({ x: TOWER_WIDTH - 1, y: tower.height - 1 }, tower)).toBe(2);
    expect(roomAt({ x: -1, y: 10 }, tower)).toBe(-1);
    expect(roomAt({ x: 10, y: tower.height }, tower)).toBe(-1);
    expect(roomAt({ x: TOWER_WIDTH, y: 10 }, tower)).toBe(-1);
  });
});
