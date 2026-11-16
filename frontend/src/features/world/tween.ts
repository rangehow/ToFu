/** Floor-plan transitions: a relayout never jumps. The scene blends the layout it shows into the next one by
 * identity (zones by team, pods and seats by index within their zone, decor by index and kind), so rooms slide
 * and resize, every seated person rides their desk, and walkers' routes (routes.ts) bend with the aisles. A zone,
 * pod or decor item that exists on one side only fades: in at its new place, out at its old one.
 * Pure. Entry points: blendLayout, easeInOut, easeOutBack. */
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

const mix = (a: number, b: number, k: number) => a + (b - a) * k;
const point = (a: Point, b: Point, k: number): Point => ({ x: mix(a.x, b.x, k), y: mix(a.y, b.y, k) });
const rect = (a: Rect, b: Rect, k: number): Rect => ({ x: mix(a.x, b.x, k), y: mix(a.y, b.y, k), w: mix(a.w, b.w, k), h: mix(a.h, b.h, k) });
const seat = (a: Seat, b: Seat, k: number): Seat => ({
  x: mix(a.x, b.x, k), y: mix(a.y, b.y, k), facing: b.facing, cut: mix(a.cut, b.cut, k), stand: point(a.stand, b.stand, k),
  desk: rect(a.desk, b.desk, k), screen: rect(a.screen, b.screen, k),
});

function zone(a: ZoneLayout, b: ZoneLayout, k: number, podAlpha: Map<string, number>): ZoneLayout {
  const pods = b.pods.map((pod, index) => {
    const before = a.pods[index];
    if (!before) { podAlpha.set(`${b.key}:${index}`, k); return pod; }
    return { ...pod, x: mix(before.x, pod.x, k), y: mix(before.y, pod.y, k) };
  });
  a.pods.slice(b.pods.length).forEach((pod, offset) => { pods.push(pod); podAlpha.set(`${b.key}:${b.pods.length + offset}`, 1 - k); });
  const seats = b.seats.map((each, index) => (a.seats[index] ? seat(a.seats[index], each, k) : each));
  seats.push(...a.seats.slice(b.seats.length));
  return {
    key: b.key, row: b.row, x: mix(a.x, b.x, k), y: mix(a.y, b.y, k), w: mix(a.w, b.w, k), h: mix(a.h, b.h, k),
    banner: point(a.banner, b.banner, k), routine: point(a.routine, b.routine, k), lead: seat(a.lead, b.lead, k), pods, seats,
  };
}

export function blendLayout(from: OfficeLayout, to: OfficeLayout, k: number): Blend {
  const zoneAlpha = new Map<string, number>();
  const podAlpha = new Map<string, number>();
  const before = new Map(from.zones.map(each => [each.key, each]));
  const zones = to.zones.map(each => {
    const old = before.get(each.key);
    if (!old) { zoneAlpha.set(each.key, k); return each; }
    return zone(old, each, k, podAlpha);
  });
  const kept = new Set(to.zones.map(each => each.key));
  for (const old of from.zones) if (!kept.has(old.key)) { zones.push(old); zoneAlpha.set(old.key, 1 - k); }
  const decorAlpha: number[] = [];
  const decor: DecorItem[] = to.decor.map((item, index) => {
    const old = from.decor[index];
    if (!old || old.kind !== item.kind) { decorAlpha.push(k); return item; }
    decorAlpha.push(1);
    return { ...item, x: mix(old.x, item.x, k), y: mix(old.y, item.y, k) };
  });
  from.decor.slice(to.decor.length).forEach(item => { decor.push(item); decorAlpha.push(1 - k); });
  const watchdog = from.watchdog && to.watchdog
    ? { station: rect(from.watchdog.station, to.watchdog.station, k), seat: seat(from.watchdog.seat, to.watchdog.seat, k) }
    : to.watchdog ?? from.watchdog;
  const watchdogAlpha = from.watchdog && to.watchdog ? 1 : to.watchdog ? k : from.watchdog ? 1 - k : 0;
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
