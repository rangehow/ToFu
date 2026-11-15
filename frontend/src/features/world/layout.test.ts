import { describe, expect, it } from 'vitest';
import {
  allocateSlots, fitOffice, layoutOffice, nearestInDirection, pathLength, shelfSlots, waitingSpots, walkHome, walkOut, walkToOwner,
  type Rect, type TeamShape,
} from './layout';

const TEAMS: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 3 },
  { key: '/harness', kind: 'project', seats: 0 },
  { key: '/tofu', kind: 'project', seats: 18 },
  { key: '/parallel', kind: 'project', seats: 3 },
  { key: '/knowledge', kind: 'project', seats: 7 },
  { key: '/watchdog', kind: 'watchdog', seats: 0 },
];
const overlap = (a: Rect, b: Rect) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const inside = (point: { x: number; y: number }, rect: Rect) =>
  point.x >= rect.x && point.x <= rect.x + rect.w && point.y >= rect.y && point.y <= rect.y + rect.h;

describe('open-plan floor', () => {
  for (const width of [1920, 1440, 1280, 1024, 768]) {
    it(`gives every team a zone of desks that never overlaps another zone or the owner's office at ${width}px`, () => {
      const layout = layoutOffice(width, TEAMS);
      expect(layout.zones.map(zone => zone.key)).toEqual(['/router', '/harness', '/tofu', '/parallel', '/knowledge']);
      expect(layout.watchdog).not.toBeNull();
      const rects = [layout.owner.room, ...layout.zones];
      rects.forEach((a, i) => rects.slice(i + 1).forEach(b => expect(overlap(a, b)).toBe(false)));
      for (const zone of layout.zones) {
        expect(zone.x + zone.w).toBeLessThanOrEqual(layout.width);
        for (const seat of zone.seats) expect(inside(seat, zone)).toBe(true);
      }
      expect(layout.zones.find(zone => zone.key === '/tofu')!.seats.length).toBeGreaterThanOrEqual(18);
      expect(layout.zones.find(zone => zone.key === '/harness')!.seats).toHaveLength(0);
    });
  }

  it('reaches the owner desk from every desk along straight aisles, and walks back the same way', () => {
    const layout = layoutOffice(1920, TEAMS);
    for (const zone of layout.zones) {
      for (const seat of [zone.lead, ...zone.seats]) {
        const path = walkToOwner(layout, seat);
        expect(path[0]).toEqual({ x: seat.x, y: seat.y });
        expect(path[path.length - 1]).toEqual(layout.owner.drop);
        for (let i = 1; i < path.length; i += 1) expect(path[i].x === path[i - 1].x || path[i].y === path[i - 1].y).toBe(true);
        expect(walkHome(layout, seat)).toEqual([...path].reverse());
      }
    }
    const out = walkOut(layout);
    expect(out[0]).toEqual(layout.owner.drop);
    expect(out[out.length - 1].x).toBe(layout.entrance.x);
    expect(pathLength(out)).toBeGreaterThan(0);
  });

  it('lines people with an open owner question up in front of the office, beside the door and off every zone', () => {
    for (const width of [1920, 1280, 768]) {
      const layout = layoutOffice(width, TEAMS);
      const { room, door } = layout.owner;
      const spots = waitingSpots(layout);
      expect(spots.length).toBeGreaterThanOrEqual(6);
      expect(spots[0].x).toBeLessThan(door.x);
      for (const spot of spots) {
        expect(spot.y).toBeGreaterThan(room.y + room.h);
        expect(spot.x).toBeGreaterThanOrEqual(room.x);
        for (const zone of layout.zones) expect(inside(spot, zone)).toBe(false);
      }
    }
  });

  it('fits the whiteboard and the trophy shelf inside the owner office, clear of the desk and of each other', () => {
    for (const width of [1920, 768]) {
      const { room, desk, whiteboard, shelf } = layoutOffice(width, TEAMS).owner;
      for (const rect of [whiteboard, shelf]) {
        expect(rect.x).toBeGreaterThanOrEqual(room.x);
        expect(rect.x + rect.w).toBeLessThanOrEqual(room.x + room.w);
        expect(rect.y).toBeGreaterThanOrEqual(room.y);
        expect(overlap(rect, desk)).toBe(false);
      }
      expect(overlap(whiteboard, shelf)).toBe(false);
      const slots = shelfSlots(shelf);
      expect(slots.length).toBe(16);
      for (const slot of slots) expect(inside(slot, shelf)).toBe(true);
    }
  });

  it('draws a short office bigger instead of leaving empty floor, never wider than the window', () => {
    const small: TeamShape[] = [{ key: '/router', kind: 'router', seats: 2 }, { key: '/tofu', kind: 'project', seats: 6 }];
    const { scale, layout } = fitOffice(1920, 1036, small);
    expect(scale).toBeGreaterThan(1);
    expect(layout.width * scale).toBeLessThanOrEqual(1920 + 0.5);
    expect(layout.height * scale).toBeGreaterThanOrEqual(1036 - 1);
    const crowded = fitOffice(1920, 1036, TEAMS.concat(Array.from({ length: 12 }, (_, i) => ({ key: `/p${i}`, kind: 'project' as const, seats: 40 }))));
    expect(crowded.scale).toBe(1);
    expect(crowded.layout.height).toBeGreaterThan(1036);
    expect(fitOffice(768, 900, small).scale).toBeCloseTo(0.6, 5);
  });

  it('keeps every seat while its person stays and fills the lowest vacancy', () => {
    const first = allocateSlots(new Map(), ['a', 'b', 'c']);
    const second = allocateSlots(first, ['c', 'd']);
    expect(second.get('c')).toBe(2);
    expect(second.get('d')).toBe(0);
  });

  it('moves keyboard focus to the nearest person in the pressed direction', () => {
    const targets = [{ x: 0, y: 0, id: 'a' }, { x: 100, y: 4, id: 'b' }, { x: 60, y: 200, id: 'c' }, { x: -90, y: 0, id: 'd' }];
    expect(nearestInDirection(targets[0], targets.slice(1), 'right')?.id).toBe('b');
    expect(nearestInDirection(targets[0], targets.slice(1), 'down')?.id).toBe('c');
    expect(nearestInDirection(targets[0], targets.slice(1), 'left')?.id).toBe('d');
    expect(nearestInDirection(targets[0], targets.slice(1), 'up')).toBeNull();
  });
});
