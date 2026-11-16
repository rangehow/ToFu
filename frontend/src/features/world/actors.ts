/** People in the office as actors. Each has a desk (`home`), a place it should rest (its desk, a spot at the
 * owner's door, or gone) and the walk that takes it there, so nobody pops in or out: a person who starts work
 * walks in through the entrance to their desk, one whose work ended stands and walks out, a change of mind
 * mid-walk turns them round where they stand (routes.ts), and a report is carried to the owner's desk, laid down
 * and followed by the walk back (or out, when that person's work is done). While the office's first reads
 * settle, and under reduced motion, people fade in and out where they rest instead, or simply are there.
 * The pose says what to draw now: a seated cell cropped at the desk line, or a standing/walking cell at the
 * feet, with its opacity and the document in hand; looping cells run on each person's own beat offset and
 * rhythm, so a room of typists never moves in unison.
 * Pure (no DOM). Entry points: createActor, setRest, setHome, startErrand, stepActor, settleActor, actorPose, isMoving. */
import { DOCS, STATES, type PersonState } from './assets/manifest.generated';
import { waitingSpots, type OfficeLayout, type Point } from './layout';
import { stableHash, type DeskDocument, type Person } from './model';
import { pointAlong, rerouteFrom, resolve, routeBetween, routeLength, samePlace, seatAt, type Place, type Waypoint } from './routes';

export type Rest = { kind: 'seat' } | { kind: 'spot'; index: number } | { kind: 'gone' };
/** walk: animate the way there; fade: appear or vanish where they rest; instant: be there now (reduced motion). */
export type Transition = 'walk' | 'fade' | 'instant';
type Direction = 'down' | 'left' | 'right' | 'up';

export interface Errand {
  document: DeskDocument;
  /** After laying the document down: out of the office instead of back to the desk. */
  leaves: boolean;
  phase: 'carry' | 'drop' | 'return';
  dropLeft: number;
  onDrop(): void;
  onDone(): void;
}
interface Walk { route: Waypoint[]; to: Place; travelled: number; points: Point[] }

export interface Actor {
  readonly id: string;
  person: Person;
  home: Place;
  rest: Rest;
  /** Where the actor stands or sits when it is not walking. */
  at: Place | null;
  walk: Walk | null;
  errand: Errand | null;
  alpha: number;
  fadeTo: number;
  /** The desk screen's light, 0..1: on while its person types there. */
  screen: number;
  /** Time spent walking, which drives the walk cycle. */
  stepMs: number;
  readonly phase: number;
  /** Beats per typing cell (3 or 4): each typist keeps their own tempo. */
  readonly rhythm: number;
  /** Left the floor: the scene drops the actor. */
  gone: boolean;
}

export interface Pose {
  kind: 'seated' | 'standing';
  /** Seated: the chair's x and the cut line; standing: the feet. */
  x: number; y: number;
  state: PersonState; column: number;
  alpha: number;
  carry: DeskDocument['kind'] | null;
  /** Top-left of the carried document inside the cell, and whether it is drawn over the body. */
  hand: Point | null; front: boolean;
}

/** Walking pace in scene units per second. */
export const WALK_SPEED = 150;
/** The art's beat for looping cells (typing, shifting weight at the door): the office game's cadence. */
export const BEAT_MS = 125;
const FADE_MS = 320;
const SCREEN_MS = 240;
const DROP_MS = 650;
/** Walkers fade in and out over this distance through the entrance door. */
const DOOR_FADE = 48;
const WAIT_BEATS = 6;

const OUTSIDE: Place = { kind: 'outside' };
const DROP: Place = { kind: 'drop' };

function placeFor(actor: Actor, rest: Rest): Place {
  return rest.kind === 'seat' ? actor.home : rest.kind === 'spot' ? { kind: 'spot', index: rest.index } : OUTSIDE;
}
const sameRest = (a: Rest, b: Rest) => a.kind === b.kind && (a.kind !== 'spot' || (b.kind === 'spot' && a.index === b.index));

export function createActor(person: Person, home: Place, rest: Rest, layout: OfficeLayout, transition: Transition): Actor {
  const hash = stableHash(person.id);
  const actor: Actor = {
    id: person.id, person, home, rest, at: null, walk: null, errand: null, alpha: 1, fadeTo: 1, screen: 0, stepMs: 0,
    phase: hash % 8, rhythm: 3 + ((hash >>> 3) % 2), gone: false,
  };
  const place = placeFor(actor, rest);
  const route = transition === 'walk' ? routeBetween(OUTSIDE, place, layout) : null;
  if (route) actor.walk = { route, to: place, travelled: 0, points: [] };
  else {
    actor.at = place;
    if (transition !== 'instant') actor.alpha = 0;
  }
  actor.screen = actor.at && person.state === 'working' && rest.kind === 'seat' ? 1 : 0;
  return actor;
}

function snap(actor: Actor, place: Place) {
  actor.walk = null;
  actor.at = place;
  actor.stepMs = 0;
  if (place.kind === 'outside') actor.gone = true;
}

/** Sends the actor on its way to where it should rest now. A fade happens where the actor is: one fading out
 * keeps walking while it fades, and one visible somewhere else walks to the place instead of jumping there. */
function travel(actor: Actor, layout: OfficeLayout, transition: Transition) {
  const place = placeFor(actor, actor.rest);
  if (transition === 'instant') {
    snap(actor, place);
    actor.alpha = actor.fadeTo = actor.rest.kind === 'gone' ? 0 : 1;
    return;
  }
  if (transition === 'fade') {
    if (actor.rest.kind === 'gone') { actor.fadeTo = 0; return; }
    if (actor.alpha === 0 || (!actor.walk && actor.at !== null && samePlace(actor.at, place))) {
      snap(actor, place);
      actor.fadeTo = 1;
      return;
    }
  }
  actor.fadeTo = 1;
  if (!actor.walk && actor.at && samePlace(actor.at, place)) return;
  const route = actor.walk ? rerouteFrom(actor.walk.route, actor.walk.travelled, actor.walk.to, place, layout)
    : actor.at ? routeBetween(actor.at, place, layout) : null;
  if (route) { actor.walk = { route, to: place, travelled: 0, points: [] }; actor.at = null; return; }
  // No way there on this floor (its room is gone): leave by fading where they are, or reappear at the place.
  if (actor.rest.kind === 'gone') { actor.fadeTo = 0; return; }
  snap(actor, place);
  actor.alpha = 0;
}

export function setRest(actor: Actor, rest: Rest, layout: OfficeLayout, transition: Transition) {
  if (sameRest(actor.rest, rest)) return;
  actor.rest = rest;
  // A document in hand is laid down first; the new rest applies on the way back.
  if (actor.errand && actor.errand.phase !== 'return') return;
  travel(actor, layout, transition);
}

/** A person moved to another desk (their team changed): they walk there if they were at or on the way to it. */
export function setHome(actor: Actor, home: Place, layout: OfficeLayout, transition: Transition) {
  if (samePlace(actor.home, home)) return;
  actor.home = home;
  if (actor.rest.kind === 'seat' && !(actor.errand && actor.errand.phase !== 'return')) travel(actor, layout, transition);
}

/** Ends every transition now (reduced motion): the actor is where it should rest, fully shown or gone, and a
 * document in hand is laid down. */
export function settleActor(actor: Actor) {
  const errand = actor.errand;
  actor.errand = null;
  errand?.onDrop();
  errand?.onDone();
  snap(actor, placeFor(actor, actor.rest));
  actor.alpha = actor.fadeTo = actor.rest.kind === 'gone' ? 0 : 1;
  actor.screen = actor.person.state === 'working' && actor.rest.kind === 'seat' ? 1 : 0;
}

/** The actor carries a document from its desk to the owner's desk. False when it is not sitting at its desk
 * (walking, waiting at the door, fading), so the caller lays the document down without a walk. */
export function startErrand(actor: Actor, errand: Omit<Errand, 'phase' | 'dropLeft'>, layout: OfficeLayout): boolean {
  if (actor.errand || actor.walk || !actor.at || !samePlace(actor.at, actor.home) || actor.alpha < 1 || actor.fadeTo < 1) return false;
  const route = routeBetween(actor.home, DROP, layout);
  if (!route) return false;
  actor.errand = { ...errand, phase: 'carry', dropLeft: DROP_MS };
  actor.walk = { route, to: DROP, travelled: 0, points: [] };
  actor.at = null;
  return true;
}

export function stepActor(actor: Actor, dt: number, layout: OfficeLayout) {
  if (actor.alpha !== actor.fadeTo) {
    const step = dt / FADE_MS;
    actor.alpha = actor.alpha < actor.fadeTo ? Math.min(actor.fadeTo, actor.alpha + step) : Math.max(actor.fadeTo, actor.alpha - step);
    if (actor.alpha === 0 && actor.fadeTo === 0 && actor.rest.kind === 'gone') actor.gone = true;
  }
  const errand = actor.errand;
  if (errand?.phase === 'drop') {
    errand.dropLeft -= dt;
    if (errand.dropLeft <= 0) {
      errand.phase = 'return';
      errand.onDrop();
      if (errand.leaves) actor.rest = { kind: 'gone' };
      travel(actor, layout, 'walk');
      if (!actor.walk) finishErrand(actor);
    }
  }
  const walk = actor.walk;
  if (walk) {
    walk.points = resolve(walk.route, layout, walk.points);
    const length = routeLength(walk.points);
    walk.travelled = Math.min(length, walk.travelled + (WALK_SPEED * dt) / 1000);
    actor.stepMs += dt;
    if (walk.travelled >= length) {
      snap(actor, walk.to);
      if (actor.errand?.phase === 'carry' && walk.to.kind === 'drop') actor.errand.phase = 'drop';
      else if (actor.errand?.phase === 'return') finishErrand(actor);
    }
  }
  const lit = !actor.walk && actor.at !== null && samePlace(actor.at, actor.home) && actor.person.state === 'working' ? 1 : 0;
  if (actor.screen !== lit) actor.screen = lit ? Math.min(1, actor.screen + dt / SCREEN_MS) : Math.max(0, actor.screen - dt / SCREEN_MS);
}

function finishErrand(actor: Actor) {
  const done = actor.errand?.onDone;
  actor.errand = null;
  done?.();
}

/** True while the actor changes anything on screen by itself (walking, fading, lighting up, laying a document down). */
export function isMoving(actor: Actor): boolean {
  const lit = !actor.walk && actor.at !== null && samePlace(actor.at, actor.home) && actor.person.state === 'working' ? 1 : 0;
  return Boolean(actor.walk) || actor.alpha !== actor.fadeTo || actor.errand?.phase === 'drop' || actor.screen !== lit;
}

const heading = (dx: number, dy: number): Direction => (Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : dy < 0 ? 'up' : 'down');

/** Where the carried document's box goes inside the cell: the art's hand anchor minus the document's anchor. */
function handOf(state: PersonState, index: number): Point | null {
  const hands = STATES[state].hands;
  if (!hands) return null;
  const hand = hands[Math.min(index, hands.length - 1)];
  const anchor = DOCS.report.carry.anchor;
  return { x: hand[0] - anchor[0], y: hand[1] - anchor[1] };
}

/** What to draw for the actor at `beat` (BEAT_MS ticks); `still` shows every state's named still cell. */
export function actorPose(actor: Actor, layout: OfficeLayout, beat: number, still: boolean): Pose | null {
  const carry = actor.errand && actor.errand.phase !== 'return' ? actor.errand.document.kind : null;
  if (actor.walk) {
    const points = actor.walk.points.length ? actor.walk.points : resolve(actor.walk.route, layout);
    const { point, dx, dy } = pointAlong(points, actor.walk.travelled);
    const state = `${carry ? 'carry' : 'walk'}-${heading(dx, dy)}` as PersonState;
    const frames = STATES[state];
    const index = still ? frames.columns.indexOf(frames.reduced) : Math.floor((actor.stepMs / 1000) * (frames.fps ?? 9)) % frames.columns.length;
    const door = Math.hypot(point.x - layout.entrance.x, point.y - layout.entrance.y);
    return {
      kind: 'standing', x: point.x, y: point.y, state, column: frames.columns[index], alpha: actor.alpha * Math.min(1, door / DOOR_FADE),
      carry, hand: carry ? handOf(state, index) : null, front: frames.docLayer !== 'back',
    };
  }
  const at = actor.at;
  if (!at || at.kind === 'outside') return null;
  if (at.kind === 'spot' || at.kind === 'drop') {
    const spot = at.kind === 'spot' ? waitingSpots(layout)[at.index] : layout.owner.drop;
    if (!spot) return null;
    const state: PersonState = at.kind === 'spot' ? 'wait-up' : 'carry-up';
    const frames = STATES[state];
    const index = still || at.kind === 'drop' ? frames.columns.indexOf(frames.reduced) : Math.floor((beat + actor.phase) / WAIT_BEATS) % frames.columns.length;
    const held = at.kind === 'spot' ? 'report' : carry;
    return { kind: 'standing', x: spot.x, y: spot.y, state, column: frames.columns[index], alpha: actor.alpha, carry: held,
      hand: held ? handOf(state, index) : null, front: frames.docLayer !== 'back' };
  }
  const seat = seatAt(layout, at);
  if (!seat) return null;
  const typing = actor.person.state === 'working';
  const state = `${typing ? 'work' : 'idle'}-${seat.facing}` as PersonState;
  const frames = STATES[state];
  const index = !typing || still ? frames.columns.indexOf(frames.reduced) : Math.floor((beat + actor.phase) / actor.rhythm) % frames.columns.length;
  return { kind: 'seated', x: seat.x, y: seat.cut, state, column: frames.columns[Math.max(0, index)], alpha: actor.alpha, carry: null, hand: null, front: true };
}
