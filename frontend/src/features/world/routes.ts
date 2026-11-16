/** Office walkways as symbolic waypoints. A waypoint is a function of the layout, so the route a person walks
 * bends with the floor while rooms slide (tween.ts) instead of leaving the walker floating off the aisles.
 * Every place has one path to the hub in front of the owner's door, along the walkways layout.ts reserves: a
 * desk's stand line, its zone's aisle, the corridor under its row, the spine along the left wall, the bottom
 * lane to the entrance. A route between two places joins both paths at the hub and drops every U-turn, so
 * nobody doubles back. A walker whose destination changes mid-route is rerouted through the destination it
 * had (every route ends at a place, however often it was rerouted) and the same U-turn rule trims the
 * excursion, so it turns round where it stands when the new place lies behind it.
 * Pure. Entry points: routeBetween, rerouteFrom, resolve, routeLength, pointAlong, seatAt. */
import { aisleOf, bottomLane, corridorBelow, waitingSpots, type OfficeLayout, type Point, type Seat, type ZoneLayout } from './layout';

export type Place =
  | { kind: 'seat'; zone: string; slot: number }
  | { kind: 'watchdog' }
  | { kind: 'drop' }
  | { kind: 'spot'; index: number }
  | { kind: 'outside' };
export type Waypoint = (layout: OfficeLayout) => Point | null;

/** The lead's desk in a zone: `slot` -1. */
export const LEAD_SLOT = -1;
/** How far beside a head desk its person steps to walk round it. */
const ROUND_DESK = 14;

const zoneOf = (layout: OfficeLayout, key: string): ZoneLayout | null => layout.zones.find(zone => zone.key === key) ?? null;

export function seatAt(layout: OfficeLayout, place: Place): Seat | null {
  if (place.kind === 'watchdog') return layout.watchdog?.seat ?? null;
  if (place.kind !== 'seat') return null;
  const zone = zoneOf(layout, place.zone);
  if (!zone) return null;
  return place.slot === LEAD_SLOT ? zone.lead : zone.seats[place.slot] ?? null;
}

export function samePlace(a: Place | null, b: Place | null): boolean {
  if (!a || !b || a.kind !== b.kind) return false;
  if (a.kind === 'seat' && b.kind === 'seat') return a.zone === b.zone && a.slot === b.slot;
  if (a.kind === 'spot' && b.kind === 'spot') return a.index === b.index;
  return true;
}

const lane = (layout: OfficeLayout) => layout.corridors[0];
const hub: Waypoint = layout => ({ x: layout.owner.door.x, y: lane(layout) });

/** A desk with its own chair and stand line (head desks and the Watchdog's console have the desk between chair
 * and stand line, so their person walks round its left end). */
function deskPath(seat: (layout: OfficeLayout) => Seat | null, roundDesk: boolean): Waypoint[] {
  const points: Waypoint[] = [layout => { const s = seat(layout); return s && { x: s.x, y: s.y }; }];
  if (roundDesk) {
    points.push(layout => { const s = seat(layout); return s && { x: s.desk.x - ROUND_DESK, y: s.y }; });
    points.push(layout => { const s = seat(layout); return s && { x: s.desk.x - ROUND_DESK, y: s.stand.y }; });
  } else {
    points.push(layout => { const s = seat(layout); return s && { x: s.stand.x, y: s.stand.y }; });
  }
  return points;
}

/** From a place to the hub. `layout` decides the path's shape (which corridors it takes); positions resolve later. */
function pathToHub(place: Place, layout: OfficeLayout): Waypoint[] {
  switch (place.kind) {
    case 'seat': {
      const zone = zoneOf(layout, place.zone);
      if (!zone) return [];
      const key = place.zone;
      const seat = (current: OfficeLayout) => seatAt(current, place);
      const aisle = (current: OfficeLayout) => { const z = zoneOf(current, key); return z ? aisleOf(z) : null; };
      const corridor = (current: OfficeLayout) => { const z = zoneOf(current, key); return z ? corridorBelow(z) : null; };
      const points = deskPath(seat, place.slot === LEAD_SLOT);
      points.push(current => { const s = seat(current), a = aisle(current); return s && a !== null ? { x: a, y: s.stand.y } : null; });
      points.push(current => { const a = aisle(current), c = corridor(current); return a !== null && c !== null ? { x: a, y: c } : null; });
      if (zone.row > 0) {
        points.push(current => { const c = corridor(current); return c === null ? null : { x: current.spine, y: c }; });
        points.push(current => ({ x: current.spine, y: lane(current) }));
      }
      points.push(hub);
      return points;
    }
    case 'watchdog': {
      if (!layout.watchdog) return [];
      const seat = (current: OfficeLayout) => current.watchdog?.seat ?? null;
      return [...deskPath(seat, true),
        current => { const s = seat(current); return s && { x: s.desk.x - ROUND_DESK, y: bottomLane(current) }; },
        current => ({ x: current.spine, y: bottomLane(current) }),
        current => ({ x: current.spine, y: lane(current) }),
        hub];
    }
    case 'drop':
      return [current => ({ ...current.owner.drop }), current => ({ x: current.owner.door.x, y: current.owner.drop.y }), hub];
    case 'spot':
      return [current => waitingSpots(current)[place.index] ?? null,
        current => { const spot = waitingSpots(current)[place.index]; return spot ? { x: spot.x, y: lane(current) } : null; },
        hub];
    case 'outside':
      return [current => ({ x: current.entrance.x, y: current.entrance.y }),
        current => ({ x: current.entrance.x, y: bottomLane(current) }),
        current => ({ x: current.spine, y: bottomLane(current) }),
        current => ({ x: current.spine, y: lane(current) }),
        hub];
  }
}

const near = (a: Point, b: Point) => Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5;

/** Drops waypoints that resolve nowhere, repeat their predecessor, or turn straight back on a line. */
function simplify(points: Waypoint[], layout: OfficeLayout): Waypoint[] {
  let list = points.map(point => ({ point, at: point(layout) })).filter((item): item is { point: Waypoint; at: Point } => item.at !== null);
  for (let changed = true; changed;) {
    changed = false;
    const next: typeof list = [];
    for (const item of list) {
      const last = next[next.length - 1];
      if (last && near(last.at, item.at)) { changed = true; continue; }
      next.push(item);
    }
    for (let index = 1; index < next.length - 1; index += 1) {
      const a = next[index - 1].at, b = next[index].at, c = next[index + 1].at;
      const collinear = (Math.abs(a.x - b.x) < 0.5 && Math.abs(b.x - c.x) < 0.5) || (Math.abs(a.y - b.y) < 0.5 && Math.abs(b.y - c.y) < 0.5);
      if (collinear && (b.x - a.x) * (c.x - b.x) + (b.y - a.y) * (c.y - b.y) < 0) { next.splice(index, 1); changed = true; index -= 1; }
    }
    list = next;
  }
  return list.map(item => item.point);
}

/** The route between two places, or null when either is not on this floor. */
export function routeBetween(from: Place, to: Place, layout: OfficeLayout): Waypoint[] | null {
  const a = pathToHub(from, layout), b = pathToHub(to, layout);
  if (!a.length || !b.length) return null;
  const route = simplify([...a, ...b.slice(0, -1).reverse()], layout);
  return route.length ? route : null;
}

export function resolve(route: ReadonlyArray<Waypoint>, layout: OfficeLayout, last: Point[] = []): Point[] {
  const points: Point[] = [];
  route.forEach((waypoint, index) => {
    const point = waypoint(layout) ?? last[index] ?? points[points.length - 1];
    if (point) points.push(point);
  });
  return points;
}

export function routeLength(points: ReadonlyArray<Point>): number {
  let total = 0;
  for (let index = 1; index < points.length; index += 1) total += Math.hypot(points[index].x - points[index - 1].x, points[index].y - points[index - 1].y);
  return total;
}

/** Where `distance` along the route lands, the segment it is on and that segment's heading. */
export function pointAlong(points: ReadonlyArray<Point>, distance: number): { point: Point; segment: number; dx: number; dy: number } {
  let travelled = 0;
  for (let index = 1; index < points.length; index += 1) {
    const a = points[index - 1], b = points[index];
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    if (travelled + length >= distance || index === points.length - 1) {
      const share = length ? Math.min(1, Math.max(0, (distance - travelled) / length)) : 1;
      return { point: { x: a.x + (b.x - a.x) * share, y: a.y + (b.y - a.y) * share }, segment: index - 1, dx: b.x - a.x, dy: b.y - a.y };
    }
    travelled += length;
  }
  const only = points[0] ?? { x: 0, y: 0 };
  return { point: { ...only }, segment: 0, dx: 0, dy: 0 };
}

/** A walker at `distance` along `route` (which ends at `destination`) heads for `to` instead. */
export function rerouteFrom(route: ReadonlyArray<Waypoint>, distance: number, destination: Place, to: Place, layout: OfficeLayout): Waypoint[] | null {
  const onward = routeBetween(destination, to, layout);
  if (!onward) return null;
  const { point, segment } = pointAlong(resolve(route, layout), distance);
  const here: Waypoint = () => ({ ...point });
  return simplify([here, ...route.slice(segment + 1), ...onward.slice(1)], layout);
}
