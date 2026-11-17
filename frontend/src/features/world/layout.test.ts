import { describe, expect, it } from 'vitest';
import {
  allocateSlots, deskTagY, fitOffice, layoutOffice, SEATS_PER_POD, shelfSlots, TAG_GAP, TAG_PILL, tagUnit, waitingSpots,
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
      // The commons in front of the office keeps the line and the walk from the door down to the corridor clear.
      const lobby = layoutOffice(width, TEAMS, 1400, 1).commons.find(area => area.key === 'commons:owner');
      if (lobby) {
        expect(lobby.y).toBeGreaterThan(spots[0].y + 30);
        expect(lobby.x + lobby.w).toBeLessThan(door.x - 20);
      }
    }
    expect(layoutOffice(1920, TEAMS, 1400, 1).commons.some(area => area.key === 'commons:owner')).toBe(true);
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

  it('fits the whole floor to the window: a short office drawn bigger, a crowded one laid out wider, never letterboxed', () => {
    const small: TeamShape[] = [{ key: '/router', kind: 'router', seats: 2 }, { key: '/tofu', kind: 'project', seats: 6 }];
    const { scale, layout } = fitOffice(1920, 1036, small);
    expect(scale).toBeGreaterThan(1);
    expect(layout.width * scale).toBeLessThanOrEqual(1920 + 0.5);
    expect(layout.height * scale).toBeGreaterThanOrEqual(1036 - 1);
    const crowded = fitOffice(1920, 1036, TEAMS.concat(Array.from({ length: 12 }, (_, i) => ({ key: `/p${i}`, kind: 'project' as const, seats: 40 }))));
    expect(crowded.scale).toBeLessThan(1);
    expect(crowded.layout.height * crowded.scale).toBeLessThanOrEqual(1036);
    expect(crowded.layout.width * crowded.scale).toBeCloseTo(1920, -1);
    expect(fitOffice(768, 900, small).scale).toBeCloseTo(0.6, 5);
  });

  it('lays the teams out in rows of equal-height rooms that reach the right wall, with room to spare as a commons', () => {
    for (const width of [1920, 1440, 1024]) {
      const layout = layoutOffice(width, TEAMS);
      const rows = new Map<number, typeof layout.zones>();
      for (const zone of layout.zones) rows.set(zone.row, [...(rows.get(zone.row) || []), zone]);
      for (const [row, zones] of rows) {
        const last = zones[zones.length - 1];
        const area = layout.commons.find(each => each.key === `commons:${row}`);
        expect(area ? area.x + area.w : last.x + last.w).toBeCloseTo(layout.width - 40, 5);
        for (const zone of zones) expect(zone.h).toBe(zones[0].h);
        if (area) {
          expect(area.x).toBeGreaterThan(last.x + last.w);
          expect(area.h).toBe(last.h);
        }
      }
    }
    // A lone small team in the last row keeps a room its desks fill; the rest of the row is the commons.
    const layout = layoutOffice(1920, TEAMS);
    const knowledge = layout.zones.find(zone => zone.key === '/knowledge')!;
    const area = layout.commons.find(each => each.key === `commons:${knowledge.row}`);
    expect(area).toBeDefined();
    expect(knowledge.w).toBeLessThan(area!.w);
  });

  it('furnishes each commons inside its area, clear of the rooms, with furniture keyed by its place', () => {
    for (const width of [1920, 1440]) {
      for (const height of [0, 1400]) {
        const layout = layoutOffice(width, TEAMS, height, 1);
        const rooms = [layout.owner.room, ...layout.zones];
        for (const area of layout.commons) {
          for (const room of rooms) expect(overlap(area, room)).toBe(false);
          const pieces = layout.decor.filter(item => item.key.startsWith(`${area.key}:`));
          expect(pieces.length).toBeGreaterThan(0);
          for (const piece of pieces) {
            const box = { x: piece.x, y: piece.y, w: piece.w ?? 0, h: piece.h ?? 0 };
            expect(box.x).toBeGreaterThanOrEqual(area.x);
            expect(box.y).toBeGreaterThanOrEqual(area.y);
            expect(box.x + box.w).toBeLessThanOrEqual(area.x + area.w);
            expect(box.y + box.h).toBeLessThanOrEqual(area.y + area.h);
          }
        }
        expect(new Set(layout.decor.map(item => item.key)).size).toBe(layout.decor.length);
      }
    }
  });

  it('keeps two lines of desk tags clear of each other, of the head desk and of the room edge at every fit zoom', () => {
    const teams: TeamShape[] = [
      { key: '/router', kind: 'router', seats: 3 }, { key: '/tofu', kind: 'project', seats: 21 },
      { key: '/parallel', kind: 'project', seats: 7 }, { key: '/knowledge', kind: 'project', seats: 5 },
      { key: '/watchdog', kind: 'watchdog', seats: 0 },
    ];
    let stacked = 0;
    for (const [width, height] of [[800, 852], [1024, 700], [1366, 720], [1440, 852], [1920, 1032], [2560, 1400]]) {
      const { scale, layout } = fitOffice(width, height, teams);
      // The camera never zooms out past the fit, so the fit zoom is where tags are biggest on the floor.
      const pixel = 1 / scale, half = (TAG_PILL * tagUnit(pixel)) / 2, clear = TAG_GAP * pixel - 1e-6;
      for (const zone of layout.zones) {
        const reach = zone.pods.map((pod, index) => {
          const seats = zone.seats.slice(index * SEATS_PER_POD, (index + 1) * SEATS_PER_POD);
          const front = seats.find(seat => seat.facing === 'down')!, back = seats.find(seat => seat.facing === 'up')!;
          return { pod, top: deskTagY(front, 1, pixel) - half, bottom: deskTagY(back, 1, pixel) + half };
        });
        for (const upper of reach) {
          expect(upper.bottom + clear).toBeLessThanOrEqual(zone.y + zone.h);
          const lower = reach.find(each => each.pod.x === upper.pod.x && each.pod.y > upper.pod.y);
          if (lower) { stacked += 1; expect(upper.bottom + clear).toBeLessThanOrEqual(lower.top); }
          else if (!reach.some(each => each.pod.x === upper.pod.x && each.pod.y < upper.pod.y)) {
            expect(upper.top - clear).toBeGreaterThanOrEqual(zone.lead.desk.y + zone.lead.desk.h);
          }
        }
        for (const first of reach.filter(each => !reach.some(other => other.pod.x === each.pod.x && other.pod.y < each.pod.y))) {
          expect(first.top - clear).toBeGreaterThanOrEqual(zone.lead.desk.y + zone.lead.desk.h);
        }
      }
    }
    expect(stacked).toBeGreaterThan(0);
  });

  it('stacks a team\'s pods when that lets the following teams share its row', () => {
    const teams: TeamShape[] = [
      { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 16 },
      { key: '/parallel', kind: 'project', seats: 8 }, { key: '/knowledge', kind: 'project', seats: 8 },
    ];
    const layout = layoutOffice(1920, teams, 0, 1);
    expect(new Set(layout.zones.map(zone => zone.row))).toEqual(new Set([0]));
    const tofu = layout.zones.find(zone => zone.key === '/tofu')!;
    expect(new Set(tofu.pods.map(pod => pod.x)).size).toBe(1);
  });

  it('gives a window taller than the plan to the rooms, not to bare floor, keeping the sign and head desk at the top', () => {
    const short = layoutOffice(1920, TEAMS, 0, 1);
    const tall = layoutOffice(1920, TEAMS, 1600, 1);
    expect(tall.height).toBe(1600);
    const bottomStrip = short.height - (short.corridors[short.corridors.length - 1] + 22);
    expect(tall.corridors[tall.corridors.length - 1] + 22).toBeCloseTo(tall.height - bottomStrip, 5);
    for (const zone of tall.zones) {
      const before = short.zones.find(each => each.key === zone.key)!;
      expect(zone.h).toBeGreaterThan(before.h);
      expect(zone.lead.desk.y - zone.y).toBeCloseTo(before.lead.desk.y - before.y, 5);
    }
  });

  it('keeps every seat while its person stays and fills the lowest vacancy', () => {
    const first = allocateSlots(new Map(), ['a', 'b', 'c']);
    const second = allocateSlots(first, ['c', 'd']);
    expect(second.get('c')).toBe(2);
    expect(second.get('d')).toBe(0);
  });

});
