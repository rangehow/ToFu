import { describe, expect, it, vi } from 'vitest';
import { actorPose, BEAT_MS, createActor, isMoving, setRest, settleActor, startErrand, stepActor, WALK_SPEED, type Actor } from './actors';
import { layoutOffice, type OfficeLayout, type TeamShape } from './layout';
import type { DeskDocument, Person } from './model';
import { LEAD_SLOT, seatAt, type Place } from './routes';

const TEAMS: TeamShape[] = [{ key: '/router', kind: 'router', seats: 0 }, { key: '/tofu', kind: 'project', seats: 16 }];
const layout: OfficeLayout = layoutOffice(1920, TEAMS, 1000, 1);
const STEP = 1000 / 60;

function person(id: string, state: Person['state'] = 'working', emphasis: Person['emphasis'] = null): Person {
  return { id, role: 'worker', team: '/tofu', name: id, detail: '', model: 'engineer', state, emphasis, assignment: null };
}
const desk = (slot: number): Place => ({ kind: 'seat', zone: '/tofu', slot });

/** Steps the actor at 60 Hz until it stops moving, checking it never jumps further than its pace allows. */
function play(actor: Actor, limitMs = 60_000) {
  let last = actorPose(actor, layout, 0, false);
  let elapsed = 0;
  while (isMoving(actor) && elapsed < limitMs) {
    stepActor(actor, STEP, layout);
    elapsed += STEP;
    const pose = actorPose(actor, layout, 0, false);
    if (last && pose && last.kind === 'standing' && pose.kind === 'standing') {
      expect(Math.hypot(pose.x - last.x, pose.y - last.y)).toBeLessThanOrEqual((WALK_SPEED * STEP) / 1000 + 1e-6);
    }
    if (pose) last = pose;
  }
  return elapsed;
}

describe('office actors', () => {
  it('walks a Worker who starts work in through the entrance to their desk, fading in at the door', () => {
    const actor = createActor(person('w1'), desk(3), { kind: 'seat' }, layout, 'walk');
    const start = actorPose(actor, layout, 0, false)!;
    expect([start.kind, start.x, start.y, start.alpha]).toEqual(['standing', layout.entrance.x, layout.entrance.y, 0]);
    expect(play(actor)).toBeGreaterThan(1000);
    const seat = seatAt(layout, desk(3))!;
    expect(actorPose(actor, layout, 0, false)).toMatchObject({ kind: 'seated', x: seat.x, y: seat.cut, alpha: 1 });
    expect(actor.screen).toBe(1);
  });

  it('walks a Worker whose work ended out of the office, and only then lets them go', () => {
    const actor = createActor(person('w2'), desk(1), { kind: 'seat' }, layout, 'instant');
    setRest(actor, { kind: 'gone' }, layout, 'walk');
    expect(actor.gone).toBe(false);
    stepActor(actor, STEP, layout);
    expect(actorPose(actor, layout, 0, false)!.kind).toBe('standing');
    play(actor);
    expect(actor.gone).toBe(true);
  });

  it('turns a Worker round where they stand when their work restarts mid-walk', () => {
    const actor = createActor(person('w3'), desk(6), { kind: 'seat' }, layout, 'instant');
    setRest(actor, { kind: 'gone' }, layout, 'walk');
    for (let index = 0; index < 90; index += 1) stepActor(actor, STEP, layout);
    const here = actorPose(actor, layout, 0, false)!;
    setRest(actor, { kind: 'seat' }, layout, 'walk');
    stepActor(actor, 0, layout);
    const turned = actorPose(actor, layout, 0, false)!;
    expect(Math.hypot(turned.x - here.x, turned.y - here.y)).toBeLessThan(0.01);
    let back = 0;
    while (actor.walk) { stepActor(actor, STEP, layout); back += STEP; }
    // The way back is the way it came: as long as the walk out had been.
    expect(back).toBeLessThanOrEqual(90 * STEP + STEP);
    play(actor);
    expect(actorPose(actor, layout, 0, false)!.kind).toBe('seated');
    expect(actor.gone).toBe(false);
  });

  it('fades people in and out where they rest while the office settles, and moves nobody', () => {
    const actor = createActor(person('w4'), desk(2), { kind: 'seat' }, layout, 'fade');
    expect(actor.walk).toBeNull();
    expect(actorPose(actor, layout, 0, false)).toMatchObject({ kind: 'seated', alpha: 0 });
    play(actor);
    expect(actor.alpha).toBe(1);
    setRest(actor, { kind: 'gone' }, layout, 'fade');
    expect(actor.walk).toBeNull();
    play(actor);
    expect(actor.gone).toBe(true);
  });

  it('carries a report to the owner\'s desk, lays it down once, and walks back to the desk', () => {
    const actor = createActor(person('w5'), desk(4), { kind: 'seat' }, layout, 'instant');
    const onDrop = vi.fn(), onDone = vi.fn();
    const document = { id: 'doc', kind: 'report' } as DeskDocument;
    expect(startErrand(actor, { document, leaves: false, onDrop, onDone }, layout)).toBe(true);
    stepActor(actor, STEP, layout);
    expect(actorPose(actor, layout, 0, false)).toMatchObject({ kind: 'standing', carry: 'report' });
    // A change of rest while the report is in hand waits until it is laid down.
    setRest(actor, { kind: 'spot', index: 0 }, layout, 'walk');
    setRest(actor, { kind: 'seat' }, layout, 'walk');
    play(actor);
    expect(onDrop).toHaveBeenCalledTimes(1);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(actorPose(actor, layout, 0, false)!.kind).toBe('seated');
    expect(startErrand(createActor(person('w6'), desk(5), { kind: 'seat' }, layout, 'walk'), { document, leaves: false, onDrop, onDone }, layout)).toBe(false);
  });

  it('takes a lead with a question to the owner\'s door and back to the head desk', () => {
    const lead: Person = { ...person('lead'), role: 'lead', emphasis: 'decision' };
    const actor = createActor(lead, { kind: 'seat', zone: '/tofu', slot: LEAD_SLOT }, { kind: 'seat' }, layout, 'instant');
    setRest(actor, { kind: 'spot', index: 0 }, layout, 'walk');
    play(actor);
    expect(actorPose(actor, layout, 0, false)).toMatchObject({ kind: 'standing', state: 'wait-up', carry: 'report' });
    setRest(actor, { kind: 'seat' }, layout, 'walk');
    play(actor);
    expect(actorPose(actor, layout, 0, false)!.kind).toBe('seated');
  });

  it('fades only where a person is: a walker fades out walking, a visible person walks rather than jump', () => {
    const walker = createActor(person('w9'), desk(9), { kind: 'seat' }, layout, 'walk');
    for (let index = 0; index < 120; index += 1) stepActor(walker, STEP, layout);
    const here = actorPose(walker, layout, 0, false)!;
    setRest(walker, { kind: 'gone' }, layout, 'fade');
    stepActor(walker, STEP, layout);
    const next = actorPose(walker, layout, 0, false)!;
    expect(Math.hypot(next.x - here.x, next.y - here.y)).toBeLessThanOrEqual((WALK_SPEED * STEP) / 1000 + 1e-6);
    play(walker);
    expect(walker.gone).toBe(true);
    const waiting: Person = { ...person('lead-2'), role: 'lead', emphasis: 'decision' };
    const lead = createActor(waiting, { kind: 'seat', zone: '/tofu', slot: LEAD_SLOT }, { kind: 'spot', index: 1 }, layout, 'instant');
    setRest(lead, { kind: 'seat' }, layout, 'fade');
    expect(lead.walk).not.toBeNull();
    play(lead);
    expect(actorPose(lead, layout, 0, false)!.kind).toBe('seated');
  });

  it('settles everything at once under reduced motion', () => {
    const walking = createActor(person('w7'), desk(7), { kind: 'seat' }, layout, 'walk');
    settleActor(walking);
    expect(isMoving(walking)).toBe(false);
    expect(actorPose(walking, layout, 0, true)!.kind).toBe('seated');
    const leaving = createActor(person('w8'), desk(8), { kind: 'seat' }, layout, 'instant');
    setRest(leaving, { kind: 'gone' }, layout, 'walk');
    settleActor(leaving);
    expect(leaving.gone).toBe(true);
  });

  it('lets each typist keep their own tempo, so no beat moves a whole room at once', () => {
    const typists = Array.from({ length: 32 }, (_, index) => createActor(person(`typist-${index}`), desk(index % 16), { kind: 'seat' }, layout, 'instant'));
    let changes = 0;
    for (let beat = 0; beat < 96; beat += 1) {
      const flipped = typists.filter(actor => actorPose(actor, layout, beat, false)!.column !== actorPose(actor, layout, beat + 1, false)!.column).length;
      expect(flipped).toBeLessThanOrEqual(typists.length / 2);
      changes += flipped;
    }
    expect(changes).toBeGreaterThan(typists.length * 96 / 5);
    expect(BEAT_MS).toBeGreaterThan(0);
    const still = typists.map(actor => actorPose(actor, layout, 3, true)!.column);
    expect(new Set(still).size).toBe(1);
  });
});
