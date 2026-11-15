/** Lead office scene on the shared game stage (features/office-game/engine): the
 * side-view tower a lead's sidebar entry opens onto. The lead's penthouse sits
 * on top; beneath it one floor per teammate: at the headquarters each project's
 * coordinator, then the lead's Worker desks. Art and the per-frame painters live
 * in art.ts; this module owns who is where and why.
 *
 * Every pose is a projected fact (docs/PROJECT_COORDINATION.md, "Lead office"):
 * a working Worker types at a lit screen of code, a blocked one sits back
 * thinking at a paused screen, a failed one slumps at a red screen, a queued
 * one waits at the shaft door holding its brief, and a result awaiting review
 * is held up beside the desk. The lead types at its desk while replying, steps
 * forward with a raised hand while it waits for the owner, and otherwise stands
 * at the task board, whose notes are the board's real tally. An unread report
 * lies on the outbox tray. Transitions replay only real changes seen while the
 * tower is shown: a delegation sends a sealed brief down the shaft and the new
 * Worker walks out of the door with it; a finished result makes its Worker
 * stand, cheer and present it; a new report lands on the tray with a sparkle.
 * First sight and reduced motion settle at once.
 *
 * Every room is one target that opens its conversation (a project floor opens
 * that project); hover or focus shows its status card. The stage owns loop,
 * camera, input, hidden/offscreen sleep and the accessible target layer; this
 * scene asks for frames only while something moves, wakes on the next beat of a
 * looping pose, and otherwise on the next local minute (the sky). Static art is
 * painted once per device scale into offscreen layers; rooms, labels, targets
 * and text layout are prepared per view, so a steady frame allocates nothing
 * beyond canvas calls. No DOM, reads or timers.
 * Entry: createLeadOfficeScene.
 */
import { PALETTE } from '../office-game/engine/paint';
import type { Scene, SceneFrame, StageTarget } from '../office-game/engine/stage';
import {
  FLOOR_CARD, FLOOR_HEIGHT, FLOOR_SPOTS, LEAD_SPOTS, OFFICE_HEIGHT, SHAFT, feetOf, officeTower, roomAt, roomRect,
  type OfficeTower, type RoomRect,
} from './layout';
import {
  INK, SIGNAL, createDaylight, createLayerCache, createLights, drawBubble, drawBurst, drawDoor, drawEnvelope, drawFloorWindow,
  drawLeadWindow, drawLighting, drawPaper, drawPerson, drawReport, drawScreen, drawSparkle, layerSize, lookFor,
  type BubbleKind, type LayerKind, type Look, type Pose, type PoseKind, type ScreenMode,
} from './art';
import type { DeskState, LeadState, OfficeDesk, OfficeLead, OfficeProject, OfficeView } from './projection';

/** Art cadence of looping poses (typing, thinking dots, the hourglass): 8 beats a second. */
const BEAT_MS = 125;
/** Walking pace and stride, room units. */
const WALK_PER_MS = .052, STRIDE = 15;
const SIT_MS = 300, DOOR_MS = 280, CHEER_MS = 1300, SPARKLE_MS = 1400;
/** A brief's trip down the shaft, room units per ms. */
const ENVELOPE_PER_MS = .24;
const MINUTE_MS = 60_000;
const LEAD_LAMPS = [72, 200] as const, FLOOR_LAMPS = [222] as const;
const FLOOR_WINDOW = { x: 184, y: 15, width: 58, height: 30 } as const;
const LEAD_PLATE = { x: 8, y: 8, width: 104, height: 56 } as const;
const ORIGIN = { x: 0, y: 0 } as const;

export type OfficeActivation =
  | { kind: 'conversation'; conversationId: string }
  | { kind: 'project'; path: string; conversationId: string };

export interface LeadOfficeSceneText {
  /** Accessible name of the lead's room. */
  lead(lead: OfficeLead): string;
  /** The lead's role name on its plate. */
  leadName: string;
  /** The lead's short state on its plate. */
  leadState(lead: OfficeLead): string;
  /** The lead's status card lines (report, board). */
  leadDetails(lead: OfficeLead): string[];
  desk(desk: OfficeDesk): string;
  /** The floor's short state word. */
  deskState(desk: OfficeDesk): string;
  /** The desk's state in a sentence, for its status card. */
  deskDetail(desk: OfficeDesk): string;
  project(project: OfficeProject): string;
  /** The project floor's facts, one line each. */
  projectFacts(project: OfficeProject): string[];
  /** What a click does, closing every status card. */
  open: string;
}

type Room = { kind: 'lead' } | { kind: 'desk'; desk: OfficeDesk } | { kind: 'project'; project: OfficeProject };
interface Rest { x: number; seat: number; facing: -1 | 0 | 1; kind: PoseKind; bubble: BubbleKind | ''; screen: ScreenMode; brief: boolean }
type Beat =
  | { kind: 'hidden'; ms: number }
  | { kind: 'door' }
  | { kind: 'walk'; to: number }
  | { kind: 'sit' } | { kind: 'stand' }
  | { kind: 'cheer' };
interface Actor {
  look: Look; pose: Pose; rest: Rest;
  beats: Beat[]; beatAge: number;
  hidden: boolean; door: number; cheer: number;
  /** Offsets this actor's loops so a floor of typists does not type in unison. */
  offset: number;
}
type Box = { left: number; top: number; width: number; height: number };
/** Text laid out for one width; recomputed only when the card's pixel width changes. */
interface TextSlot { width: number; height: number; title: string[]; facts: string[]; details: string[][] }
/** Everything a room shows, prepared once per view. */
interface Entry {
  room: Room; key: string; rect: RoomRect; conversationId: string;
  layer: LayerKind; lit: boolean; actor: Actor | undefined;
  card: { title: string; facts: string[]; tone: string };
  status: { title: string; details: string[]; tone: string };
  hotspot: [number, number, number, number];
  cardText: TextSlot; statusText: TextSlot;
  box: Box; cardBox: Box;
}

const roomKey = (room: Room) => room.kind === 'desk' ? `desk:${room.desk.id}` : room.kind === 'project' ? `project:${room.project.path}` : 'lead';
const DESK_TONE: Record<DeskState, string> = { working: SIGNAL.good, queued: SIGNAL.wait, blocked: SIGNAL.ask, review: SIGNAL.gold, failed: SIGNAL.fail };
const LEAD_TONE: Record<LeadState, string> = { replying: SIGNAL.good, asking: SIGNAL.ask, idle: PALETTE.gold };
const LOOPING: ReadonlySet<PoseKind> = new Set(['type', 'think', 'wait', 'ask', 'cheer']);

function deskRest(state: DeskState): Rest {
  const seated = (kind: PoseKind, bubble: BubbleKind | '', screen: ScreenMode): Rest =>
    ({ x: FLOOR_SPOTS.chair, seat: 1, facing: 1, kind, bubble, screen, brief: true });
  switch (state) {
    case 'working': return seated('type', '', 'code');
    case 'blocked': return seated('think', 'think', 'paused');
    case 'failed': return seated('slump', 'alert', 'error');
    case 'queued': return { x: FLOOR_SPOTS.door, seat: 0, facing: -1, kind: 'wait', bubble: 'wait', screen: 'off', brief: false };
    case 'review': return { x: FLOOR_SPOTS.beside, seat: 0, facing: 0, kind: 'present', bubble: '', screen: 'done', brief: false };
  }
}
function leadRest(state: LeadState): Rest {
  if (state === 'replying') return { x: LEAD_SPOTS.chair, seat: 1, facing: 1, kind: 'type', bubble: 'typing', screen: 'code', brief: false };
  if (state === 'asking') return { x: LEAD_SPOTS.front, seat: 0, facing: 0, kind: 'ask', bubble: 'ask', screen: 'off', brief: false };
  return { x: LEAD_SPOTS.stand, seat: 0, facing: -1, kind: 'ponder', bubble: '', screen: 'off', brief: false };
}
function projectRest(project: OfficeProject): Rest | null {
  if (!project.coordinatorId) return null;
  if (project.questions) return { x: FLOOR_SPOTS.beside, seat: 0, facing: 0, kind: 'ask', bubble: 'ask', screen: 'off', brief: false };
  if (project.workers) return { x: FLOOR_SPOTS.chair, seat: 1, facing: 1, kind: 'type', bubble: '', screen: 'code', brief: true };
  return { x: FLOOR_SPOTS.beside, seat: 0, facing: -1, kind: 'ponder', bubble: '', screen: 'off', brief: false };
}

function ellipsize(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let label = text;
  while (label.length > 1 && ctx.measureText(`${label}…`).width > maxWidth) label = label.slice(0, -1);
  return `${label.trimEnd()}…`;
}
/** At most `lines` lines within maxWidth, breaking at spaces where a line has one; the last is ellipsized. */
function wrap(ctx: CanvasRenderingContext2D, text: string, maxWidth: number, lines: number): string[] {
  const result: string[] = [];
  let rest = text.trim();
  while (rest && result.length < lines) {
    if (ctx.measureText(rest).width <= maxWidth) { result.push(rest); break; }
    if (result.length === lines - 1) { result.push(ellipsize(ctx, rest, maxWidth)); break; }
    let end = 1;
    while (end < rest.length && ctx.measureText(rest.slice(0, end + 1)).width <= maxWidth) end++;
    const space = rest.lastIndexOf(' ', end);
    const cut = space > 0 ? space : end;
    result.push(rest.slice(0, cut).trim()); rest = rest.slice(cut).trim();
  }
  return result;
}
function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
const emptySlot = (): TextSlot => ({ width: -1, height: -1, title: [], facts: [], details: [] });
const emptyBox = (): Box => ({ left: 0, top: 0, width: 0, height: 0 });

export function createLeadOfficeScene(ports: {
  wake(): void;
  activate(target: OfficeActivation): void;
  text: LeadOfficeSceneText;
  fontFamily: string;
}) {
  const layers = createLayerCache();
  const daylight = createDaylight();
  const actors = new Map<string, Actor>();
  let actorList: Actor[] = [];
  let view: OfficeView | undefined, tower: OfficeTower = officeTower(1);
  let entries: Entry[] = [];
  let targets: StageTarget[] = [];
  let initialized = false, still = false, disposed = false;
  let sparkle = -1, openLabel = `${ports.text.open} ›`, boardLabel = '';
  let paintContext: CanvasRenderingContext2D | null = null;
  let lights: ReturnType<typeof createLights> | null = null;
  let leadSky: CanvasGradient | null = null, floorSky: CanvasGradient | null = null;
  const fonts = {
    title: `600 11px ${ports.fontFamily}`, body: `500 10px ${ports.fontFamily}`, small: `600 9px ${ports.fontFamily}`,
  };

  function settle(actor: Actor) {
    const { rest, pose } = actor;
    actor.beats = []; actor.beatAge = 0; actor.hidden = false; actor.door = 0; actor.cheer = -1;
    pose.x = rest.x; pose.seat = rest.seat; pose.facing = rest.facing; pose.kind = rest.kind; pose.carry = false; pose.phase = 0;
  }
  function makeActor(look: Look, rest: Rest, feet: number, identity: string): Actor {
    const pose: Pose = { x: rest.x, feet, facing: rest.facing, seat: rest.seat, kind: rest.kind, phase: 0, tick: 0, carry: false };
    let offset = 0;
    for (let index = 0; index < identity.length; index++) offset = (offset + identity.charCodeAt(index)) % 7;
    return { look, pose, rest, beats: [], beatAge: 0, hidden: false, door: 0, cheer: -1, offset };
  }
  /** Beats from where an actor is to a new rest: stand, cheer, walk, sit. */
  function plan(actor: Actor, next: Rest, options: { arrival?: number; cheer?: boolean } = {}) {
    const beats: Beat[] = [], pose = actor.pose;
    actor.rest = next;
    if (options.arrival !== undefined) {
      Object.assign(pose, { x: SHAFT.door.x + SHAFT.door.width / 2, seat: 0, facing: -1, kind: 'stand', carry: true });
      beats.push({ kind: 'hidden', ms: options.arrival }, { kind: 'door' });
    } else if (pose.seat > .5 && (next.seat < .5 || Math.abs(next.x - pose.x) > .5)) beats.push({ kind: 'stand' });
    if (options.cheer) beats.push({ kind: 'cheer' });
    if (Math.abs(next.x - pose.x) > .5 || options.arrival !== undefined) beats.push({ kind: 'walk', to: next.x });
    if (next.seat > .5 && (pose.seat < .5 || beats.length)) beats.push({ kind: 'sit' });
    actor.beats = beats; actor.beatAge = 0;
    if (!beats.length) settle(actor);
  }
  function stepActor(actor: Actor, dt: number) {
    const beat = actor.beats[0], pose = actor.pose;
    if (!beat) {
      if (actor.door > 0) actor.door = Math.max(0, actor.door - dt / DOOR_MS);
      return;
    }
    actor.beatAge += dt;
    let done = false;
    if (beat.kind === 'hidden') { actor.hidden = true; done = actor.beatAge >= beat.ms; }
    else if (beat.kind === 'door') { actor.hidden = false; actor.door = Math.min(1, actor.beatAge / DOOR_MS); done = actor.door >= 1; }
    else if (beat.kind === 'walk') {
      const distance = beat.to - pose.x, move = Math.min(Math.abs(distance), WALK_PER_MS * dt);
      pose.kind = 'walk'; pose.facing = distance < 0 ? -1 : 1; pose.x += Math.sign(distance) * move; pose.phase = (pose.phase + move / STRIDE) % 1;
      done = Math.abs(beat.to - pose.x) <= .01;
      if (done) pose.x = beat.to;
    } else if (beat.kind === 'sit' || beat.kind === 'stand') {
      const k = Math.min(1, actor.beatAge / SIT_MS);
      pose.kind = 'stand'; pose.facing = beat.kind === 'sit' ? actor.rest.facing || 1 : pose.facing || 1;
      pose.seat = beat.kind === 'sit' ? Math.max(pose.seat, k) : Math.min(pose.seat, 1 - k);
      if (beat.kind === 'sit' && k >= 1) pose.carry = false;
      done = k >= 1;
    } else {
      pose.kind = 'cheer'; pose.facing = 0; pose.seat = 0; actor.cheer = actor.beatAge;
      done = actor.beatAge >= CHEER_MS;
    }
    // The door closes behind someone who has walked clear of it.
    if (actor.door > 0 && beat.kind !== 'door' && pose.x < SHAFT.door.x - 8) actor.door = Math.max(0, actor.door - dt / DOOR_MS);
    if (done) {
      actor.beats.shift(); actor.beatAge = 0;
      if (!actor.beats.length) { const door = actor.door; settle(actor); actor.door = door; }
    }
  }

  /* ------------------------------------------------------------- painting */

  function prepare(ctx: CanvasRenderingContext2D, frame: SceneFrame) {
    if (ctx !== paintContext) { paintContext = ctx; lights = createLights(ctx); leadSky = null; floorSky = null; }
    if (daylight.at(performance.timeOrigin + frame.now) || !leadSky || !floorSky) {
      const { skyTop, skyLow } = daylight.state, lead = LEAD_SPOTS.window;
      leadSky = ctx.createLinearGradient(0, lead.y, 0, lead.y + lead.height);
      leadSky.addColorStop(0, skyTop); leadSky.addColorStop(1, skyLow);
      floorSky = ctx.createLinearGradient(0, FLOOR_WINDOW.y, 0, FLOOR_WINDOW.y + FLOOR_WINDOW.height);
      floorSky.addColorStop(0, skyTop); floorSky.addColorStop(1, skyLow);
    }
    return Math.round((window.devicePixelRatio || 1) / frame.pixel * 100) / 100;
  }
  function layer(ctx: CanvasRenderingContext2D, kind: LayerKind, scale: number, x: number, y: number) {
    const image = layers.get(kind, scale);
    if (!image) return;
    const size = layerSize(kind);
    ctx.drawImage(image, x, y, size[0], size[1]);
  }
  function tickOf(actor: Actor | undefined, frame: SceneFrame) {
    return frame.still ? 0 : Math.floor(frame.now / BEAT_MS) + (actor ? actor.offset : 0);
  }
  /** The people, desk and screen of a room, back to front. */
  function drawStation(ctx: CanvasRenderingContext2D, frame: SceneFrame, scale: number, actor: Actor | undefined, feet: number,
    lead: boolean, screen: ScreenMode) {
    const chairX = lead ? LEAD_SPOTS.chair : FLOOR_SPOTS.chair, deskX = lead ? LEAD_SPOTS.desk : FLOOR_SPOTS.desk;
    const deskKind: LayerKind = lead ? 'lead-desk' : 'desk', deskHeight = layerSize(deskKind)[1];
    layer(ctx, 'chair', scale, chairX - 12, feet - 29);
    const tick = tickOf(actor, frame), shown = !!actor && !actor.hidden;
    if (shown) actor!.pose.tick = actor!.pose.kind === 'cheer' ? Math.floor(actor!.beatAge / BEAT_MS) : tick;
    const seated = shown && actor!.pose.seat > .5;
    if (seated) drawPerson(ctx, actor!.look, actor!.pose);
    layer(ctx, deskKind, scale, deskX, feet - deskHeight);
    const top = feet - deskHeight + (lead ? 26 : 22);
    drawScreen(ctx, deskX + 19.5, top - 17.5, 15, 12, screen, tick);
    if (lead) drawScreen(ctx, deskX + 41.5, top - 17.5, 15, 12, screen, tick + 1);
    if (shown && actor!.rest.brief && !actor!.pose.carry) {
      // The brief lies on the desk while its Worker has it.
      ctx.fillStyle = '#e8c47c'; ctx.fillRect(deskX + 35, top - 1.6, 8, 1.6);
      ctx.fillStyle = '#fffaf0'; ctx.fillRect(deskX + 36, top - 2.2, 6, .8);
    }
    if (shown && !seated) drawPerson(ctx, actor!.look, actor!.pose);
  }
  function bubbleOf(ctx: CanvasRenderingContext2D, actor: Actor | undefined, frame: SceneFrame) {
    if (!actor || actor.hidden || actor.beats.length || !actor.rest.bubble) return;
    const pose = actor.pose, headTop = pose.feet - (pose.seat > .5 ? 35 : 37);
    const x = pose.kind === 'slump' ? pose.x + 10 : pose.x + pose.facing * 2;
    drawBubble(ctx, actor.rest.bubble, x, headTop - 1, tickOf(actor, frame));
  }
  function drawLead(ctx: CanvasRenderingContext2D, frame: SceneFrame, scale: number, actor: Actor | undefined) {
    const feet = feetOf(OFFICE_HEIGHT);
    layer(ctx, 'lead', scale, 0, 0);
    drawLeadWindow(ctx, leadSky!, daylight.state);
    drawDoor(ctx, feet, 0);
    // The task board: one note per task when they fit, ticked when finished, over the exact progress bar.
    const board = LEAD_SPOTS.board, tally = view?.lead.board;
    if (tally && tally.total > 0) {
      const columns = 6, cell = (board.width - 10) / columns, rows = Math.ceil(tally.total / columns);
      if (rows * cell <= board.height - 20) {
        for (let index = 0; index < tally.total; index++) {
          const x = board.x + 5 + (index % columns) * cell, y = board.y + 6 + Math.floor(index / columns) * cell;
          ctx.fillStyle = index < tally.done ? '#bfe3c4' : '#f6dc8a'; ctx.fillRect(x + .8, y + .8, cell - 1.6, cell - 1.6);
          ctx.fillStyle = index < tally.done ? SIGNAL.good : '#c9a06c'; ctx.fillRect(x + cell / 2 - .7, y + .2, 1.4, 1.4);
        }
      }
      ctx.fillStyle = '#5a3a24'; ctx.fillRect(board.x + 5, board.y + board.height - 15, board.width - 10, 4);
      ctx.fillStyle = SIGNAL.good; ctx.fillRect(board.x + 5, board.y + board.height - 15, (board.width - 10) * tally.done / tally.total, 4);
    }
    if (view?.lead.report) drawReport(ctx, LEAD_SPOTS.tray.x + LEAD_SPOTS.tray.width / 2, feet - 26);
    drawStation(ctx, frame, scale, actor, feet, true, actor && !actor.beats.length ? actor.rest.screen : 'off');
    drawLighting(ctx, lights!, daylight.state, OFFICE_HEIGHT, LEAD_LAMPS, LEAD_SPOTS.window.x, LEAD_SPOTS.window.width, true);
    bubbleOf(ctx, actor, frame);
    if (sparkle >= 0) drawSparkle(ctx, LEAD_SPOTS.tray.x + LEAD_SPOTS.tray.width / 2, feet - 30, sparkle, SPARKLE_MS);
  }
  function drawFloor(ctx: CanvasRenderingContext2D, frame: SceneFrame, scale: number, entry: Entry) {
    const feet = feetOf(FLOOR_HEIGHT), actor = entry.actor;
    layer(ctx, entry.layer, scale, 0, 0);
    drawFloorWindow(ctx, floorSky!, daylight.state, FLOOR_WINDOW.x, FLOOR_WINDOW.y, FLOOR_WINDOW.width, FLOOR_WINDOW.height);
    drawDoor(ctx, feet, actor ? actor.door : 0);
    drawStation(ctx, frame, scale, actor, feet, false, actor && !actor.beats.length ? actor.rest.screen : 'off');
    if (entry.room.kind === 'desk' && entry.room.desk.state === 'review' && actor && !actor.beats.length) drawPaper(ctx, FLOOR_SPOTS.desk + 30, feet - 21, false);
    drawLighting(ctx, lights!, daylight.state, FLOOR_HEIGHT, FLOOR_LAMPS, FLOOR_WINDOW.x, FLOOR_WINDOW.width, entry.lit);
    bubbleOf(ctx, actor, frame);
    if (actor && actor.cheer >= 0) drawBurst(ctx, actor.pose.x, feet - 40, actor.cheer, CHEER_MS);
  }

  /* -------------------------------------------------------- screen overlays */

  /** World rect → screen box, written into `box` (origin is the world origin on screen). */
  function project(box: Box, x: number, y: number, width: number, height: number, origin: { x: number; y: number }, unit: number) {
    box.left = origin.x + x * unit; box.top = origin.y + y * unit; box.width = width * unit; box.height = height * unit;
    return box;
  }
  /** A pinned paper card with a state stripe: the room's name, then its facts. */
  function card(ctx: CanvasRenderingContext2D, box: Box, content: Entry['card'], slot: TextSlot) {
    if (box.width < 40 || box.height < 16) return;
    const pad = Math.max(4, Math.min(7, box.height * .12)), stripe = 3, titleLine = 12.5, factLine = 12;
    const textWidth = box.width - stripe - pad * 2;
    if (slot.width !== Math.round(box.width) || slot.height !== Math.round(box.height)) {
      slot.width = Math.round(box.width); slot.height = Math.round(box.height);
      const room = Math.max(1, Math.floor((box.height - pad * 2 - Math.min(content.facts.length, 1) * factLine) / titleLine));
      ctx.font = fonts.title; slot.title = wrap(ctx, content.title, textWidth, Math.min(3, room));
      const shown = Math.max(0, Math.floor((box.height - pad * 2 - slot.title.length * titleLine) / factLine));
      ctx.font = fonts.body; slot.facts = content.facts.slice(0, shown).map((fact, index) => ellipsize(ctx, fact, textWidth - (index ? 0 : 9)));
    }
    ctx.save();
    ctx.fillStyle = 'rgba(40,22,10,0.22)'; roundRect(ctx, box.left + 1.5, box.top + 2, box.width, box.height, 4); ctx.fill();
    ctx.fillStyle = '#fffaf0'; roundRect(ctx, box.left, box.top, box.width, box.height, 4); ctx.fill();
    ctx.fillStyle = content.tone; roundRect(ctx, box.left, box.top, stripe + 3, box.height, 3); ctx.fill();
    ctx.fillStyle = '#fffaf0'; ctx.fillRect(box.left + stripe, box.top, 3, box.height);
    ctx.fillStyle = '#c8402f'; ctx.beginPath(); ctx.arc(box.left + box.width / 2, box.top + 2.5, 1.8, 0, Math.PI * 2); ctx.fill();
    const textLeft = box.left + stripe + pad, used = slot.title.length * titleLine + slot.facts.length * factLine;
    let y = box.top + Math.max(pad, (box.height - used) / 2) + titleLine / 2;
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    ctx.font = fonts.title; ctx.fillStyle = INK;
    for (let index = 0; index < slot.title.length; index++) { ctx.fillText(slot.title[index], textLeft, y); y += titleLine; }
    ctx.font = fonts.body;
    for (let index = 0; index < slot.facts.length; index++) {
      if (index === 0) { ctx.fillStyle = content.tone; ctx.beginPath(); ctx.arc(textLeft + 3, y - .5, 3, 0, Math.PI * 2); ctx.fill(); }
      ctx.fillStyle = 'rgba(43,32,24,0.78)'; ctx.fillText(slot.facts[index], textLeft + (index ? 0 : 9), y);
      y += factLine;
    }
    ctx.restore();
  }
  /** The hovered or focused room's status card, inside its room's top-right corner. */
  function statusCard(ctx: CanvasRenderingContext2D, entry: Entry) {
    const box = entry.box, width = Math.min(240, box.width - 16), pad = 8, line = 13;
    if (width < 80) return;
    const slot = entry.statusText, content = entry.status;
    if (slot.width !== Math.round(width)) {
      slot.width = Math.round(width);
      ctx.font = fonts.title; slot.title = wrap(ctx, content.title, width - pad * 2, 4);
      ctx.font = fonts.body; slot.details = content.details.map((detail, index) => wrap(ctx, detail, width - pad * 2 - (index ? 0 : 10), 2));
      slot.height = pad * 2 + slot.title.length * line + 4 + line + slot.details.reduce((sum, wrapped) => sum + wrapped.length * 12, 0);
    }
    const height = slot.height, left = box.left + box.width - width - 8, top = box.top + 8;
    ctx.save();
    ctx.fillStyle = 'rgba(20,12,6,0.35)'; roundRect(ctx, left + 2, top + 3, width, height, 7); ctx.fill();
    ctx.fillStyle = 'rgba(33,24,17,0.95)'; roundRect(ctx, left, top, width, height, 7); ctx.fill();
    ctx.strokeStyle = content.tone; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    let y = top + pad + line / 2;
    ctx.font = fonts.title; ctx.fillStyle = PALETTE.cream;
    for (let index = 0; index < slot.title.length; index++) { ctx.fillText(slot.title[index], left + pad, y); y += line; }
    y += 2; ctx.font = fonts.body;
    for (let index = 0; index < slot.details.length; index++) {
      const wrapped = slot.details[index];
      for (let row = 0; row < wrapped.length; row++) {
        if (index === 0 && row === 0) { ctx.fillStyle = content.tone; ctx.beginPath(); ctx.arc(left + pad + 3, y, 3, 0, Math.PI * 2); ctx.fill(); }
        ctx.fillStyle = 'rgba(251,241,218,0.86)'; ctx.fillText(wrapped[row], left + pad + (index === 0 ? 10 : 0), y); y += 12;
      }
    }
    y += 2; ctx.font = fonts.small; ctx.fillStyle = PALETTE.gold; ctx.fillText(openLabel, left + pad, y + 1);
    ctx.restore();
  }
  function overlay(ctx: CanvasRenderingContext2D, entry: Entry, origin: { x: number; y: number }, unit: number) {
    const rect = entry.rect;
    project(entry.box, rect.x, rect.y, rect.width, rect.height, origin, unit);
    if (entry.room.kind === 'lead') {
      card(ctx, project(entry.cardBox, rect.x + LEAD_PLATE.x, rect.y + LEAD_PLATE.y, LEAD_PLATE.width, LEAD_PLATE.height, origin, unit), entry.card, entry.cardText);
      const tally = view?.lead.board;
      if (tally) {
        const board = LEAD_SPOTS.board;
        ctx.save(); ctx.font = fonts.small; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillStyle = '#fffaf0';
        ctx.fillText(boardLabel, origin.x + (board.x + board.width / 2) * unit, origin.y + (board.y + board.height - 6) * unit);
        ctx.restore();
      }
    } else {
      card(ctx, project(entry.cardBox, rect.x + FLOOR_CARD.x, rect.y + FLOOR_CARD.y, FLOOR_CARD.width, FLOOR_CARD.height, origin, unit), entry.card, entry.cardText);
    }
    if (view?.current && entry.conversationId === view.current) {
      // The conversation open beside the office: its room is framed, as a list marks its selected row.
      const box = entry.box;
      ctx.save(); ctx.strokeStyle = PALETTE.gold; ctx.lineWidth = 2;
      ctx.strokeRect(Math.round(box.left) + 1, Math.round(box.top) + 1, Math.round(box.width) - 2, Math.round(box.height) - 2);
      ctx.restore();
    }
  }
  function envelopes(ctx: CanvasRenderingContext2D) {
    const from = feetOf(OFFICE_HEIGHT) - 22;
    for (let index = 1; index < entries.length; index++) {
      const actor = entries[index].actor, beat = actor?.beats[0];
      if (!actor || !beat || beat.kind !== 'hidden') continue;
      const to = entries[index].rect.y + feetOf(FLOOR_HEIGHT) - 22;
      const k = Math.min(1, actor.beatAge / beat.ms), eased = k * k * (3 - 2 * k);
      drawEnvelope(ctx, SHAFT.x + SHAFT.width / 2, from + (to - from) * eased);
    }
  }

  const scene: Scene = {
    get width() { return tower.width; },
    get height() { return tower.height; },
    backdrop() { return undefined; },
    step(dt) {
      if (still) return;
      for (let index = 0; index < actorList.length; index++) stepActor(actorList[index], dt);
      if (sparkle >= 0) { sparkle += dt; if (sparkle >= SPARKLE_MS) sparkle = -1; }
    },
    draw(frame) {
      const { ctx, painter, pixel, now } = frame;
      const scale = prepare(ctx, frame);
      frame.world();
      ctx.imageSmoothingEnabled = true;
      for (let index = 0; index < entries.length; index++) {
        const entry = entries[index], rect = entry.rect;
        ctx.save(); ctx.translate(rect.x, rect.y);
        ctx.beginPath(); ctx.rect(0, 0, rect.width, rect.height); ctx.clip();
        if (entry.room.kind === 'lead') drawLead(ctx, frame, scale, entry.actor); else drawFloor(ctx, frame, scale, entry);
        ctx.restore();
        if (frame.hover === entry.key || frame.focus === entry.key) painter.hotspot(ctx, entry.hotspot, pixel, now, frame.focus === entry.key ? 'focus' : 'hover');
      }
      envelopes(ctx);
      frame.screen();
      const origin = frame.project(ORIGIN), unit = 1 / pixel;
      let shown: Entry | undefined;
      for (let index = 0; index < entries.length; index++) {
        overlay(ctx, entries[index], origin, unit);
        if (entries[index].key === frame.hover || (!frame.hover && entries[index].key === frame.focus)) shown = entries[index];
      }
      if (shown) statusCard(ctx, shown);
    },
    nextWake(now, motionless) {
      const minute = MINUTE_MS - ((performance.timeOrigin + now) % MINUTE_MS);
      if (motionless || still) return minute;
      if (sparkle >= 0) return 0;
      let looping = false;
      for (let index = 0; index < actorList.length; index++) {
        const actor = actorList[index];
        if (actor.beats.length || actor.door > 0) return 0;
        if (LOOPING.has(actor.pose.kind)) looping = true;
      }
      return looping ? Math.min(minute, BEAT_MS - (now % BEAT_MS)) : minute;
    },
    hit(point) {
      const index = roomAt(point, tower);
      return index < 0 || !view ? '' : entries[index].key;
    },
    targets() { return targets; },
    activate(id) {
      if (!view) return;
      const entry = entries.find(item => item.key === id);
      if (!entry) return;
      const room = entry.room;
      if (room.kind === 'project') {
        if (room.project.coordinatorId) ports.activate({ kind: 'project', path: room.project.path, conversationId: room.project.coordinatorId });
        return;
      }
      if (entry.conversationId) ports.activate({ kind: 'conversation', conversationId: entry.conversationId });
    },
    setStill(next) {
      still = next;
      if (still) { for (const actor of actorList) settle(actor); sparkle = -1; }
    },
  };

  function entryOf(room: Room, index: number, current: OfficeView): Entry {
    const rect = roomRect(index), key = roomKey(room);
    const base = { room, key, rect, actor: actors.get(key), hotspot: [rect.x + 6, rect.y + 6, rect.width - 12, rect.height - 12] as Entry['hotspot'],
      cardText: emptySlot(), statusText: emptySlot(), box: emptyBox(), cardBox: emptyBox() };
    if (room.kind === 'lead') {
      const lead = current.lead;
      boardLabel = lead.board ? `${lead.board.done}/${lead.board.total}` : '';
      return { ...base, conversationId: lead.conversationId, layer: 'lead', lit: true,
        card: { title: ports.text.leadName, facts: [ports.text.leadState(lead)], tone: LEAD_TONE[lead.state] },
        status: { title: ports.text.leadName, details: [ports.text.leadState(lead), ...ports.text.leadDetails(lead)], tone: LEAD_TONE[lead.state] } };
    }
    if (room.kind === 'desk') {
      const desk = room.desk, tone = DESK_TONE[desk.state];
      return { ...base, conversationId: desk.conversationId, layer: `floor:${desk.model}`, lit: true,
        card: { title: desk.title, facts: [ports.text.deskState(desk)], tone },
        status: { title: desk.title, details: [ports.text.deskDetail(desk)], tone } };
    }
    const project = room.project, tone = project.color || PALETTE.gold, facts = ports.text.projectFacts(project);
    return { ...base, conversationId: project.coordinatorId, layer: 'floor:coordinator', lit: !!project.coordinatorId,
      card: { title: project.name, facts, tone }, status: { title: project.name, details: facts, tone } };
  }

  return {
    scene,
    update(next: OfficeView) {
      if (disposed) return;
      const previous = view;
      view = next; openLabel = `${ports.text.open} ›`;
      const rooms: Room[] = [{ kind: 'lead' },
        ...next.projects.map(project => ({ kind: 'project', project }) as Room),
        ...next.desks.map(desk => ({ kind: 'desk', desk }) as Room)];
      tower = officeTower(rooms.length);
      const animate = initialized && !still;
      const keep = new Set<string>(['lead']);
      let leadActor = actors.get('lead');
      const nextLead = leadRest(next.lead.state);
      if (!leadActor) { leadActor = makeActor(lookFor('coordinator', next.lead.conversationId), nextLead, feetOf(OFFICE_HEIGHT), 'lead'); actors.set('lead', leadActor); }
      else if (leadActor.rest.kind !== nextLead.kind || leadActor.rest.x !== nextLead.x) {
        if (animate) plan(leadActor, nextLead); else { leadActor.rest = nextLead; settle(leadActor); }
      }
      rooms.forEach((room, index) => {
        if (room.kind === 'lead') return;
        const key = roomKey(room);
        const rest = room.kind === 'desk' ? deskRest(room.desk.state) : projectRest(room.project);
        if (!rest) return;
        keep.add(key);
        const actor = actors.get(key);
        if (!actor) {
          const look = room.kind === 'desk' ? lookFor(room.desk.model, room.desk.conversationId) : lookFor('coordinator', room.project.coordinatorId);
          const created = makeActor(look, rest, feetOf(FLOOR_HEIGHT), key);
          actors.set(key, created);
          // A Worker delegated while the office is shown: its brief goes down the shaft and it walks out with it.
          if (animate && room.kind === 'desk') {
            const distance = roomRect(index).y + feetOf(FLOOR_HEIGHT) - feetOf(OFFICE_HEIGHT);
            plan(created, rest, { arrival: Math.max(0, distance) / ENVELOPE_PER_MS });
          }
        } else if (actor.rest.kind !== rest.kind || actor.rest.x !== rest.x || actor.rest.screen !== rest.screen) {
          if (!animate) { actor.rest = rest; settle(actor); return; }
          plan(actor, rest, { cheer: room.kind === 'desk' && room.desk.state === 'review' });
        }
      });
      for (const key of [...actors.keys()]) if (!keep.has(key)) actors.delete(key);
      actorList = [...actors.values()];
      entries = rooms.map((room, index) => entryOf(room, index, next));
      targets = entries.map((entry): StageTarget => {
        const room = entry.room;
        const data: Record<string, string> = room.kind === 'lead' ? { office: 'lead', state: next.lead.state, conversation: entry.conversationId }
          : room.kind === 'desk' ? { office: 'desk', state: room.desk.state, conversation: entry.conversationId }
            : { office: 'project', project: room.project.path, conversation: entry.conversationId };
        const label = room.kind === 'lead' ? ports.text.lead(next.lead) : room.kind === 'desk' ? ports.text.desk(room.desk) : ports.text.project(room.project);
        return { id: entry.key, label, ...entry.rect, data, current: !!next.current && entry.conversationId === next.current };
      });
      if (animate && next.lead.report && next.lead.report !== previous?.lead.report) sparkle = 0;
      // First sight is the first view that carries the office's facts, not the loading frame.
      if (next.coverage !== 'loading' && (next.altitude !== 'headquarters' || next.projectsCoverage !== 'loading')) initialized = true;
    },
    /** How many static layers are painted: bounded by room kinds, never by frames. */
    layerCount: () => layers.size(),
    /** A room's person as projected and as it moves now ('lead', 'desk:<id>', 'project:<path>'). */
    person(key: string) {
      const actor = actors.get(key);
      if (!actor) return null;
      const { rest, pose } = actor;
      return { rest: { kind: rest.kind, x: rest.x, seated: rest.seat > .5, facing: rest.facing, bubble: rest.bubble, screen: rest.screen },
        now: { kind: pose.kind, x: pose.x, seat: pose.seat, hidden: actor.hidden, door: actor.door, carrying: pose.carry },
        beats: actor.beats.map(beat => beat.kind) };
    },
    /** True while a delivery sparkle plays on the outbox tray. */
    delivering: () => sparkle >= 0,
    dispose() {
      if (disposed) return;
      disposed = true; layers.clear(); actors.clear(); actorList = []; entries = []; targets = [];
      paintContext = null; lights = null; leadSky = null; floorSky = null;
    },
  };
}
export type LeadOfficeScene = ReturnType<typeof createLeadOfficeScene>;
