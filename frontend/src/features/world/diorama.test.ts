import { describe, expect, it } from 'vitest';
import { gradeAt, roomFurniture, roomLights } from './diorama';
import { layoutOffice, type TeamShape } from './layout';

const TEAMS: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 16 },
  { key: '/parallel', kind: 'project', seats: 5 }, { key: '/watchdog', kind: 'watchdog', seats: 0 },
];
const overlap = (a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

describe('office light and rooms', () => {
  it('grades the office by the local hour: deep night, dawn, day, golden hour and dusk', () => {
    expect(gradeAt(23)).toMatchObject({ phase: 'night', dark: 1, sun: 0 });
    expect(gradeAt(3).phase).toBe('night');
    expect(gradeAt(6).phase).toBe('dawn');
    expect(gradeAt(12)).toMatchObject({ phase: 'day', dark: 0, sun: 1 });
    expect(gradeAt(18).phase).toBe('golden');
    expect(gradeAt(20).phase).toBe('dusk');
    expect(gradeAt(19).dark).toBeLessThan(gradeAt(20).dark);
    expect(gradeAt(20).dark).toBeLessThan(gradeAt(22).dark);
  });

  for (const height of [0, 1400]) {
    it(`furnishes each room only where nobody walks or sits (window ${height || 'auto'} high)`, () => {
      const layout = layoutOffice(1920, TEAMS, height, 1);
      for (const zone of layout.zones) {
        const taken = [zone.lead.desk, ...zone.pods.map(pod => ({ x: pod.x - 4, y: pod.y, w: pod.w + 8, h: pod.h }))];
        for (const piece of roomFurniture(zone)) {
          // Inside the room, right of its aisle, clear of desks and pods and of the head desk's walk to the aisle.
          expect(piece.x).toBeGreaterThanOrEqual(zone.x + 26);
          expect(piece.x + piece.w).toBeLessThanOrEqual(zone.x + zone.w);
          expect(piece.y + piece.h).toBeLessThanOrEqual(zone.y + zone.h);
          for (const box of taken) expect(overlap(piece, box)).toBe(false);
          const leadWalk = { x: zone.x, y: zone.lead.stand.y - 6, w: zone.lead.desk.x - zone.x, h: 12 };
          if (piece.kind !== 'rug') expect(overlap(piece, leadWalk)).toBe(false);
        }
      }
    });
  }

  it('lights every pod, head desk and floor lamp for the night', () => {
    const layout = layoutOffice(1920, TEAMS, 1400, 1);
    const lights = roomLights(layout);
    const pods = layout.zones.reduce((sum, zone) => sum + zone.pods.length, 0);
    const lamps = layout.zones.reduce((sum, zone) => sum + roomFurniture(zone).filter(piece => piece.kind === 'lamp').length, 0);
    expect(lamps).toBeGreaterThan(0);
    expect(lights.length).toBe(1 + layout.zones.length + pods + lamps + (layout.watchdog ? 1 : 0) + 3);
  });
});
