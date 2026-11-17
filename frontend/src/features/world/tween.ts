/** Floor-plan transitions: a relayout never jumps. The scene blends the layout it shows into the next one by
 * identity (zones by team, pods and seats by index within their zone, decor by its place's key), so rooms slide
 * and resize, every seated person rides their desk, and walkers' routes (routes.ts) bend with the aisles.
 * Something that exists on one side only fades without competing with the motion: a leaving room, pod or decor
 * item fades out over the first half (before neighbours move into its space), an arriving one fades in over the
 * second half (once its space has opened). A pod or desk that comes or goes inside a room that stays rides with
 * that room, keeping its offset from the room's centre, so it never floats over the bare floor beside it (the
 * scene also clips it to the room while it fades).
 * A floor that changes again mid-blend (the first reads land one after another) starts a FloorChange whose source
 * is the blend in flight, which keeps playing underneath, as a game engine crossfades from a live animation rather
 * than a frozen pose: every room keeps its speed, and whatever is fading keeps its opacity and finishes arriving
 * with the second half or leaving with the first, so a burst of changes never stops, jumps or pops anything.
 * Pure. Entry points: FloorChange, showChange, advanceChange, settledBlend, blendLayout, easeInOut, easeOutBack,
 * arriving, leaving. */
import type { DecorItem, OfficeLayout, Point, Rect, Seat, ZoneLayout } from './layout';

/** Opacity of what is fading: zones by team key, pods by `zone:index`, decor by its place's key; absent is 1. */
export interface Fades {
  zoneAlpha: ReadonlyMap<string, number>;
  podAlpha: ReadonlyMap<string, number>;
  decorAlpha: ReadonlyMap<string, number>;
  watchdogAlpha: number;
}
export interface Blend extends Fades { layout: OfficeLayout }

const NOTHING: ReadonlyMap<string, number> = new Map();
/** A floor at rest: nothing fading. */
export const settledBlend = (layout: OfficeLayout): Blend => ({ layout, zoneAlpha: NOTHING, podAlpha: NOTHING, decorAlpha: NOTHING, watchdogAlpha: 1 });

export const easeInOut = (k: number) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
/** Lands with a slight overshoot (a document dropping onto the desk). */
export const easeOutBack = (k: number) => 1 + 2.2 * Math.pow(k - 1, 3) + 1.2 * Math.pow(k - 1, 2);

/** Opacity of something arriving or leaving at blend progress `k`. */
export const arriving = (k: number) => Math.min(1, Math.max(0, 2 * k - 1));
export const leaving = (k: number) => Math.min(1, Math.max(0, 1 - 2 * k));
/** On both sides but still fading in what is shown (opacity `carried`): it finishes arriving. */
const staying = (carried: number | undefined, k: number) => (carried === undefined || carried >= 1 ? undefined : carried + (1 - carried) * arriving(k));
const going = (carried: number | undefined, k: number) => (carried ?? 1) * leaving(k);

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

function setFade(alphas: Map<string, number>, key: string, alpha: number | undefined) {
  if (alpha !== undefined) alphas.set(key, alpha);
}

function zone(a: ZoneLayout, b: ZoneLayout, k: number, carried: Fades, podAlpha: Map<string, number>): ZoneLayout {
  const shown = rect(a, b, k);
  const here = centre(shown), was = centre(a), will = centre(b);
  const pods = b.pods.map((pod, index) => {
    const before = a.pods[index];
    const key = `${b.key}:${index}`;
    if (!before) { podAlpha.set(key, arriving(k)); return { ...pod, x: pod.x + here.x - will.x, y: pod.y + here.y - will.y }; }
    setFade(podAlpha, key, staying(carried.podAlpha.get(key), k));
    return { ...pod, x: mix(before.x, pod.x, k), y: mix(before.y, pod.y, k) };
  });
  a.pods.slice(b.pods.length).forEach((pod, offset) => {
    const key = `${b.key}:${b.pods.length + offset}`;
    pods.push({ ...pod, x: pod.x + here.x - was.x, y: pod.y + here.y - was.y });
    podAlpha.set(key, going(carried.podAlpha.get(key), k));
  });
  const seats = b.seats.map((each, index) => (a.seats[index] ? seat(a.seats[index], each, k) : shiftSeat(each, here.x - will.x, here.y - will.y)));
  seats.push(...a.seats.slice(b.seats.length).map(each => shiftSeat(each, here.x - was.x, here.y - was.y)));
  return {
    key: b.key, row: b.row, ...shown,
    banner: point(a.banner, b.banner, k), routine: point(a.routine, b.routine, k), lead: seat(a.lead, b.lead, k), pods, seats,
  };
}

/** The floor at progress `k` from `from` (shown with `carried` fades) to `to`. At k = 0 it looks exactly like
 * `from`, and it already holds every new desk (invisible, riding its room), so a newcomer can walk to it. */
export function blendLayout(from: OfficeLayout, to: OfficeLayout, k: number, carried: Fades = settledBlend(from)): Blend {
  const zoneAlpha = new Map<string, number>();
  const podAlpha = new Map<string, number>();
  const before = new Map(from.zones.map(each => [each.key, each]));
  const zones = to.zones.map(each => {
    const old = before.get(each.key);
    if (!old) { zoneAlpha.set(each.key, arriving(k)); return each; }
    setFade(zoneAlpha, each.key, staying(carried.zoneAlpha.get(each.key), k));
    return zone(old, each, k, carried, podAlpha);
  });
  const kept = new Set(to.zones.map(each => each.key));
  for (const old of from.zones) {
    if (kept.has(old.key)) continue;
    zones.push(old);
    zoneAlpha.set(old.key, going(carried.zoneAlpha.get(old.key), k));
    // A pod still fading in a room that now leaves keeps its opacity while the room fades.
    old.pods.forEach((_, index) => setFade(podAlpha, `${old.key}:${index}`, carried.podAlpha.get(`${old.key}:${index}`)));
  }
  const decorAlpha = new Map<string, number>();
  const earlier = new Map(from.decor.map(item => [item.key, item]));
  const decor: DecorItem[] = to.decor.map(item => {
    const old = earlier.get(item.key);
    if (!old) { decorAlpha.set(item.key, arriving(k)); return item; }
    setFade(decorAlpha, item.key, staying(carried.decorAlpha.get(item.key), k));
    return { ...item, x: mix(old.x, item.x, k), y: mix(old.y, item.y, k) };
  });
  const later = new Set(to.decor.map(item => item.key));
  for (const item of from.decor) if (!later.has(item.key)) { decor.push(item); decorAlpha.set(item.key, going(carried.decorAlpha.get(item.key), k)); }
  const watchdog = from.watchdog && to.watchdog
    ? { station: rect(from.watchdog.station, to.watchdog.station, k), seat: seat(from.watchdog.seat, to.watchdog.seat, k) }
    : to.watchdog ?? from.watchdog;
  const watchdogAlpha = from.watchdog && to.watchdog ? staying(carried.watchdogAlpha, k) ?? 1
    : to.watchdog ? arriving(k) : from.watchdog ? going(carried.watchdogAlpha, k) : 0;
  const corridors = to.corridors.map((value, index) => mix(from.corridors[index] ?? from.corridors[from.corridors.length - 1] ?? value, value, k));
  return {
    layout: {
      width: mix(from.width, to.width, k), height: mix(from.height, to.height, k), wall: to.wall, owner: to.owner, zones, watchdog,
      entrance: point(from.entrance, to.entrance, k), lounge: rect(from.lounge, to.lounge, k), bar: rect(from.bar, to.bar, k),
      // A commons is only the place its furniture (decor, above) was laid out for.
      spine: to.spine, corridors, decor, commons: to.commons,
    },
    zoneAlpha, podAlpha, decorAlpha, watchdogAlpha,
  };
}

/** A floor-plan change in flight, `age` ms into blending from its source toward `to`. The source is the floor at
 * rest it began from, or the change that was still in flight then, which keeps playing underneath. */
export interface FloorChange { from: FloorChange | Blend; to: OfficeLayout; age: number }

const inFlight = (source: FloorChange | Blend): source is FloorChange => 'age' in source;

/** What a change shows now; every change blends for `duration` ms. */
export function showChange(change: FloorChange, duration: number): Blend {
  const source = inFlight(change.from) ? showChange(change.from, duration) : change.from;
  return blendLayout(source.layout, change.to, easeInOut(Math.min(1, change.age / duration)), source);
}

/** A change and every change still playing beneath it, `dt` ms later; a change beneath that has finished becomes
 * the still frame it ended on. Null once this change has finished too: the floor then shows exactly
 * settledBlend(change.to) (whatever had left is invisible by then, everything else is in place and opaque). */
export function advanceChange(change: FloorChange, dt: number, duration: number): FloorChange | null {
  const next = advance(change, dt, duration);
  return inFlight(next) ? next : null;
}

function advance(change: FloorChange, dt: number, duration: number): FloorChange | Blend {
  const from = inFlight(change.from) ? advance(change.from, dt, duration) : change.from;
  const next: FloorChange = { from, to: change.to, age: Math.min(duration, change.age + dt) };
  return next.age >= duration ? showChange(next, duration) : next;
}
