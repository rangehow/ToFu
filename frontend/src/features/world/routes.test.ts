import { describe, expect, it } from 'vitest';
import { layoutOffice, type OfficeLayout, type Point, type Rect, type TeamShape } from './layout';
import { LEAD_SLOT, pointAlong, rerouteFrom, resolve, routeBetween, routeLength, seatAt, type Place } from './routes';
import { blendLayout } from './tween';

const TEAMS: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 3 },
  { key: '/tofu', kind: 'project', seats: 18 },
  { key: '/parallel', kind: 'project', seats: 3 },
  { key: '/knowledge', kind: 'project', seats: 7 },
  { key: '/extra', kind: 'project', seats: 9 },
  { key: '/watchdog', kind: 'watchdog', seats: 0 },
];
const DROP: Place = { kind: 'drop' };
const OUTSIDE: Place = { kind: 'outside' };

function desks(layout: OfficeLayout): Place[] {
  const places: Place[] = [{ kind: 'watchdog' }];
  for (const zone of layout.zones) {
    places.push({ kind: 'seat', zone: zone.key, slot: LEAD_SLOT });
    zone.seats.forEach((_, slot) => places.push({ kind: 'seat', zone: zone.key, slot }));
  }
  return places;
}
const axisAligned = (points: Point[]) => points.every((point, index) => index === 0
  || Math.abs(point.x - points[index - 1].x) < 0.5 || Math.abs(point.y - points[index - 1].y) < 0.5);
/** No step reverses the previous one along the same line. */
const neverDoublesBack = (points: Point[]) => points.every((point, index) => {
  if (index < 2) return true;
  const a = points[index - 2], b = points[index - 1];
  const collinear = (Math.abs(a.x - b.x) < 0.5 && Math.abs(b.x - point.x) < 0.5) || (Math.abs(a.y - b.y) < 0.5 && Math.abs(b.y - point.y) < 0.5);
  return !collinear || (b.x - a.x) * (point.x - b.x) + (b.y - a.y) * (point.y - b.y) >= 0;
});
/** Whether a segment passes through a rectangle's inside (touching its edge is not crossing). */
function crosses(a: Point, b: Point, rect: Rect): boolean {
  const left = Math.min(a.x, b.x), right = Math.max(a.x, b.x), top = Math.min(a.y, b.y), bottom = Math.max(a.y, b.y);
  return left < rect.x + rect.w - 0.5 && right > rect.x + 0.5 && top < rect.y + rect.h - 0.5 && bottom > rect.y + 0.5;
}

describe('office walkways', () => {
  const layout = layoutOffice(1920, TEAMS);

  it('walks every desk to the owner\'s desk along straight walkways, and back the same way', () => {
    for (const place of desks(layout)) {
      const there = resolve(routeBetween(place, DROP, layout)!, layout);
      const seat = seatAt(layout, place)!;
      expect(there[0]).toEqual({ x: seat.x, y: seat.y });
      expect(there[there.length - 1]).toEqual(layout.owner.drop);
      expect(axisAligned(there)).toBe(true);
      expect(neverDoublesBack(there)).toBe(true);
      expect(resolve(routeBetween(DROP, place, layout)!, layout)).toEqual([...there].reverse());
    }
  });

  it('walks round head desks and the Watchdog\'s console instead of through them', () => {
    const consoles = [...layout.zones.map(zone => zone.lead.desk), layout.watchdog!.seat.desk];
    for (const place of desks(layout)) {
      const points = resolve(routeBetween(place, OUTSIDE, layout)!, layout);
      for (let index = 1; index < points.length; index += 1) {
        for (const desk of consoles) expect(crosses(points[index - 1], points[index], desk)).toBe(false);
      }
    }
  });

  it('comes in and goes out through the entrance', () => {
    for (const place of desks(layout)) {
      const inward = resolve(routeBetween(OUTSIDE, place, layout)!, layout);
      expect(inward[0]).toEqual(layout.entrance);
      expect(axisAligned(inward)).toBe(true);
    }
  });

  it('bends a route with the floor while rooms move instead of leaving it on the old aisles', () => {
    const grown = layoutOffice(1920, TEAMS.map(team => (team.key === '/router' ? { ...team, seats: 20 } : team)));
    const place: Place = { kind: 'seat', zone: '/knowledge', slot: 2 };
    const route = routeBetween(place, OUTSIDE, layout)!;
    const before = resolve(route, layout), after = resolve(route, grown);
    const midway = resolve(route, blendLayout(layout, grown, 0.5).layout);
    expect(seatAt(grown, place)).not.toEqual(seatAt(layout, place));
    midway.forEach((point, index) => {
      expect(point.x).toBeCloseTo((before[index].x + after[index].x) / 2, 5);
      expect(point.y).toBeCloseTo((before[index].y + after[index].y) / 2, 5);
    });
  });

  it('turns a walker round where it stands when its destination changes, however often it changes', () => {
    const seat: Place = { kind: 'seat', zone: '/tofu', slot: 5 };
    const out = routeBetween(seat, OUTSIDE, layout)!;
    const outPoints = resolve(out, layout);
    const halfway = routeLength(outPoints) / 2;
    const here = pointAlong(outPoints, halfway).point;
    const back = rerouteFrom(out, halfway, OUTSIDE, seat, layout)!;
    const backPoints = resolve(back, layout);
    expect(backPoints[0]).toEqual(here);
    expect(routeLength(backPoints)).toBeCloseTo(halfway, 5);
    expect(backPoints[backPoints.length - 1]).toEqual(outPoints[0]);
    // Changing its mind again on the way back still keeps to the walkways.
    const third = rerouteFrom(back, halfway / 3, seat, { kind: 'spot', index: 0 }, layout)!;
    const thirdPoints = resolve(third, layout);
    expect(thirdPoints[0]).toEqual(pointAlong(backPoints, halfway / 3).point);
    expect(axisAligned(thirdPoints)).toBe(true);
    expect(neverDoublesBack(thirdPoints)).toBe(true);
  });
});
