/** Tofu office scene on the shared game stage (features/office-game/engine): the open-plan floor, the people on
 * it and the documents on the owner's desk, drawn as one canvas frame, so the floor and everyone on it always
 * agree. The stage owns the loop, camera, gestures, hidden-tab sleep and the accessible button layer; this scene
 * owns who is where and how they get there.
 *
 * Calm by construction (the owner's 11-16 audit of the DOM office measured whole-floor reflows every few
 * seconds, people popping in and out on every attempt flip and typists moving in unison):
 * - Rooms never shrink while the office is open: a team keeps the most desks it has needed (`capacity`), so a
 *   Worker whose attempt ends leaves an empty desk instead of reflowing the floor. A floor plan that does change
 *   (a team grows a pod, a team appears, the window resizes) blends into the next one (tween.ts) instead of
 *   jumping, and walkers' routes bend with it (routes.ts).
 * - People never pop: they walk in through the entrance to their desk, stand and walk out, turn round when
 *   their work restarts mid-walk, walk to the owner's door with a question and back (actors.ts). While the
 *   office's first reads settle (and under reduced motion) they fade in and out where they rest instead.
 * - Looping cells run on each person's own beat offset and tempo; a lit screen fades on and off; a document
 *   that arrives drops onto the desk with a small overshoot.
 * - The floor art fades in as it decodes (procedural floor first, then furniture, then each sheet's people).
 * The loop asks for frames only while something moves, on the art's beat while only loops play, and otherwise
 * on the next hour (the window skies). Reduced motion settles every transition at once.
 * Entry point: createWorldScene. */
import { PALETTE as UI } from '../office-game/engine/paint';
import { clampCamera, resetCamera, screenToWorld, type Camera } from '../office-game/engine/camera';
import type { Scene, SceneFrame, StageTarget } from '../office-game/engine/stage';
import { CELL, PROPS, STATES, type WorldTheme } from './assets/manifest.generated';
import { actorPose, BEAT_MS, createActor, isMoving, setHome, setRest, settleActor, startErrand, stepActor, type Actor, type Pose, type Rest, type Transition } from './actors';
import { ART_SCALE, spriteArt, type SpriteName, type WorldArt } from './art';
import {
  allocateSlots, BOARD_GRID, fitOffice, SEATED_SHOWN, SEATS_PER_POD, shelfSlots, TROPHY_H, TROPHY_W,
  waitingSpots, type OfficeLayout, type Rect, type Seat, type TeamShape,
} from './layout';
import { stableHash } from './hash';
import { firstLine, type DeskDocument, type Office, type Person, type Team, type Trophy } from './model';
import {
  CLUTTER_CHOICES, drawFloor, drawFurniture, drawInside, drawLighting, drawRug, drawWall, FLOOR_TILE, NOISE_SIZE, PALETTE, paintFloorTile,
  paintNoiseTile, paintPod, paintWindow, POD_BLEED, rr, seeded, windowSlots, type Atlas,
} from './paint';
import { LEAD_SLOT, samePlace, seatAt, type Place } from './routes';
import { FEET, sheetFor } from './sprites';
import { advanceChange, easeOutBack, settledBlend, showChange, type Blend, type FloorChange } from './tween';

export type OpenTarget = { kind: 'person'; id: string } | { kind: 'document'; id: string };
export interface WalkPlan { person: string; document: DeskDocument; leaves: boolean }

export interface WorldSceneText {
  ownerPlate: string;
  watchdogPlate: string;
  /** A person's accessible name and their state in words. */
  person(person: Person, waiting: boolean): { label: string; state: string };
  document(kind: DeskDocument['kind']): string;
  pile(count: number): string;
  board(done: number, total: number): string;
  trophy(title: string): string;
  trophyDetail: string;
  trophyMore(count: number): string;
  routine(count: number, more: boolean): string;
}
export interface WorldScenePorts {
  open(target: OpenTarget): void;
  /** Ask the stage for a frame (new art decoded, a walk planned outside the loop). */
  wake(): void;
  camera: Camera;
  fontFamily: string;
  text: WorldSceneText;
  art: WorldArt;
}

/** A floor-plan transition, the art fading in, a tooltip appearing and a document landing (presentation). */
const TWEEN_MS = 650;
const ART_FADE_MS = 400;
const TIP_MS = 140;
const DROP_MS = 450;
const HOUR_MS = 3_600_000;

interface HitBox { id: string; kind: 'person' | 'document' | 'extra'; x: number; y: number; w: number; h: number; label: string; detail: string; data: Record<string, string> }
/** `room`: the opacity of the room or pod the person sits in while it arrives or leaves in a floor-plan blend,
 * and the room's box to clip to while their pod fades (null: no clip). */
interface Drawn { actor: Actor; pose: Pose; sheet: CanvasImageSource | null; fade: number; room: { alpha: number; clip: Rect | null } }
const SETTLED_ROOM = { alpha: 1, clip: null };
/** A full-view layer in device pixels: `key` names what it holds, `pending` the key the previous frame wanted;
 * the counts say how often frames blitted it, rebuilt it or drew the content directly. */
interface CachedLayer { canvas: HTMLCanvasElement | null; key: string; pending: string; hits: number; builds: number; direct: number }

/** Where a seat's lit screen goes: over a desk-up monitor's face, or as a glow round the back of a desk-down,
 * head-desk or Watchdog monitor (the prop's "screen" slot, relative to its anchor). */
function screenBox(seat: Seat, station: Rect | null): { name: SpriteName; x: number; y: number; w: number; h: number } {
  if (seat.facing === 'up') {
    const [sx, sy] = PROPS['desk-up'].slots!.screen;
    const art = spriteArt('screen-front');
    return { name: 'screen-front', x: seat.desk.x + sx - art.anchor[0], y: seat.desk.y + sy - art.anchor[1], w: art.size[0], h: art.size[1] };
  }
  const [sx, sy, sw, sh] = station ? PROPS['watchdog-station'].slots!.screen
    : seat.desk.w > 60 ? PROPS['lead-desk'].slots!.screen : PROPS['desk-down'].slots!.screen;
  const origin = station ? { x: station.x, y: station.y } : seat.desk;
  const art = spriteArt('screen-glow');
  return { name: 'screen-glow', x: origin.x + sx + sw / 2 - art.anchor[0], y: origin.y + sy + sh / 2 - art.anchor[1], w: art.size[0], h: art.size[1] };
}

/** The pod layer resolution for a device scale: the next power of two up to the art's own resolution. */
function layerLevel(devicePerUnit: number): number {
  return Math.min(ART_SCALE, Math.pow(2, Math.ceil(Math.log2(Math.max(devicePerUnit, 1 / 64)))));
}

export function createWorldScene(ports: WorldScenePorts) {
  const { camera, art, text } = ports;
  const font = (size: number, weight = 600) => `${weight} ${size}px ${ports.fontFamily}`;
  let office: Office | null = null;
  let settling = true;
  let still = false;
  let theme: WorldTheme = 'dark';
  let viewport = { width: 0, height: 0 };
  let target: OfficeLayout | null = null;
  /** The floor-plan change in flight; null while the floor is at rest. */
  let moving: FloorChange | null = null;
  let shown: Blend | null = null;
  let shapeKey = '';
  const capacity = new Map<string, number>();
  const slots = new Map<string, Map<string, number>>();
  /** Spots in the line at the owner's door, kept like desks: a person waiting keeps their spot. */
  let waiting = new Map<string, number>();
  const actors = new Map<string, Actor>();
  /** Sheets drawn per actor, so a theme change keeps the old sheet until the new one is decoded. */
  const drawnSheet = new Map<string, string>();
  const sheetFade = new Map<string, number>();
  const pending = new Set<string>();
  const seenDocuments = new Set<string>();
  const landed = new Map<string, number>();
  let selected = '';
  let followId = '';
  let lastFocus = '';
  let lastHover = '';
  /** The first placement is first sight: everyone already there appears where they rest. */
  let placed = false;
  let reveal = 0;
  let furniture = 0;
  let tip = { id: '', alpha: 0 };
  let hits: HitBox[] = [];
  let targetList: StageTarget[] = [];
  let targetsDirty = true;
  let disposed = false;
  // Caches keyed by everything they depend on.
  const podLayers = new Map<string, HTMLCanvasElement>();
  const windowLayers = new Map<string, HTMLCanvasElement>();
  const sheetLayers = new Map<string, HTMLCanvasElement>();
  let patterns: { key: string; planks: CanvasPattern | null; pile: CanvasPattern | null } | null = null;
  const floorLayer: CachedLayer = { canvas: null, key: '', pending: '', hits: 0, builds: 0, direct: 0 };
  const lightLayer: CachedLayer = { canvas: null, key: '', pending: '', hits: 0, builds: 0, direct: 0 };

  const layoutNow = () => shown?.layout ?? null;

  /* ------------------------------------------------------------------ floor plan */

  function shapes(): TeamShape[] {
    return (office?.teams || []).map(team => ({ key: team.key, kind: team.kind, seats: capacity.get(team.key) ?? 0 }));
  }

  function applyWorld(first: boolean) {
    const layout = layoutNow();
    if (!layout) return;
    camera.worldWidth = layout.width;
    camera.worldHeight = layout.height;
    if (first) resetCamera(camera); else clampCamera(camera);
  }

  /** A new floor plan when the shapes or the window changed; data changes blend, window changes snap. */
  function relayout(snap: boolean) {
    if (!office || !viewport.width || !viewport.height) return;
    const teamShapes = shapes();
    const key = `${viewport.width}x${viewport.height}|${teamShapes.map(shape => `${shape.key}:${shape.kind}:${Math.ceil(shape.seats / SEATS_PER_POD)}`).join(',')}`;
    if (key === shapeKey) return;
    shapeKey = key;
    const next = fitOffice(viewport.width, viewport.height, teamShapes).layout;
    const first = !shown;
    if (first || snap || still) {
      target = next; moving = null; shown = settledBlend(next);
    } else {
      moving = { from: moving ?? shown!, to: next, age: 0 }; target = next;
    }
    applyWorld(first);
    targetsDirty = true;
  }

  /* ------------------------------------------------------------------ people */

  function homeOf(team: Team, person: Person): Place | null {
    if (person.role === 'watchdog') return { kind: 'watchdog' };
    if (person === team.lead) return { kind: 'seat', zone: team.key, slot: LEAD_SLOT };
    const slot = slots.get(team.key)?.get(person.id);
    return slot === undefined ? null : { kind: 'seat', zone: team.key, slot };
  }

  /** `quiet`: what changes is knowledge, not the office (first reads still settling, or the update that ends
   * them), so people fade in and out where they rest instead of walking. */
  function placePeople(previousTeams: ReadonlySet<string>, quiet: boolean) {
    if (!office || !target) return;
    const layout = target;
    const firstSight = !placed;
    placed = true;
    const spots = waitingSpots(layout);
    const askers = [...office.attention.decisions].sort((a, b) => a.since - b.since)
      .map(item => item.source.conversationId).filter((id): id is string => Boolean(id) && office!.people.has(id!));
    // The order of questions asked together varies between reads; a spot never changes hands while its person
    // waits (no two people walking round each other to swap places), and a newcomer takes the free spot nearest
    // the door.
    waiting = allocateSlots(waiting, [...new Set(askers)]);
    const present = new Set<string>();
    for (const team of office.teams) {
      const freshTeam = firstSight || !previousTeams.has(team.key);
      for (const person of [team.lead, ...team.workers]) {
        const home = homeOf(team, person);
        if (!home || !seatAt(layout, home)) continue;
        present.add(person.id);
        const index = person.emphasis === 'decision' ? waiting.get(person.id) ?? -1 : -1;
        const rest: Rest = index >= 0 && index < spots.length ? { kind: 'spot', index } : { kind: 'seat' };
        // Workers walk in and out; a lead, the Router and the Watchdog arrive with their team.
        const transition: Transition = still ? 'instant' : quiet || freshTeam || person.role !== 'worker' ? 'fade' : 'walk';
        const actor = actors.get(person.id);
        if (!actor) { actors.set(person.id, createActor(person, home, rest, shown?.layout ?? layout, transition)); continue; }
        actor.person = person;
        setHome(actor, home, shown?.layout ?? layout, still ? 'instant' : 'walk');
        setRest(actor, rest, shown?.layout ?? layout, still ? 'instant' : quiet && actor.rest.kind === 'gone' ? 'fade' : 'walk');
      }
    }
    for (const actor of actors.values()) {
      if (present.has(actor.id)) continue;
      setRest(actor, { kind: 'gone' }, shown?.layout ?? layout, still ? 'instant' : quiet ? 'fade' : 'walk');
    }
    for (const [id, actor] of actors) if (actor.gone) { actors.delete(id); drawnSheet.delete(id); }
  }

  /** Desk slots and the grow-only capacity of every team: a person keeps their desk while they are on the floor,
   * leaving ones included, and a team keeps the most desks it has needed. */
  function allocate() {
    if (!office) return;
    for (const team of office.teams) {
      const ids = team.workers.map(worker => worker.id);
      for (const actor of actors.values()) {
        if (actor.person.team === team.key && actor.person.role === 'worker' && !actor.gone && !ids.includes(actor.id)) ids.push(actor.id);
      }
      const next = allocateSlots(slots.get(team.key) || new Map(), ids);
      slots.set(team.key, next);
      const needed = next.size ? Math.max(...next.values()) + 1 : 0;
      capacity.set(team.key, Math.max(capacity.get(team.key) ?? 0, needed));
    }
  }

  /* ------------------------------------------------------------------ documents */

  function syncDocuments(animate: boolean) {
    for (const document of office?.documents || []) {
      if (seenDocuments.has(document.id)) continue;
      seenDocuments.add(document.id);
      if (animate && !pending.has(document.id)) landed.set(document.id, 0);
    }
  }
  function deskDocuments() {
    const onDesk = (office?.documents || []).filter(document => !pending.has(document.id));
    const results = onDesk.filter(document => document.ref.source === 'worker-result');
    const fan = onDesk.filter(document => document.ref.source !== 'worker-result');
    return { results, fan: [...fan.filter(document => document.kind === 'report'), ...fan.filter(document => document.kind === 'deliverable')] };
  }

  /* ------------------------------------------------------------------ targets and hit testing */

  function boxOf(pose: Pose): Rect {
    return pose.kind === 'seated'
      ? { x: pose.x - CELL.w / 2, y: pose.y - CELL.h * SEATED_SHOWN, w: CELL.w, h: CELL.h * SEATED_SHOWN }
      : { x: pose.x - FEET.x, y: pose.y - FEET.y, w: CELL.w, h: CELL.h };
  }

  function rebuildTargets() {
    targetsDirty = false;
    const layout = layoutNow();
    const next: HitBox[] = [];
    if (layout && office) {
      const beat = 0;
      for (const actor of actors.values()) {
        if (actor.rest.kind === 'gone' || actor.alpha <= 0.05) continue;
        const pose = actorPose(actor, layout, beat, true);
        if (!pose) continue;
        const words = text.person(actor.person, actor.at?.kind === 'spot');
        next.push({ id: actor.id, kind: 'person', ...boxOf(pose), label: words.label,
          detail: `${actor.person.detail ? `${actor.person.detail} · ` : ''}${words.state}`,
          data: { person: actor.id, role: actor.person.role, ...(actor.person.emphasis ? { emphasis: actor.person.emphasis } : {}) } });
      }
      const { results, fan } = deskDocuments();
      const tray = layout.owner.tray;
      const top = results[results.length - 1];
      if (top) {
        const art = spriteArt('doc-pile');
        next.push({ id: 'pile', kind: 'extra', x: tray.x + 4, y: tray.y + 6, w: art.size[0], h: art.size[1],
          label: `${text.pile(results.length)} · ${top.title}`, detail: top.title, data: { extra: 'pile', document: top.id } });
      }
      fan.forEach((document, index) => {
        next.push({ id: document.id, kind: 'document', x: tray.x + 24 + index * 15, y: tray.y + 4 + (index % 2) * 3, w: 19, h: 23,
          label: `${text.document(document.kind)} · ${document.title}`, detail: document.title, data: { document: document.id, kind: document.kind } });
      });
      const board = office.whiteboard;
      if (board) {
        const rect = layout.owner.whiteboard;
        next.push({ id: 'board', kind: 'extra', ...rect, label: text.board(board.board.done, board.board.total),
          detail: firstLine(board.board.summary), data: { extra: 'board' } });
      }
      const shelf = shelfSlots(layout.owner.shelf);
      const trophies = office.trophies.length > shelf.length ? office.trophies.slice(0, shelf.length - 1) : office.trophies;
      // The shelf's last trophy also tells how many more the "+N" stands for.
      const more = office.trophies.length - trophies.length;
      trophies.forEach((trophy, index) => {
        const detail = more && index === trophies.length - 1 ? `${text.trophyDetail} · ${text.trophyMore(more)}` : text.trophyDetail;
        next.push({ id: trophy.id, kind: 'extra', x: shelf[index].x, y: shelf[index].y, w: TROPHY_W, h: TROPHY_H,
          label: `${text.trophy(trophy.title)} · ${detail}`, detail, data: { extra: trophy.id } });
      });
    }
    hits = next;
    targetList = hits.map(hit => ({ id: hit.id, label: hit.label, x: hit.x, y: hit.y, width: hit.w, height: hit.h,
      current: hit.id === selected, data: hit.data }));
  }

  function hitAt(point: { x: number; y: number }): HitBox | null {
    let best: HitBox | null = null;
    for (const hit of hits) {
      if (point.x < hit.x || point.x > hit.x + hit.w || point.y < hit.y || point.y > hit.y + hit.h) continue;
      // Documents and the owner's things lie on top of people; among people the one nearer the viewer wins.
      if (!best || (best.kind === 'person' && hit.kind !== 'person') || (best.kind === hit.kind && hit.y + hit.h > best.y + best.h)) best = hit;
    }
    return best;
  }

  /* ------------------------------------------------------------------ painting */

  /** The plank and carpet-pile patterns for a look and resolution (patterns are not bound to a context). */
  function prepare(ctx: CanvasRenderingContext2D, level: number, look: WorldTheme) {
    const key = `${look}|${level}`;
    if (patterns?.key === key) return patterns;
    const palette = PALETTE[look];
    const tile = document.createElement('canvas');
    tile.width = Math.round(FLOOR_TILE.w * level); tile.height = Math.round(FLOOR_TILE.h * level);
    const tileCtx = tile.getContext('2d');
    let planks: CanvasPattern | null = null;
    if (tileCtx) {
      tileCtx.scale(level, level);
      paintFloorTile(tileCtx, palette);
      planks = ctx.createPattern(tile, 'repeat');
      planks?.setTransform(new DOMMatrix([1 / level, 0, 0, 1 / level, 0, 0]));
    }
    const noise = document.createElement('canvas');
    noise.width = noise.height = NOISE_SIZE;
    const noiseCtx = noise.getContext('2d');
    if (noiseCtx) paintNoiseTile(noiseCtx, palette);
    const pile = noiseCtx ? ctx.createPattern(noise, 'repeat') : null;
    patterns = { key, planks, pile };
    return patterns;
  }

  /** Draws a full-view layer from its cache when `key` (camera transform and content) is unchanged; rebuilds the
   * cache once a key has held for a frame, so a moving camera or a floor that is still fading or blending draws
   * directly instead of rebuilding every frame. An empty key always draws directly. */
  function drawCached(ctx: CanvasRenderingContext2D, slot: CachedLayer, key: string, paint: (target: CanvasRenderingContext2D) => void) {
    if (key && slot.key !== key && slot.pending === key) {
      const canvas = slot.canvas ?? document.createElement('canvas');
      slot.canvas = canvas;
      if (canvas.width !== ctx.canvas.width || canvas.height !== ctx.canvas.height) {
        canvas.width = ctx.canvas.width;
        canvas.height = ctx.canvas.height;
      }
      const target = canvas.getContext('2d');
      if (target) {
        target.setTransform(1, 0, 0, 1, 0, 0);
        target.clearRect(0, 0, canvas.width, canvas.height);
        target.setTransform(ctx.getTransform());
        target.imageSmoothingEnabled = true;
        target.imageSmoothingQuality = 'high';
        paint(target);
        slot.key = key;
        slot.builds += 1;
      }
    }
    slot.pending = key;
    if (!key || slot.key !== key || !slot.canvas) { slot.direct += 1; paint(ctx); return; }
    slot.hits += 1;
    const transform = ctx.getTransform();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.drawImage(slot.canvas, 0, 0);
    ctx.setTransform(transform);
  }

  function podLayer(atlas: Atlas, atlasTheme: string, zoneKey: string, index: number, level: number, layout: OfficeLayout): HTMLCanvasElement | null {
    const zone = layout.zones.find(each => each.key === zoneKey);
    const pod = zone?.pods[index];
    if (!zone || !pod) return null;
    const key = `${atlasTheme}|${level}|${zoneKey}|${index}`;
    let layer = podLayers.get(key);
    if (layer) return layer;
    layer = document.createElement('canvas');
    layer.width = Math.round((pod.w + POD_BLEED * 2) * level);
    layer.height = Math.round((pod.h + POD_BLEED * 2) * level);
    const ctx = layer.getContext('2d');
    if (!ctx) return null;
    ctx.scale(level, level);
    ctx.imageSmoothingQuality = 'high';
    const perPod = SEATS_PER_POD;
    const seats = zone.seats.slice(index * perPod, (index + 1) * perPod);
    const seed = stableHash(zoneKey);
    paintPod(ctx, atlas, pod, seats, seat => Math.floor(seeded(seed + index * perPod + seat)() * CLUTTER_CHOICES));
    podLayers.set(key, layer);
    return layer;
  }

  function windowLayer(atlas: Atlas, atlasTheme: string, index: number, hour: number, level: number): HTMLCanvasElement | null {
    const key = `${atlasTheme}|${level}|${index}|${hour}`;
    let layer = windowLayers.get(key);
    if (layer) return layer;
    const prop = atlas.props.window;
    if (!prop) return null;
    layer = document.createElement('canvas');
    layer.width = Math.round(prop.size[0] * level); layer.height = Math.round(prop.size[1] * level);
    const ctx = layer.getContext('2d');
    if (!ctx) return null;
    ctx.scale(level, level);
    paintWindow(ctx, atlas, hour, index);
    windowLayers.set(key, layer);
    return layer;
  }

  /** A sprite at (x, y, w, h), only its part inside `room` when given. */
  function sprite(ctx: CanvasRenderingContext2D, name: SpriteName, x: number, y: number, w: number, h: number, room: Rect | null = null) {
    const source = art.sprite(name);
    if (source) drawInside(ctx, source, 0, 0, source.width, source.height, x, y, w, h, room);
  }

  function drawPerson(ctx: CanvasRenderingContext2D, item: Drawn) {
    const { pose, sheet } = item;
    if (!sheet) return;
    const scale = (sheet as { width: number }).width / (CELL.w * CELL.columns);
    const sx = pose.column * CELL.w * scale, sy = STATES[pose.state].row * CELL.h * scale;
    const box = boxOf(pose);
    const alpha = ctx.globalAlpha;
    const room = item.room.clip;
    ctx.globalAlpha = alpha * pose.alpha * item.fade * item.room.alpha;
    const carry = pose.carry && pose.hand ? `carry-${pose.carry}` as SpriteName : null;
    const carryArt = carry ? spriteArt(carry) : null;
    if (carry && carryArt && !pose.front) sprite(ctx, carry, box.x + pose.hand!.x, box.y + pose.hand!.y, carryArt.size[0], carryArt.size[1], room);
    drawInside(ctx, sheet, sx, sy, CELL.w * scale, box.h * scale, box.x, box.y, box.w, box.h, room);
    if (carry && carryArt && pose.front) sprite(ctx, carry, box.x + pose.hand!.x, box.y + pose.hand!.y, carryArt.size[0], carryArt.size[1], room);
    ctx.globalAlpha = alpha;
  }

  /** The actor's sheet for the shown theme at the frame's resolution, or the one it was drawn with until that
   * decodes; each sheet's people fade in together the first time it can be drawn. */
  function sheetOf(actor: Actor, level: number): { sheet: CanvasImageSource | null; fade: number } {
    const url = sheetFor(actor.person, art.theme() ?? theme);
    if (art.sheet(url)) drawnSheet.set(actor.id, url);
    const used = drawnSheet.get(actor.id);
    const sheet = used ? art.sheet(used) : null;
    if (!used || !sheet) return { sheet: null, fade: 0 };
    if (!sheetFade.has(used)) sheetFade.set(used, still ? 1 : 0);
    return { sheet: sheetLayer(used, sheet, level), fade: sheetFade.get(used)! };
  }

  /** A sheet at the pod layers' resolution (`level`), painted once per sheet and level like a mip level, so each
   * person is drawn from it at about one to one: resampling the full-resolution sheet for every person on every
   * frame costs more than the rest of the frame together (a software-rasterized canvas stalls for whole frames). */
  function sheetLayer(url: string, sheet: CanvasImageSource & { width: number; height: number }, level: number): CanvasImageSource {
    const own = sheet.width / (CELL.w * CELL.columns);
    if (level >= own) return sheet;
    const key = `${url}|${level}`;
    let layer = sheetLayers.get(key);
    if (layer) return layer;
    layer = document.createElement('canvas');
    layer.width = Math.max(1, Math.round(CELL.w * CELL.columns * level));
    layer.height = Math.max(1, Math.round(sheet.height * level / own));
    const ctx = layer.getContext('2d');
    if (!ctx) return sheet;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(sheet, 0, 0, layer.width, layer.height);
    sheetLayers.set(key, layer);
    return layer;
  }

  function plate(ctx: CanvasRenderingContext2D, label: string, x: number, y: number) {
    ctx.font = font(11, 700);
    const width = ctx.measureText(label).width + 16;
    rr(ctx, x, y, width, 20, 6); ctx.fillStyle = UI.panel; ctx.fill();
    ctx.fillStyle = UI.cream; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    ctx.fillText(label, x + 8, y + 10.5);
  }

  /** A team's sign: a walnut nameplate edged in the team colour with its name, board tally and idle count. */
  function banner(ctx: CanvasRenderingContext2D, team: Team, x: number, y: number) {
    const name = team.kind === 'router' ? team.lead.name : team.name;
    const board = team.board && team.board.total ? team.board : null;
    const routine = team.routine ? text.routine(team.routine, team.history) : '';
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    ctx.font = font(13, 700);
    const nameWidth = ctx.measureText(name).width;
    ctx.font = font(10, 600);
    const tally = board ? `${board.done}/${board.total}` : '';
    const tallyWidth = board ? 38 + ctx.measureText(tally).width : 0;
    const routineWidth = routine ? ctx.measureText(routine).width : 0;
    const width = 14 + nameWidth + (board ? 10 + tallyWidth : 0) + (routine ? 10 + routineWidth : 0) + 12;
    const alpha = ctx.globalAlpha;
    if (team.failed) ctx.globalAlpha = alpha * 0.72;
    rr(ctx, x + 1, y + 2, width, 24, 6); ctx.fillStyle = 'rgba(20, 12, 6, 0.28)'; ctx.fill();
    const fill = ctx.createLinearGradient(0, y, 0, y + 24);
    fill.addColorStop(0, '#3a2a1e'); fill.addColorStop(1, '#22170f');
    rr(ctx, x, y, width, 24, 6); ctx.fillStyle = fill; ctx.fill();
    ctx.strokeStyle = team.color; ctx.globalAlpha *= 0.6; ctx.lineWidth = 1;
    if (team.failed) ctx.setLineDash([3, 3]);
    rr(ctx, x + 0.5, y + 0.5, width - 1, 23, 6); ctx.stroke(); ctx.setLineDash([]);
    ctx.globalAlpha = team.failed ? alpha * 0.72 : alpha;
    ctx.save(); rr(ctx, x, y, width, 24, 6); ctx.clip(); ctx.fillStyle = team.color; ctx.fillRect(x, y, 4, 24); ctx.restore();
    let left = x + 14;
    ctx.font = font(13, 700); ctx.fillStyle = UI.cream; ctx.fillText(name, left, y + 12.5);
    left += nameWidth + 10;
    if (board) {
      rr(ctx, left, y + 10.5, 34, 4, 2); ctx.fillStyle = 'rgba(251, 241, 218, 0.16)'; ctx.fill();
      if (board.done) { rr(ctx, left, y + 10.5, 34 * board.done / board.total, 4, 2); ctx.fillStyle = UI.good; ctx.fill(); }
      ctx.font = font(10, 600); ctx.fillStyle = 'rgba(251, 241, 218, 0.72)'; ctx.fillText(tally, left + 38, y + 12.5);
      left += tallyWidth + 10;
    }
    if (routine) { ctx.font = font(10, 600); ctx.fillStyle = 'rgba(251, 241, 218, 0.62)'; ctx.fillText(routine, left, y + 12.5); }
    ctx.globalAlpha = alpha;
  }

  function drawDocuments(ctx: CanvasRenderingContext2D, layout: OfficeLayout) {
    const { results, fan } = deskDocuments();
    const tray = layout.owner.tray;
    const lay = (id: string, name: SpriteName, x: number, y: number, w: number, h: number, angle: number) => {
      const age = landed.get(id);
      const k = age === undefined ? 1 : Math.min(1, age / DROP_MS);
      const eased = easeOutBack(k);
      ctx.save();
      ctx.globalAlpha *= Math.min(1, k * 2);
      ctx.translate(x + w / 2, y + h / 2 - 14 * (1 - eased));
      ctx.rotate(((angle - 12 * (1 - eased)) * Math.PI) / 180);
      sprite(ctx, name, -w / 2, -h / 2, w, h);
      ctx.restore();
    };
    const top = results[results.length - 1];
    if (top) {
      const art = spriteArt('doc-pile');
      lay(top.id, 'doc-pile', tray.x + 4, tray.y + 6, art.size[0], art.size[1], -2);
      if (results.length > 1) {
        const label = String(results.length);
        ctx.font = font(9, 700);
        const width = Math.max(11, ctx.measureText(label).width + 6);
        const x = tray.x + 4 + art.size[0] - width / 2 + 3, y = tray.y + 2;
        rr(ctx, x, y, width, 11, 5.5); ctx.fillStyle = '#22170f'; ctx.fill();
        ctx.fillStyle = UI.cream; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(label, x + width / 2, y + 6);
      }
    }
    fan.forEach((document, index) => lay(document.id, document.kind === 'deliverable' ? 'doc-deliverable' : 'doc-report',
      tray.x + 24 + index * 15, tray.y + 4 + (index % 2) * 3, 19, 23, document.kind === 'deliverable' ? 3 : -4));
  }

  function drawBoard(ctx: CanvasRenderingContext2D, layout: OfficeLayout) {
    const value = office?.whiteboard;
    if (!value) return;
    const rect = layout.owner.whiteboard;
    const tasks = value.board.tasks;
    const capacityNotes = BOARD_GRID.columns * BOARD_GRID.rows;
    const notes = tasks.length > capacityNotes ? tasks.slice(0, capacityNotes - 1) : tasks;
    const padX = 6.75, padY = 7.5, gap = 3, rowHeight = 12;
    const cell = (rect.w - padX * 2 - gap * (BOARD_GRID.columns - 1)) / BOARD_GRID.columns;
    const slotAt = (index: number) => ({ x: rect.x + padX + (index % BOARD_GRID.columns) * (cell + gap), y: rect.y + padY + Math.floor(index / BOARD_GRID.columns) * (rowHeight + gap) });
    notes.forEach((task, index) => {
      const name = (['in_progress', 'completed', 'blocked'].includes(task.status) ? `note-${task.status}` : 'note-pending') as SpriteName;
      const at = slotAt(index), size = Math.min(cell, rowHeight);
      sprite(ctx, name, at.x + (cell - size) / 2, at.y, size, size);
    });
    if (tasks.length > notes.length) {
      const at = slotAt(notes.length);
      ctx.font = font(8, 700); ctx.fillStyle = '#3a2a1c'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(`+${tasks.length - notes.length}`, at.x + cell / 2, at.y + rowHeight / 2);
    }
  }

  function drawTrophies(ctx: CanvasRenderingContext2D, layout: OfficeLayout, trophies: ReadonlyArray<Trophy>) {
    const shelf = shelfSlots(layout.owner.shelf);
    const onShelf = trophies.length > shelf.length ? trophies.slice(0, shelf.length - 1) : trophies;
    const cup = spriteArt('trophy');
    const scale = Math.min(TROPHY_W / cup.size[0], TROPHY_H / cup.size[1]);
    onShelf.forEach((_, index) => {
      const slot = shelf[index], w = cup.size[0] * scale, h = cup.size[1] * scale;
      sprite(ctx, 'trophy', slot.x + (TROPHY_W - w) / 2, slot.y + TROPHY_H - h, w, h);
    });
    const more = trophies.length - onShelf.length;
    if (more > 0) {
      const slot = shelf[shelf.length - 1];
      ctx.font = font(8, 700); ctx.fillStyle = UI.cream; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(`+${more}`, slot.x + TROPHY_W / 2, slot.y + TROPHY_H / 2);
    }
  }

  function tooltip(ctx: CanvasRenderingContext2D, frame: SceneFrame, hit: HitBox, alpha: number) {
    const anchor = frame.project({ x: hit.x + hit.w / 2, y: hit.y });
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.font = font(12, 700);
    const name = hit.kind === 'person' ? hit.label.split(' · ')[0] : hit.label.split(' · ')[0];
    let detail = hit.detail;
    const maxWidth = 300;
    ctx.font = font(11, 500);
    while (detail.length > 1 && ctx.measureText(detail).width > maxWidth) detail = `${detail.slice(0, -2)}…`;
    const detailWidth = detail ? ctx.measureText(detail).width : 0;
    ctx.font = font(12, 700);
    const width = Math.min(maxWidth, Math.max(ctx.measureText(name).width, detailWidth)) + 16;
    const height = detail ? 38 : 22;
    const { insets, viewWidth } = frame.camera;
    const left = Math.min(Math.max(anchor.x - width / 2, insets.left + 4), viewWidth - insets.right - width - 4);
    const top = Math.max(insets.top + 4, anchor.y - height - 8);
    rr(ctx, left + 1, top + 2, width, height, 7); ctx.fillStyle = 'rgba(10, 6, 3, 0.35)'; ctx.fill();
    rr(ctx, left, top, width, height, 7); ctx.fillStyle = 'rgba(24, 16, 10, 0.94)'; ctx.fill();
    ctx.strokeStyle = 'rgba(251, 241, 218, 0.16)'; ctx.lineWidth = 1; ctx.stroke();
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillStyle = UI.cream; ctx.fillText(name, left + 8, top + 11.5, maxWidth);
    if (detail) { ctx.font = font(11, 500); ctx.fillStyle = 'rgba(251, 241, 218, 0.7)'; ctx.fillText(detail, left + 8, top + 27.5); }
    ctx.restore();
  }

  /** Everything under the people that only a new floor plan, look, hour or zoom level changes. */
  function paintFloor(ctx: CanvasRenderingContext2D, layout: OfficeLayout, blend: Blend, atlas: Atlas | null, look: WorldTheme,
    level: number, hour: number, teams: ReadonlyMap<string, Team>) {
    const palette = PALETTE[look];
    const zoneAlpha = (key: string) => blend.zoneAlpha.get(key) ?? 1;
    const { planks, pile } = prepare(ctx, level, look);
    const glass = PROPS.window.slots?.glass ?? [0, 0, 196, 44];
    const windows = windowSlots(layout.width, glass);
    ctx.globalAlpha = reveal;
    drawFloor(ctx, layout, planks, palette, hour, windows);
    drawWall(ctx, layout, palette);
    if (atlas) {
      ctx.globalAlpha = reveal * furniture;
      windows.forEach((pane, index) => {
        const layer = windowLayer(atlas, look, index, hour, level);
        const prop = PROPS.window;
        if (layer) ctx.drawImage(layer, pane.x - prop.anchor[0] - glass[0], pane.y - prop.anchor[1] - glass[1], prop.size[0], prop.size[1]);
      });
    }
    for (const zone of layout.zones) {
      ctx.globalAlpha = reveal * zoneAlpha(zone.key);
      drawRug(ctx, zone, teams.get(zone.key)?.color || '#8f7ae6', palette, pile);
    }
    if (atlas) {
      ctx.globalAlpha = reveal * furniture;
      const routers = new Set([...teams.values()].filter(team => team.kind === 'router').map(team => team.key));
      drawFurniture(ctx, atlas, layout, routers, zoneAlpha, key => blend.decorAlpha.get(key) ?? 1, blend.watchdogAlpha);
      for (const zone of layout.zones) {
        zone.pods.forEach((pod, index) => {
          const layer = podLayer(atlas, look, zone.key, index, level, layout);
          if (!layer) return;
          const fading = blend.podAlpha.get(`${zone.key}:${index}`);
          ctx.globalAlpha = reveal * furniture * zoneAlpha(zone.key) * (fading ?? 1);
          // A pod arriving or leaving in a room that is still growing or shrinking stays inside the room.
          drawInside(ctx, layer, 0, 0, layer.width, layer.height, pod.x - POD_BLEED, pod.y - POD_BLEED, pod.w + POD_BLEED * 2,
            pod.h + POD_BLEED * 2, fading === undefined ? null : zone);
        });
      }
    }
    ctx.globalAlpha = 1;
  }

  function draw(frame: SceneFrame) {
    const { ctx } = frame;
    const layout = layoutNow();
    if (!layout || !office || reveal <= 0) return;
    const devicePerUnit = (window.devicePixelRatio || 1) / frame.pixel;
    const level = layerLevel(devicePerUnit);
    const atlasSource = art.atlas();
    // The look follows the art actually shown, so a theme change reskins floor, furniture and people at once.
    const look = art.theme() ?? theme;
    const palette = PALETTE[look];
    const hour = new Date(performance.timeOrigin + frame.now).getHours();
    const night = !palette.daylight || hour < 7 || hour >= 19;
    const atlas: Atlas | null = atlasSource && art.theme() ? { image: atlasSource, props: PROPS } : null;
    const blend = shown!;
    const zoneAlpha = (key: string) => blend.zoneAlpha.get(key) ?? 1;
    const beat = Math.floor(frame.now / BEAT_MS);
    const teams = new Map(office.teams.map(team => [team.key, team]));

    frame.world();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    const m = ctx.getTransform();
    const view = `${ctx.canvas.width}x${ctx.canvas.height}:${m.a}:${m.d}:${m.e}:${m.f}|${shapeKey}|${look}`;
    const settled = !moving && reveal >= 1 && (!atlas || furniture >= 1);
    const colors = office.teams.map(team => team.color).join();
    drawCached(ctx, floorLayer, settled ? `${view}|${Boolean(atlas)}|${level}|${hour}|${colors}` : '',
      target => paintFloor(target, layout, blend, atlas, look, level, hour, teams));
    if (atlas) {
      ctx.globalAlpha = reveal * furniture;
      drawBoard(ctx, layout);
      drawTrophies(ctx, layout, office.trophies);
    }
    /** A person sitting at a desk that arrives or leaves with its room or pod shows with that desk. */
    const roomOf = (actor: Actor): Drawn['room'] => {
      if (actor.walk || !actor.at || !samePlace(actor.at, actor.home)) return SETTLED_ROOM;
      const home = actor.home;
      if (home.kind === 'watchdog') return blend.watchdogAlpha < 1 ? { alpha: blend.watchdogAlpha, clip: null } : SETTLED_ROOM;
      if (home.kind !== 'seat') return SETTLED_ROOM;
      const pod = home.slot < 0 ? undefined : blend.podAlpha.get(`${home.zone}:${Math.floor(home.slot / SEATS_PER_POD)}`);
      const alpha = zoneAlpha(home.zone) * (pod ?? 1);
      if (alpha >= 1 && pod === undefined) return SETTLED_ROOM;
      return { alpha, clip: pod === undefined ? null : layout.zones.find(zone => zone.key === home.zone) ?? null };
    };
    // People, back to front, with their lit screens beneath them.
    const drawn: Drawn[] = [];
    const sceneStill = still || frame.still;
    for (const actor of actors.values()) {
      const pose = actorPose(actor, layout, beat, sceneStill);
      if (pose) drawn.push({ actor, pose, ...sheetOf(actor, level), room: roomOf(actor) });
    }
    drawn.sort((a, b) => a.pose.y - b.pose.y);
    if (atlas) {
      for (const item of drawn) {
        const seat = seatAt(layout, item.actor.home);
        if (!seat || item.actor.screen <= 0) continue;
        const box = screenBox(seat, item.actor.home.kind === 'watchdog' ? layout.watchdog?.station ?? null : null);
        ctx.globalAlpha = reveal * furniture * item.actor.screen * item.room.alpha;
        sprite(ctx, box.name, box.x, box.y, box.w, box.h, item.room.clip);
      }
    }
    const hoverId = frame.hover, focusId = frame.focus;
    for (const item of drawn) {
      if (item.actor.id === selected || item.actor.id === hoverId) {
        const ring = boxOf(item.pose);
        ctx.globalAlpha = reveal * item.pose.alpha * item.room.alpha * (item.actor.id === selected ? 1 : 0.55);
        ctx.beginPath(); ctx.ellipse(ring.x + ring.w / 2, ring.y + ring.h + 1, ring.w * 0.7, 4.5, 0, 0, Math.PI * 2);
        ctx.strokeStyle = item.actor.id === selected ? '#f3d488' : 'rgba(251, 241, 218, 0.9)'; ctx.lineWidth = 2; ctx.stroke();
      }
      ctx.globalAlpha = reveal * (atlas ? furniture : 0);
      drawPerson(ctx, item);
    }
    ctx.globalAlpha = reveal * furniture;
    if (atlas) drawDocuments(ctx, layout);
    drawCached(ctx, lightLayer, settled ? `${view}|${night}` : '', target => {
      target.globalAlpha = reveal;
      drawLighting(target, layout, palette, night, layout.zones);
      target.globalAlpha = 1;
    });
    // Signals over the light: the Watchdog's beacon and the marks above heads.
    const watchdog = office.teams.find(team => team.kind === 'watchdog')?.lead;
    if (layout.watchdog && watchdog?.emphasis === 'exception') {
      const [sx, sy] = PROPS['watchdog-station'].slots!.alarm;
      const glow = spriteArt('alarm');
      sprite(ctx, 'alarm', layout.watchdog.station.x + sx - glow.anchor[0], layout.watchdog.station.y + sy - glow.anchor[1], glow.size[0], glow.size[1]);
    }
    for (const item of drawn) {
      const emphasis = item.actor.person.emphasis;
      if (!emphasis || !item.sheet) continue;
      const name: SpriteName = emphasis === 'exception' ? 'emblem-exception' : sceneStill ? 'emblem-decision-still' : 'emblem-decision';
      const mark = spriteArt(name);
      const box = boxOf(item.pose);
      const bob = emphasis === 'decision' && !sceneStill ? Math.sin(((beat + item.actor.phase) * Math.PI) / 6) * 1.6 : 0;
      ctx.globalAlpha = reveal * item.pose.alpha * item.fade * item.room.alpha;
      sprite(ctx, name, box.x + box.w / 2 - mark.anchor[0], box.y - mark.size[1] + bob, mark.size[0], mark.size[1]);
    }
    ctx.globalAlpha = reveal;
    // Labels after the light, so they stay legible at night.
    plate(ctx, text.ownerPlate, layout.owner.room.x + 14, layout.owner.room.y + layout.owner.room.h - 30);
    if (layout.watchdog && office.teams.some(team => team.kind === 'watchdog')) {
      ctx.globalAlpha = reveal * blend.watchdogAlpha;
      plate(ctx, text.watchdogPlate, layout.watchdog.station.x + 8, layout.watchdog.station.y + layout.watchdog.station.h - 22);
    }
    for (const zone of layout.zones) {
      const team = teams.get(zone.key);
      if (!team) continue;
      ctx.globalAlpha = reveal * zoneAlpha(zone.key);
      banner(ctx, team, zone.banner.x, zone.banner.y);
    }
    ctx.globalAlpha = 1;
    const focused = focusId ? hits.find(hit => hit.id === focusId) : undefined;
    if (focused) frame.painter.hotspot(ctx, [focused.x - 3, focused.y - 3, focused.w + 6, focused.h + 6], frame.pixel, frame.now, 'focus');
    if (focusId !== lastFocus) {
      lastFocus = focusId;
      if (focused && !visible(focused)) followId = focused.id;
    }
    const tipHit = hits.find(hit => hit.id === (hoverId || focusId));
    frame.screen();
    if (tipHit && tip.id === tipHit.id && tip.alpha > 0) tooltip(ctx, frame, tipHit, tip.alpha);
  }

  /** True when a box lies inside the frame the camera shows (insets excluded). */
  function visible(box: Rect): boolean {
    const topLeft = screenToWorld(camera, { x: camera.insets.left, y: camera.insets.top });
    const bottomRight = screenToWorld(camera, { x: camera.viewWidth - camera.insets.right, y: camera.viewHeight - camera.insets.bottom });
    return box.x >= topLeft.x && box.y >= topLeft.y && box.x + box.w <= bottomRight.x && box.y + box.h <= bottomRight.y;
  }

  /* ------------------------------------------------------------------ the stage's scene */

  const scene: Scene = {
    get width() { return layoutNow()?.width ?? 1280; },
    get height() { return layoutNow()?.height ?? 720; },
    // The floor plan is fitted to the view, so the office covers every pixel at every camera position.
    opaque: true,
    backdrop() { return undefined; },
    step(dt) {
      if (disposed) return;
      const ready = art.theme() !== null;
      if (layoutNow() && reveal < 1) reveal = Math.min(1, reveal + dt / ART_FADE_MS);
      if (ready && furniture < 1) furniture = Math.min(1, furniture + dt / ART_FADE_MS);
      for (const [url, value] of sheetFade) if (value < 1) sheetFade.set(url, Math.min(1, value + dt / ART_FADE_MS));
      if (moving) {
        moving = advanceChange(moving, dt, TWEEN_MS);
        shown = moving ? showChange(moving, TWEEN_MS) : settledBlend(target!);
        applyWorld(false);
        targetsDirty = true;
      }
      const layout = layoutNow();
      if (layout) {
        for (const [id, actor] of actors) {
          const moving = isMoving(actor);
          if (moving) { stepActor(actor, dt, layout); targetsDirty = true; }
          if (actor.gone) { actors.delete(id); drawnSheet.delete(id); }
        }
      }
      for (const [id, age] of landed) { if (age >= DROP_MS) landed.delete(id); else landed.set(id, age + dt); }
      const tipId = lastHover || lastFocus;
      if (tip.id !== tipId) tip = { id: tipId, alpha: 0 };
      if (tip.id && tip.alpha < 1) tip.alpha = Math.min(1, tip.alpha + dt / TIP_MS);
    },
    draw(frame) {
      lastHover = frame.hover;
      if (targetsDirty) rebuildTargets();
      draw(frame);
    },
    nextWake(now, motionless) {
      const hour = HOUR_MS - ((performance.timeOrigin + now) % HOUR_MS);
      if (!layoutNow()) return Infinity;
      if (reveal < 1 || (art.theme() !== null && furniture < 1) || [...sheetFade.values()].some(value => value < 1)) return 0;
      if (moving || landed.size || (tip.id && tip.alpha < 1) || (lastHover || lastFocus) !== tip.id) return 0;
      let looping = false;
      for (const actor of actors.values()) {
        if (isMoving(actor)) return 0;
        if (actor.person.emphasis === 'decision' || actor.at?.kind === 'spot' || (actor.person.state === 'working' && actor.at && samePlace(actor.at, actor.home))) looping = true;
      }
      if (motionless || still || !looping) return hour;
      return Math.min(hour, BEAT_MS - (now % BEAT_MS));
    },
    hit(point) { return hitAt(point)?.id ?? ''; },
    targets() {
      if (targetsDirty) rebuildTargets();
      return targetList;
    },
    activate(id) {
      const hit = hits.find(each => each.id === id);
      if (!hit || !office) return;
      if (hit.kind === 'person') ports.open({ kind: 'person', id });
      else if (hit.kind === 'document') ports.open({ kind: 'document', id });
      else if (id === 'pile') ports.open({ kind: 'document', id: hit.data.document });
      else if (id === 'board' && office.whiteboard) ports.open({ kind: 'person', id: office.whiteboard.owner });
      else ports.open({ kind: 'document', id });
    },
    follow() {
      if (!followId) return undefined;
      const hit = hits.find(each => each.id === followId);
      if (!hit) { followId = ''; return undefined; }
      const goal = { x: hit.x + hit.w / 2, y: hit.y + hit.h / 2 };
      const probe = clampCamera({ ...camera, insets: { ...camera.insets }, x: goal.x, y: goal.y });
      if (Math.abs(probe.x - camera.x) + Math.abs(probe.y - camera.y) < 1) { followId = ''; return undefined; }
      return goal;
    },
    setStill(next) {
      still = next;
      if (!still) return;
      if (moving && target) { moving = null; shown = settledBlend(target); applyWorld(false); }
      reveal = layoutNow() ? 1 : reveal;
      furniture = art.theme() ? 1 : furniture;
      for (const url of sheetFade.keys()) sheetFade.set(url, 1);
      landed.clear();
      for (const actor of actors.values()) {
        settleActor(actor);
        if (actor.gone) { actors.delete(actor.id); drawnSheet.delete(actor.id); }
      }
      targetsDirty = true;
    },
  };

  return {
    scene,
    /** A new projection of the office; `settling` while its first reads have not all answered. */
    update(next: Office, isSettling: boolean) {
      const previousTeams = new Set((office?.teams || []).map(team => team.key));
      const firstSight = !office;
      const quiet = settling || isSettling;
      office = next;
      settling = isSettling;
      allocate();
      relayout(false);
      placePeople(firstSight ? new Set(next.teams.map(team => team.key)) : previousTeams, quiet);
      syncDocuments(!firstSight && !quiet && !still);
      targetsDirty = true;
      ports.wake();
    },
    setViewport(width: number, height: number) {
      if (!width || !height || (width === viewport.width && height === viewport.height)) return;
      viewport = { width, height };
      relayout(true);
      if (office) placePeople(new Set(office.teams.map(team => team.key)), true);
      ports.wake();
    },
    setTheme(next: WorldTheme) {
      theme = next;
      // Everyone on the floor is redressed in the same frame as the furniture.
      art.setTheme(next, [...actors.values()].map(actor => sheetFor(actor.person, next)));
      ports.wake();
    },
    /** Plays one real report: its author carries the document to the owner's desk and walks back (or out). */
    walk(plan: WalkPlan, done: () => void, dropped?: () => void) {
      pending.add(plan.document.id);
      seenDocuments.add(plan.document.id);
      const land = () => {
        pending.delete(plan.document.id);
        if (!still) landed.set(plan.document.id, 0);
        targetsDirty = true;
        dropped?.();
      };
      const actor = actors.get(plan.person);
      const layout = layoutNow();
      const started = !still && actor && layout && startErrand(actor, { document: plan.document, leaves: plan.leaves, onDrop: land, onDone: done }, layout);
      if (!started) { land(); done(); }
      ports.wake();
    },
    select(id: string | null) {
      selected = id || '';
      followId = selected;
      targetsDirty = true;
      ports.wake();
    },
    documentCounts() {
      const { results, fan } = deskDocuments();
      return { reports: results.length + fan.filter(document => document.kind === 'report').length,
        deliverables: fan.filter(document => document.kind === 'deliverable').length };
    },
    isWalking: (id: string) => Boolean(actors.get(id)?.walk),
    layout: () => target,
    /** Read-only state for specifications and audits. */
    inspect() {
      const layout = layoutNow();
      const counts = (slot: CachedLayer) => ({ hits: slot.hits, builds: slot.builds, direct: slot.direct });
      return {
        tweening: Boolean(moving), layout: target, shown: layout,
        fading: { zones: Object.fromEntries(shown?.zoneAlpha ?? []), pods: Object.fromEntries(shown?.podAlpha ?? []),
          decor: Object.fromEntries(shown?.decorAlpha ?? []), watchdog: shown?.watchdogAlpha ?? 1 },
        layers: { floor: counts(floorLayer), light: counts(lightLayer) },
        capacity: Object.fromEntries(capacity),
        actors: [...actors.values()].map(actor => ({
          id: actor.id, team: actor.person.team, rest: actor.rest.kind, at: actor.at?.kind ?? null, walking: Boolean(actor.walk),
          alpha: actor.alpha, errand: actor.errand?.phase ?? null,
          pose: layout ? actorPose(actor, layout, 0, true) : null,
        })),
      };
    },
    dispose() {
      disposed = true;
      actors.clear(); podLayers.clear(); windowLayers.clear(); sheetLayers.clear(); patterns = null; hits = []; targetList = [];
      floorLayer.canvas = lightLayer.canvas = null;
    },
  };
}
export type WorldScene = ReturnType<typeof createWorldScene>;
