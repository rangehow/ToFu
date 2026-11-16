import { describe, expect, it } from 'vitest';
import { layoutOffice, type TeamShape } from './layout';
import { blendLayout, easeInOut } from './tween';

const BEFORE: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 8 }, { key: '/knowledge', kind: 'project', seats: 8 },
];
const AFTER: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 17 }, { key: '/knowledge', kind: 'project', seats: 8 },
  { key: '/parallel', kind: 'project', seats: 4 },
];

describe('floor-plan transitions', () => {
  const from = layoutOffice(1920, BEFORE, 1000, 1);
  const to = layoutOffice(1920, AFTER, 1000, 1);

  it('starts exactly where the old floor was and ends exactly on the new one', () => {
    const start = blendLayout(from, to, 0).layout, end = blendLayout(from, to, 1).layout;
    for (const zone of from.zones) {
      const shown = start.zones.find(each => each.key === zone.key)!;
      expect([shown.x, shown.y, shown.w, shown.h]).toEqual([zone.x, zone.y, zone.w, zone.h]);
      expect(shown.seats.slice(0, zone.seats.length).map(seat => [seat.x, seat.y])).toEqual(zone.seats.map(seat => [seat.x, seat.y]));
    }
    for (const zone of to.zones) {
      const shown = end.zones.find(each => each.key === zone.key)!;
      expect([shown.x, shown.y, shown.w, shown.h]).toEqual([zone.x, zone.y, zone.w, zone.h]);
    }
    expect([start.width, start.height]).toEqual([from.width, from.height]);
  });

  it('moves every room, desk and corridor continuously in between', () => {
    let previous = blendLayout(from, to, 0).layout;
    for (let step = 1; step <= 40; step += 1) {
      const next = blendLayout(from, to, easeInOut(step / 40)).layout;
      for (const zone of from.zones) {
        const a = previous.zones.find(each => each.key === zone.key)!, b = next.zones.find(each => each.key === zone.key)!;
        expect(Math.abs(b.x - a.x) + Math.abs(b.y - a.y)).toBeLessThan(120);
        a.seats.forEach((seat, index) => expect(Math.abs(b.seats[index].x - seat.x) + Math.abs(b.seats[index].y - seat.y)).toBeLessThan(120));
      }
      previous = next;
    }
  });

  it('fades a new room and a new pod in where they will be, never popping them', () => {
    const quarter = blendLayout(from, to, 0.25);
    expect(quarter.zoneAlpha.get('/parallel')).toBe(0.25);
    const tofu = to.zones.find(zone => zone.key === '/tofu')!;
    expect(tofu.pods.length).toBeGreaterThan(from.zones.find(zone => zone.key === '/tofu')!.pods.length);
    expect(quarter.podAlpha.get(`/tofu:${tofu.pods.length - 1}`)).toBe(0.25);
    expect(quarter.zoneAlpha.has('/tofu')).toBe(false);
    const gone = blendLayout(to, from, 0.25);
    expect(gone.zoneAlpha.get('/parallel')).toBe(0.75);
    expect(gone.layout.zones.some(zone => zone.key === '/parallel')).toBe(true);
  });
});
