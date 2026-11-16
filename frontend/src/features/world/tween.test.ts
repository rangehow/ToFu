import { describe, expect, it } from 'vitest';
import { layoutOffice, SEATS_PER_POD, type OfficeLayout, type Rect, type TeamShape, type ZoneLayout } from './layout';
import { advanceChange, blendLayout, easeInOut, settledBlend, showChange, type Blend, type FloorChange } from './tween';

const BEFORE: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 8 }, { key: '/knowledge', kind: 'project', seats: 8 },
];
const AFTER: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 17 }, { key: '/knowledge', kind: 'project', seats: 8 },
  { key: '/parallel', kind: 'project', seats: 4 },
];
/** /knowledge leaves, /parallel arrives and the Watchdog's station appears. */
const ELSEWHERE: TeamShape[] = [
  { key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 12 }, { key: '/parallel', kind: 'project', seats: 4 },
  { key: '/watchdog', kind: 'watchdog', seats: 1 },
];
const DURATION = 650;

/** Everything a frame shows, by identity: where it is (and how big) and, last, how opaque, as the scene draws it. */
function picture(blend: Blend): Map<string, number[]> {
  const shown = new Map<string, number[]>();
  for (const zone of blend.layout.zones) {
    const room = blend.zoneAlpha.get(zone.key) ?? 1;
    const pod = (index: number) => room * (blend.podAlpha.get(`${zone.key}:${index}`) ?? 1);
    shown.set(`room ${zone.key}`, [zone.x, zone.y, zone.w, zone.h, room]);
    shown.set(`head desk ${zone.key}`, [zone.lead.x, zone.lead.y, room]);
    zone.pods.forEach((each, index) => shown.set(`pod ${zone.key}:${index}`, [each.x, each.y, pod(index)]));
    zone.seats.forEach((each, index) => shown.set(`desk ${zone.key}:${index}`, [each.x, each.y, pod(Math.floor(index / SEATS_PER_POD))]));
  }
  for (const item of blend.layout.decor) shown.set(`decor ${item.key}`, [item.x, item.y, blend.decorAlpha.get(item.key) ?? 1]);
  const station = blend.layout.watchdog?.station;
  if (station) shown.set('watchdog', [station.x, station.y, blend.watchdogAlpha]);
  for (const [key, value] of shown) if (opacity(value) <= 0) shown.delete(key);
  return shown;
}
const opacity = (value: number[]) => value[value.length - 1];

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

  it('fades arrivals in once their space has opened and departures out before it closes, never popping them', () => {
    const tofu = to.zones.find(zone => zone.key === '/tofu')!;
    const newPod = `/tofu:${tofu.pods.length - 1}`;
    expect(tofu.pods.length).toBeGreaterThan(from.zones.find(zone => zone.key === '/tofu')!.pods.length);
    const opacity = (k: number) => ({ room: blendLayout(from, to, k).zoneAlpha.get('/parallel'), pod: blendLayout(from, to, k).podAlpha.get(newPod),
      gone: blendLayout(to, from, k).zoneAlpha.get('/parallel') });
    expect(opacity(0.25)).toEqual({ room: 0, pod: 0, gone: 0.5 });
    expect(opacity(0.5)).toEqual({ room: 0, pod: 0, gone: 0 });
    expect(opacity(0.75)).toEqual({ room: 0.5, pod: 0.5, gone: 0 });
    let previous = opacity(0);
    for (let step = 1; step <= 40; step += 1) {
      const next = opacity(step / 40);
      expect(Math.abs(next.room! - previous.room!)).toBeLessThanOrEqual(2 / 40 + 1e-9);
      expect(Math.abs(next.pod! - previous.pod!)).toBeLessThanOrEqual(2 / 40 + 1e-9);
      previous = next;
    }
    expect(blendLayout(from, to, 0.25).zoneAlpha.has('/tofu')).toBe(false);
    expect(blendLayout(to, from, 0.25).layout.zones.some(zone => zone.key === '/parallel')).toBe(true);
  });

  it('keeps the plant beside each room beside it all through the blend, and fades the others by their place', () => {
    for (const key of from.zones.map(zone => `beside:${zone.key}`)) {
      const before = from.decor.find(item => item.key === key), after = to.decor.find(item => item.key === key);
      if (!before || !after) continue;
      const room = (layout: typeof from) => layout.zones.find(zone => `beside:${zone.key}` === key)!;
      const gap = { x: after.x - (room(to).x + room(to).w), y: after.y - (room(to).y + room(to).h) };
      for (const k of [0, 0.25, 0.5, 0.75, 1]) {
        const blend = blendLayout(from, to, k);
        const plant = blend.layout.decor.find(item => item.key === key)!;
        expect(plant.x - (room(blend.layout).x + room(blend.layout).w)).toBeCloseTo(gap.x, 6);
        expect(plant.y - (room(blend.layout).y + room(blend.layout).h)).toBeCloseTo(gap.y, 6);
        expect(plant.variant).toBe(after.variant);
      }
    }
    const quarter = blendLayout(from, to, 0.25);
    quarter.layout.decor.forEach(item => {
      const known = from.decor.some(each => each.key === item.key) && to.decor.some(each => each.key === item.key);
      expect(quarter.decorAlpha.get(item.key) ?? 1).toBe(known ? 1 : to.decor.some(each => each.key === item.key) ? 0 : 0.5);
    });
  });

  it('moves each plant to its own new place when a team slots in between rooms or the floor narrows', () => {
    const between: TeamShape[] = [BEFORE[0], { key: '/parallel', kind: 'project', seats: 4 }, ...BEFORE.slice(1)];
    const cases = [
      [from, layoutOffice(1920, between, 1000, 1)],
      [from, layoutOffice(1500, BEFORE, 1000, 1)],
    ] as const;
    for (const [a, b] of cases) {
      for (const item of b.decor) {
        const old = a.decor.find(each => each.key === item.key);
        if (!old) continue;
        // A plant that exists in both plans glides from its old place to its new one, fully visible.
        expect(blendLayout(a, b, 0.5).decorAlpha.get(item.key) ?? 1).toBe(1);
        const midway = blendLayout(a, b, 0.5).layout.decor.find(each => each.key === item.key)!;
        expect(midway.x).toBeCloseTo((old.x + item.x) / 2, 6);
        expect(midway.y).toBeCloseTo((old.y + item.y) / 2, 6);
      }
    }
    expect(cases[1][0].decor.filter(item => item.key.startsWith('wall:')).length)
      .not.toBe(cases[1][1].decor.filter(item => item.key.startsWith('wall:')).length);
  });

  it('carries a pod that arrives inside a room that stays with that room, never over the bare floor beside it', () => {
    const index = to.zones.find(zone => zone.key === '/tofu')!.pods.length - 1;
    const target = to.zones.find(zone => zone.key === '/tofu')!;
    const centre = (box: { x: number; y: number; w: number; h: number }) => ({ x: box.x + box.w / 2, y: box.y + box.h / 2 });
    const offset = { x: target.pods[index].x - centre(target).x, y: target.pods[index].y - centre(target).y };
    for (const k of [0, 0.3, 0.6, 0.9, 1]) {
      const room = blendLayout(from, to, k).layout.zones.find(zone => zone.key === '/tofu')!;
      const pod = room.pods[index];
      expect(pod.x - centre(room).x).toBeCloseTo(offset.x, 6);
      expect(pod.y - centre(room).y).toBeCloseTo(offset.y, 6);
      // Its desks ride with it.
      const desk = room.seats[index * SEATS_PER_POD];
      expect(desk.x - pod.x).toBeCloseTo(target.seats[index * SEATS_PER_POD].x - target.pods[index].x, 6);
      expect(desk.y - pod.y).toBeCloseTo(target.seats[index * SEATS_PER_POD].y - target.pods[index].y, 6);
    }
    // While it is visible, its room has grown far enough to hold most of it (the scene clips the rest).
    for (const k of [0.6, 0.75, 0.9]) {
      const room = blendLayout(from, to, easeInOut(k)).layout.zones.find(zone => zone.key === '/tofu')!;
      const pod = room.pods[index];
      const inside = Math.max(0, Math.min(pod.y + pod.h, room.y + room.h) - Math.max(pod.y, room.y));
      expect(inside / pod.h).toBeGreaterThan(0.5);
    }
  });

  it('starts a change that arrives mid-blend from exactly what is shown, fades included', () => {
    for (const k of [0.3, 0.6, 0.8]) {
      const shown = blendLayout(from, to, easeInOut(k));
      const now = picture(shown);
      for (const next of [from, layoutOffice(1920, ELSEWHERE, 1000, 1), to]) {
        const restarted = picture(blendLayout(shown.layout, next, 0, shown));
        expect([...restarted.keys()].sort()).toEqual([...now.keys()].sort());
        for (const [key, value] of now) restarted.get(key)!.forEach((each, index) => expect(each, key).toBeCloseTo(value[index], 9));
      }
    }
  });

  it('keeps every room moving at the speed it had when the floor changes again mid-blend', () => {
    const STEP = 1;
    const travel = (zone: ZoneLayout) => { const was = from.zones.find(each => each.key === zone.key); return was ? Math.hypot(zone.x - was.x, zone.y - was.y, zone.w - was.w) : 0; };
    const key = [...to.zones].sort((a, b) => travel(b) - travel(a))[0].key;
    const room = (change: FloorChange) => showChange(change, DURATION).layout.zones.find(zone => zone.key === key)!;
    const velocity = (a: Rect, b: Rect) => ({ x: (b.x - a.x) / STEP, y: (b.y - a.y) / STEP, w: (b.w - a.w) / STEP });
    for (const age of [200, 325, 450]) {
      const playing: FloorChange = { from: settledBlend(from), to, age };
      const unchanged = velocity(room(playing), room({ ...playing, age: age + STEP }));
      const speed = Math.hypot(unchanged.x, unchanged.y, unchanged.w);
      expect(speed).toBeGreaterThan(0.1);
      for (const next of [from, layoutOffice(1920, ELSEWHERE, 1000, 1)]) {
        const changed: FloorChange = { from: playing, to: next, age: 0 };
        const after = velocity(room(changed), room(advanceChange(changed, STEP, DURATION)!));
        expect(Math.hypot(after.x - unchanged.x, after.y - unchanged.y, after.w - unchanged.w)).toBeLessThan(0.01 * speed);
      }
    }
  });

  it('never stops, jumps or pops anything through a burst of changes, and settles on the last plan', () => {
    const plan = (shapes: TeamShape[]) => layoutOffice(1920, shapes, 1000, 1);
    // /parallel slots in between the rooms while /knowledge grows a pod, still fading in when /knowledge leaves.
    const between: TeamShape[] = [BEFORE[0], { key: '/parallel', kind: 'project', seats: 4 }, BEFORE[1], { key: '/knowledge', kind: 'project', seats: 12 }];
    const burst = new Map<number, OfficeLayout>([
      [0, to], [170, plan(between)], [260, plan(ELSEWHERE)], [700, to], [820, plan([...AFTER, { key: '/watchdog', kind: 'watchdog', seats: 1 }])],
    ]);
    // Frames a quarter of a millisecond apart: a pop or a jump is a step that does not shrink with the frame time,
    // a stop is a step in speed that shrinks with it, while a blend's own easing changes speed with its square.
    const STEP = 0.25;
    let change: FloorChange | null = null;
    let shown = settledBlend(from), target = from;
    const frames: Array<Map<string, number[]>> = [];
    for (let tick = 0; tick * STEP <= 820 + DURATION + 30; tick += 1) {
      const next = burst.get(tick * STEP);
      if (next) { change = { from: change ?? shown, to: next, age: 0 }; target = next; }
      shown = change ? showChange(change, DURATION) : settledBlend(target);
      frames.push(picture(shown));
      if (change) change = advanceChange(change, STEP, DURATION);
    }
    expect(change).toBeNull();
    expect(picture(shown)).toEqual(picture(settledBlend(target)));
    // Per frame, a blend changes an opacity by at most 2 * 3 * STEP / DURATION (the fade's slope times the ease's
    // steepest), about 0.002, and a speed by at most 12 * distance * STEP^2 / DURATION^2, about 0.003 units for
    // a room crossing the floor; nested changes add a few of these. A visible pop or jump is a far larger step.
    let fade = { by: 0, at: '' }, move = { by: 0, at: '' };
    frames.forEach((frame, index) => {
      if (index === 0) return;
      const previous = frames[index - 1], earlier = frames[index - 2];
      for (const key of new Set([...frame.keys(), ...previous.keys()])) {
        const step = Math.abs((frame.has(key) ? opacity(frame.get(key)!) : 0) - (previous.has(key) ? opacity(previous.get(key)!) : 0));
        if (step > fade.by) fade = { by: step, at: `${key} at ${index * STEP} ms` };
        const [c, b, a] = [frame.get(key), previous.get(key), earlier?.get(key)];
        if (!a || !b || !c) continue;
        for (let axis = 0; axis < c.length - 1; axis += 1) {
          const jerk = Math.abs(c[axis] - 2 * b[axis] + a[axis]);
          if (jerk > move.by) move = { by: jerk, at: `${key} axis ${axis} at ${index * STEP} ms` };
        }
      }
    });
    expect(fade.by, fade.at).toBeLessThan(0.01);
    expect(move.by, move.at).toBeLessThan(0.02);
  });
});
