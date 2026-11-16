/** Floor-plan transitions: a relayout never jumps. The scene blends the layout it shows into the next one by
 * identity (zones by team, pods and seats by index within their zone, decor by its place's key), so rooms slide
 * and resize, every seated person rides their desk, and walkers' routes (routes.ts) bend with the aisles.
 * Something that exists on one side only fades without competing with the motion: a leaving room, pod or decor
 * item fades out over the first half (before neighbours move into its space), an arriving one fades in over the
 * second half (once its space has opened). A pod or desk that comes or goes inside a room that stays rides with
 * that room, keeping its offset from the room's centre, so it never floats over the bare floor beside it (the
 * scene also clips it to the room while it fades).
 * Pure. Entry points: blendLayout, easeInOut, easeOutBack, arriving, leaving. */
import type { DecorItem, OfficeLayout, Point, Rect, Seat, ZoneLayout } from './layout';

export interface Blend {
  layout: OfficeLayout;
  /** Opacity of zones, pods (`zone:index`) and decor items (by index into layout.decor) that are fading. */
  zoneAlpha: ReadonlyMap<string, number>;
  podAlpha: ReadonlyMap<string, number>;
  decorAlpha: ReadonlyArray<number>;
  watchdogAlpha: number;
}

export const easeInOut = (k: number) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
/** Lands with a slight overshoot (a document dropping onto the desk). */
export const easeOutBack = (k: number) => 1 + 2.2 * Math.pow(k - 1, 3) + 1.2 * Math.pow(k - 1, 2);

/** Opacity of something arriving or leaving at blend progress `k`. */
export const arriving = (k: number) => Math.min(1, Math.max(0, 2 * k - 1));
export const leaving = (k: number) => Math.min(1, Math.max(0, 1 - 2 * k));

const mix = (a: number, b: number, k: number) => a + (b - a) * k;
const centre = (box: Rect): Point => ({ x: box.x + box.w / 2, y: box.y + box.h / 2 });
const point = (a: Point, b: Point, k: number): Point => ({ x: mix(a.x, b.x, k), y: mix(a.y, b.y, k) });
const rect = (a: Rect, b: Rect, k: number): Rect => ({ x: mix(a.x, b.x, k), y: mix(a.y, b.y, k), w: mix(a.w, b.w, k), h: mix(a.h, b.h, k) });
const seat = (a: Seat, b: Seat, k: number): Seat => ({
  x: mix(a.x, b.x, k), y: mix(a.y, b.y, k), facing: b.facing, cut: mix(a.cut, b.cut, k), stand: point(a.stand, b.stand, k),
  desk: rect(a.desk, b.desk, k), screen: rect(a.screen, b.screen, k),
});

const shiftSeat = (s: Seat, dx: number, dy: number): Seat => ({
  ...s, x: s.x + dx, y: s.y + dy, cut: s.cut + dy, stand: { x: s.stand.x + dx, y: s.stand.y + dy },
  desk: { ...s.desk, x: s.desk.x + dx, y: s.desk.y + dy }, screen: { ...s.screen, x: s.screen.x + dx, y: s.screen.y + dy },
});

function zone(a: ZoneLayout, b: ZoneLayout, k: number, podAlpha: Map<string, number>): ZoneLayout {
  const shown = rect(a, b, k);
  const here = centre(shown), was = centre(a), will = centre(b);
  const pods = b.pods.map((pod, index) => {
    const before = a.pods[index];
    if (!before) { podAlpha.set(`${b.key}:${index}`, arriving(k)); return { ...pod, x: pod.x + here.x - will.x, y: pod.y + here.y - will.y }; }
    return { ...pod, x: mix(before.x, pod.x, k), y: mix(before.y, pod.y, k) };
  });
  a.pods.slice(b.pods.length).forEach((pod, offset) => {
    pods.push({ ...pod, x: pod.x + here.x - was.x, y: pod.y + here.y - was.y });
    podAlpha.set(`${b.key}:${b.pods.length + offset}`, leaving(k));
  });
  const seats = b.seats.map((each, index) => (a.seats[index] ? seat(a.seats[index], each, k) : shiftSeat(each, here.x - will.x, here.y - will.y)));
  seats.push(...a.seats.slice(b.seats.length).map(each => shiftSeat(each, here.x - was.x, here.y - was.y)));
  return {
    key: b.key, row: b.row, ...shown,
    banner: point(a.banner, b.banner, k), routine: point(a.routine, b.routine, k), lead: seat(a.lead, b.lead, k), pods, seats,
  };
}

export function blendLayout(from: OfficeLayout, to: OfficeLayout, k: number): Blend {
  const zoneAlpha = new Map<string, number>();
  const podAlpha = new Map<string, number>();
  const before = new Map(from.zones.map(each => [each.key, each]));
  const zones = to.zones.map(each => {
    const old = before.get(each.key);
    if (!old) { zoneAlpha.set(each.key, arriving(k)); return each; }
    return zone(old, each, k, podAlpha);
  });
  const kept = new Set(to.zones.map(each => each.key));
  for (const old of from.zones) if (!kept.has(old.key)) { zones.push(old); zoneAlpha.set(old.key, leaving(k)); }
  const decorAlpha: number[] = [];
  const earlier = new Map(from.decor.map(item => [item.key, item]));
  const decor: DecorItem[] = to.decor.map(item => {
    const old = earlier.get(item.key);
    if (!old || old.kind !== item.kind) { decorAlpha.push(arriving(k)); return item; }
    decorAlpha.push(1);
    return { ...item, x: mix(old.x, item.x, k), y: mix(old.y, item.y, k) };
  });
  const later = new Set(to.decor.map(item => item.key));
  for (const item of from.decor) if (!later.has(item.key)) { decor.push(item); decorAlpha.push(leaving(k)); }
  const watchdog = from.watchdog && to.watchdog
    ? { station: rect(from.watchdog.station, to.watchdog.station, k), seat: seat(from.watchdog.seat, to.watchdog.seat, k) }
    : to.watchdog ?? from.watchdog;
  const watchdogAlpha = from.watchdog && to.watchdog ? 1 : to.watchdog ? arriving(k) : from.watchdog ? leaving(k) : 0;
  const corridors = to.corridors.map((value, index) => mix(from.corridors[index] ?? from.corridors[from.corridors.length - 1] ?? value, value, k));
  return {
    layout: {
      width: mix(from.width, to.width, k), height: mix(from.height, to.height, k), wall: to.wall, owner: to.owner, zones, watchdog,
      entrance: point(from.entrance, to.entrance, k), lounge: rect(from.lounge, to.lounge, k), bar: rect(from.bar, to.bar, k),
      spine: to.spine, corridors, decor,
    },
    zoneAlpha, podAlpha, decorAlpha, watchdogAlpha,
  };
}
