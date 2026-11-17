/** Tofu office floor plan: pure geometry in scene units (CSS pixels at scale 1).
 * The owner's glass office sits top-left under the window wall; team zones fill rows to its right and below
 * (one zone per team: a banner, the lead's head desk, then pods of two facing desk rows), each as wide as its
 * desks need and as tall as its row; a row's spare width is a commons (commons.ts: furnished breakout areas)
 * unless it is too narrow for one, when the row's rooms share it, and so are the floor in front of the owner's
 * office and the bottom strip's free stretch. The bottom strip holds the entrance with the Watchdog's station and
 * the lounge. Walkways (routes.ts walks them): each zone's
 * left aisle, one corridor under every row, the spine along the left wall and the bottom lane to the entrance,
 * so every desk reaches the owner's door and the way out. Seats keep their slot while their person stays
 * (allocateSlots), so the office never reshuffles.
 * Entry points: fitOffice, layoutOffice, allocateSlots, waitingSpots, shelfSlots, corridorBelow, bottomLane. */
import { CELL } from './assets/manifest.generated';
import { COMMONS_MIN_H, COMMONS_MIN_W, furnishCommons } from './commons';
import { stableHash } from './hash';

export interface Point { x: number; y: number }
export interface Rect { x: number; y: number; w: number; h: number }
export type Facing = 'down' | 'up';
/** A desk place. (x, y) is the chair centre; `cut` is the screen line where a seated body disappears
 * behind its desk (facing down) or chair back (facing up); `stand` is where its person steps out to walk. */
export interface Seat { x: number; y: number; facing: Facing; cut: number; stand: Point; desk: Rect; screen: Rect }
export interface Pod { x: number; y: number; w: number; h: number; columns: number }
export interface ZoneLayout {
  key: string; x: number; y: number; w: number; h: number; row: number;
  banner: Point; lead: Seat; pods: Pod[]; seats: Seat[]; routine: Point;
}
/** Furniture drawn from the art atlas at its anchor point (`x`, `y`), or drawn in code from its top-left corner
 * and size (`w`, `h`; diorama.ts drawPiece). */
export type DecorKind = 'plant' | 'tall-plant' | 'cooler' | 'printer' | 'meeting'
  | 'shelf' | 'cabinet' | 'sofa' | 'armchair' | 'table' | 'round-table' | 'bench' | 'lamp' | 'rug' | 'credenza';
/** `key` names the item's place (the n-th gap of the window wall, the plant beside a team's room, the cooler, a
 * piece of a commons), so a floor-plan blend moves each item to its own new place; `variant` is stable per key. */
export interface DecorItem { key: string; kind: DecorKind; x: number; y: number; w?: number; h?: number; variant: number }
/** A row's breakout area (commons.ts); `key` names its row. */
export interface CommonsArea extends Rect { key: string }
export interface OfficeLayout {
  width: number; height: number; wall: number;
  owner: { room: Rect; desk: Rect; chair: Point; door: Point; drop: Point; tray: Rect; whiteboard: Rect; shelf: Rect };
  zones: ZoneLayout[]; watchdog: { station: Rect; seat: Seat } | null;
  entrance: Point; lounge: Rect; bar: Rect; spine: number; corridors: number[]; decor: DecorItem[]; commons: CommonsArea[];
}
export interface TeamShape { key: string; seats: number; kind: 'router' | 'project' | 'watchdog' }

export const WALL = 84;
export const MARGIN = 40;
export const SEAT_PITCH = 70;
export const POD_COLUMNS = 4;
export const DESK_W = 56;
export const DESK_D = 28;
const POD_PAD = 10;
const AISLE = 18;
const CHAIR_ROW = 32;
const DIVIDER = 6;
const POD_GAP = 22;
export const POD_W = POD_COLUMNS * SEAT_PITCH + POD_PAD * 2;
export const SEATS_PER_POD = POD_COLUMNS * 2;
export const POD_H = AISLE + CHAIR_ROW + DESK_D + DIVIDER + DESK_D + CHAIR_ROW + AISLE;
const ZONE_PAD = 18;
/** Banner row plus the head desk; the lead sits low enough that a mark above its head clears the banner. The
 * first pod sits at least this far down (tagBands). */
const HEAD_H = 156;
/** The head desk's top and depth below the room's top. */
const LEAD_DESK_Y = 92;
const LEAD_DESK_H = 32;

/** Desk tags: the floor plan keeps room for them (tagBands), life.ts draws them and scene.ts places them. A tag's
 * text is TAG_FONT units at zoom 1 and never below TAG_MIN_PX CSS pixels; a Worker's pill is TAG_PILL text units
 * tall and a lead's TAG_LEAD_PILL; lines of tags step by a lead's pill and a hair; neighbours on a line keep
 * TAG_GAP CSS pixels apart. */
export const TAG_FONT = 8.5;
export const TAG_MIN_PX = 10;
export const TAG_PILL = 12;
export const TAG_LEAD_PILL = 14;
export const TAG_GAP = 4;
/** Scene units per tag text unit at `pixel` scene units per CSS pixel. */
export const tagUnit = (pixel: number) => Math.max(TAG_FONT, TAG_MIN_PX * pixel) / TAG_FONT;
/** The step between two lines of tags. */
export const tagStep = (pixel: number) => TAG_LEAD_PILL * tagUnit(pixel) + 1.5;

/** The centre of a seated person's desk tag, `line` lines outward from its desk row: over the head of someone
 * facing the viewer (a seated body shows SEATED_SHOWN of its cell above the desk's cut line), under the chair of
 * someone with their back to the viewer. A head desk's tag keeps its first line. */
export function deskTagY(seat: Seat, line: number, pixel: number): number {
  const outward = line * tagStep(pixel);
  if (seat.facing === 'down') return seat.cut - CELL.h * SEATED_SHOWN - 5 - (seat.desk.w > DESK_W ? 0 : outward);
  return seat.y + 21 + outward;
}

/** The floor a plan laid out at `scale` keeps round its pods for desk tags at the fit zoom, the farthest the camera
 * zooms out (tags never shrink below TAG_MIN_PX, so a smaller scale keeps more floor): `head` from a room's top to
 * its first pod (the head desk, then two lines of tags over the front row), `stack` between stacked pods (two lines
 * under the back row, two over the next front row), `foot` under the last pod (two lines under its back row). Two
 * lines, because a Worker's tag may span two desks (life.ts tagBox), so neighbours alternate. */
function tagBands(scale: number): TagBands {
  const pixel = 1 / scale;
  const half = (TAG_PILL * tagUnit(pixel)) / 2, clear = TAG_GAP * pixel;
  const seats = podSeats({ x: 0, y: 0, w: POD_W, h: POD_H, columns: 1 });
  const front = seats.find(seat => seat.facing === 'down')!, back = seats.find(seat => seat.facing === 'up')!;
  const over = half + clear - deskTagY(front, 1, pixel);
  const under = deskTagY(back, 1, pixel) + half + clear - POD_H;
  return { head: Math.max(HEAD_H, LEAD_DESK_Y + LEAD_DESK_H + over), stack: Math.max(POD_GAP, over + under), foot: Math.max(ZONE_PAD, under) };
}
interface TagBands { head: number; stack: number; foot: number }
const IDLE_ZONE_W = 250;
const ZONE_GAP = 28;
const CORRIDOR = 44;
export const OFFICE_W = 318;
export const OFFICE_H = 248;
const BOTTOM_H = 168;
/** The whiteboard holds a grid of notes, one per task on the owner's board. */
export const BOARD_GRID = { columns: 4, rows: 3 } as const;
/** The trophy shelf: its planks (offsets from the shelf top) and trophies standing on them. */
export const SHELF_PLANKS = [24, 52] as const;
export const TROPHY_W = 10;
export const TROPHY_H = 14;
const TROPHY_PITCH = 14;
/** A waiting person's spacing along the glass wall and how far in front of it their feet stand. */
const WAIT_PITCH = 34;
const WAIT_GAP = 26;
/** Below the feet of the line at the owner's door: its first two lines of tags (scene.ts tagAnchor). */
const LOBBY_CLEAR = 44;
/** Half a walker's width and a step: what furniture keeps clear of a walkway's centre line. */
const WALKER_CLEAR = 24;
/** Half the printer's width (assets PROPS printer). */
const PRINTER_HALF = 23;
/** Seated bodies show this share of their full height above the cut line. */
export const SEATED_SHOWN = 0.58;
/** Full standing character height in scene units. */
export const PERSON_H = 52;

/** The office is never laid out narrower than this many scene units; a narrower window scales it down. */
export const MIN_WIDTH = 1280;

export function sceneScale(viewportWidth: number): number {
  return Math.min(1, viewportWidth / MIN_WIDTH);
}

/** The largest scale at which the whole office fits the window, so the camera's zoom 1 shows all of it with no
 * letterbox. A short office is drawn bigger (up to the scale that lays it out exactly MIN_WIDTH units wide)
 * instead of leaving empty floor; a tall one is laid out wider (more rooms per row) at a smaller scale. Each
 * search stops when one more step would move the right edge by less than a pixel. */
export function fitOffice(viewportWidth: number, viewportHeight: number, teams: ReadonlyArray<TeamShape>): { scale: number; layout: OfficeLayout } {
  const low = sceneScale(viewportWidth);
  const high = viewportWidth / MIN_WIDTH;
  const fits = (scale: number) => layoutOffice(viewportWidth, teams, 0, scale).height * scale <= viewportHeight;
  const search = (fitting: number, tooBig: number) => {
    while ((tooBig - fitting) * viewportWidth > 1) {
      const middle = (fitting + tooBig) / 2;
      if (fits(middle)) fitting = middle; else tooBig = middle;
    }
    return fitting;
  };
  let best = low;
  if (viewportHeight && fits(low)) {
    if (high > low) best = fits(high) ? high : search(low, high);
  } else if (viewportHeight) {
    // Halving terminates: once every room shares one row, the floor's height stops growing while the window's
    // height in scene units keeps doubling.
    let tooBig = low;
    best = low / 2;
    while (!fits(best)) { tooBig = best; best /= 2; }
    best = search(best, tooBig);
  }
  return { scale: best, layout: layoutOffice(viewportWidth, teams, viewportHeight, best) };
}

/** Desks keep their slot while their person stays; a newcomer takes the lowest free slot. */
export function allocateSlots(previous: ReadonlyMap<string, number>, ids: ReadonlyArray<string>): Map<string, number> {
  const next = new Map<string, number>();
  const taken = new Set<number>();
  for (const id of ids) {
    const slot = previous.get(id);
    if (slot !== undefined) { next.set(id, slot); taken.add(slot); }
  }
  let free = 0;
  for (const id of ids) {
    if (next.has(id)) continue;
    while (taken.has(free)) free += 1;
    next.set(id, free); taken.add(free);
  }
  return next;
}

/** The low partition between a pod's two facing desk rows. */
export function podDivider(pod: Pod): number {
  return pod.y + AISLE + CHAIR_ROW + DESK_D;
}

function podSeats(pod: Pod): Seat[] {
  const seats: Seat[] = [];
  const topDesk = pod.y + AISLE + CHAIR_ROW;
  const bottomDesk = topDesk + DESK_D + DIVIDER;
  for (let column = 0; column < pod.columns; column += 1) {
    const x = pod.x + POD_PAD + SEAT_PITCH * (column + 0.5);
    const desk = { x: x - DESK_W / 2, y: topDesk, w: DESK_W, h: DESK_D };
    seats.push({ x, y: pod.y + AISLE + CHAIR_ROW / 2, facing: 'down', cut: topDesk + 3, stand: { x, y: pod.y + AISLE / 2 },
      desk, screen: { x: x + 8, y: topDesk + 2, w: 16, h: 9 } });
  }
  for (let column = 0; column < pod.columns; column += 1) {
    const x = pod.x + POD_PAD + SEAT_PITCH * (column + 0.5);
    const desk = { x: x - DESK_W / 2, y: bottomDesk, w: DESK_W, h: DESK_D };
    const chair = bottomDesk + DESK_D + CHAIR_ROW / 2;
    seats.push({ x, y: chair, facing: 'up', cut: chair - 4, stand: { x, y: pod.y + pod.h - AISLE / 2 },
      desk, screen: { x: x - 12, y: bottomDesk - 9, w: 24, h: 15 } });
  }
  return seats;
}

/** A team without unfinished work keeps only its head desk; desks appear in pods of two facing rows. */
type ZoneShape = ReturnType<typeof zoneShape>;
/** `columns` asks for at most that many pods side by side; by default the pods form a near-square block. */
function zoneShape(shape: TeamShape, width: number, bands: TagBands, wanted?: number) {
  const pods = Math.ceil(shape.seats / SEATS_PER_POD);
  if (!pods) return { pods, columns: 1, w: IDLE_ZONE_W, h: HEAD_H + ZONE_PAD };
  const fit = Math.max(1, Math.floor((width - ZONE_PAD * 2 + POD_GAP) / (POD_W + POD_GAP)));
  const columns = Math.min(fit, wanted ?? (pods <= 2 ? pods : Math.ceil(Math.sqrt(pods))));
  const rows = Math.ceil(pods / columns);
  return {
    pods, columns,
    w: ZONE_PAD * 2 + columns * POD_W + (columns - 1) * POD_GAP,
    h: bands.head + rows * POD_H + (rows - 1) * bands.stack + bands.foot,
  };
}

interface ZoneRow { start: number; y: number; height: number; items: Array<{ key: string; shape: ZoneShape }> }

/** Rows fill greedily left to right, the first one beside the owner's office. */
function arrangeRows(teams: ReadonlyArray<TeamShape>, width: number, floorTop: number, bands: TagBands,
  columns: ReadonlyMap<string, number>): ZoneRow[] {
  let x = MARGIN + OFFICE_W + ZONE_GAP;
  let current: ZoneRow = { start: x, y: floorTop, height: OFFICE_H, items: [] };
  const rows = [current];
  for (const team of teams) {
    const shape = zoneShape(team, width - MARGIN * 2, bands, columns.get(team.key));
    if (x > MARGIN && x + shape.w > width - MARGIN) {
      x = MARGIN;
      current = { start: x, y: current.y + current.height + CORRIDOR, height: 0, items: [] };
      rows.push(current);
    }
    current.items.push({ key: team.key, shape });
    x += shape.w + ZONE_GAP;
    current.height = Math.max(current.height, shape.h);
  }
  return rows;
}

const rowsBottom = (rows: ReadonlyArray<ZoneRow>) => rows[rows.length - 1].y + rows[rows.length - 1].height;

/** The shortest floor plan found by narrowing pod blocks one column at a time: a team whose pods stack
 * instead of spreading can let the next team share its row, which shortens the whole office. Each step
 * takes the single narrowing that shortens the plan most and stops when none does. */
function compactRows(teams: ReadonlyArray<TeamShape>, width: number, floorTop: number, bands: TagBands): ZoneRow[] {
  const columns = new Map<string, number>();
  let rows = arrangeRows(teams, width, floorTop, bands, columns);
  for (;;) {
    let best: { key: string; columns: number; rows: ZoneRow[] } | null = null;
    for (const row of rows) for (const item of row.items) {
      if (item.shape.columns <= 1) continue;
      const trial = new Map(columns).set(item.key, item.shape.columns - 1);
      const candidate = arrangeRows(teams, width, floorTop, bands, trial);
      if (rowsBottom(candidate) < rowsBottom(best?.rows ?? rows)) best = { key: item.key, columns: item.shape.columns - 1, rows: candidate };
    }
    if (!best) return rows;
    columns.set(best.key, best.columns);
    rows = best.rows;
  }
}

/** `w` and `h` may exceed the shape's own size (a row's height, a row whose spare width is too narrow for a
 * commons); its pods stay centred under the head desk. */
function buildZone(key: string, x: number, y: number, row: number, shape: ZoneShape, w: number, h: number, bands: TagBands): ZoneLayout {
  const pods: Pod[] = [];
  const left = x + (w - shape.w) / 2;
  // The head desk and its pods stay under the team sign; a taller room's spare floor below them is furnished.
  const top = y;
  for (let index = 0; index < shape.pods; index += 1) {
    const column = index % shape.columns;
    const line = Math.floor(index / shape.columns);
    pods.push({ x: left + ZONE_PAD + column * (POD_W + POD_GAP), y: top + bands.head + line * (POD_H + bands.stack),
      w: POD_W, h: POD_H, columns: POD_COLUMNS });
  }
  const leadX = x + w / 2;
  const leadDesk = { x: leadX - 46, y: top + LEAD_DESK_Y, w: 92, h: LEAD_DESK_H };
  const lead: Seat = {
    x: leadX, y: top + 78, facing: 'down', cut: leadDesk.y + 3, stand: { x: leadX, y: top + HEAD_H - 4 },
    desk: leadDesk, screen: { x: leadX + 14, y: leadDesk.y + 2, w: 20, h: 10 },
  };
  return {
    key, x, y, w, h, row, banner: { x: x + ZONE_PAD, y: y + 12 }, lead, pods,
    seats: pods.flatMap(podSeats), routine: { x: x + w - ZONE_PAD, y: y + 16 },
  };
}

/** `viewportHeight` (CSS px) makes the floor reach the bottom of the window; the lounge, bar, entrance and
 * Watchdog station sit along that bottom edge. */
export function layoutOffice(viewportWidth: number, teams: ReadonlyArray<TeamShape>, viewportHeight = 0,
  scale = sceneScale(viewportWidth)): OfficeLayout {
  const width = Math.max(MIN_WIDTH, Math.round(viewportWidth / scale));
  const floorTop = WALL + 22;
  // Each row reaches the right wall and its rooms take the row's height, so the teams read as one even block of
  // rooms: a row's spare width is its commons when one fits there, else its rooms share it.
  const bands = tagBands(scale);
  const rows = compactRows(teams.filter(each => each.kind !== 'watchdog'), width, floorTop, bands);
  // A window taller than the plan gives the rows its spare height (furnished rooms, not bare corridor floor).
  const spare = Math.max(0, Math.floor(viewportHeight / scale) - (rowsBottom(rows) + CORRIDOR + BOTTOM_H + MARGIN));
  rows.forEach((line, index) => { line.y += (spare / rows.length) * index; line.height += spare / rows.length; });
  const zones: ZoneLayout[] = [];
  const commons: CommonsArea[] = [];
  const rowBottoms: number[] = [];
  rows.forEach((line, row) => {
    const used = line.items.reduce((sum, item) => sum + item.shape.w + ZONE_GAP, -ZONE_GAP);
    const spare = line.items.length ? Math.max(0, width - MARGIN - line.start - used) : 0;
    const breakout = spare - ZONE_GAP >= COMMONS_MIN_W;
    const extra = breakout || !line.items.length ? 0 : spare / line.items.length;
    let left = line.start;
    for (const item of line.items) {
      const w = item.shape.w + extra;
      zones.push(buildZone(item.key, left, line.y, row, item.shape, w, line.height, bands));
      left += w + ZONE_GAP;
    }
    if (breakout) commons.push({ key: `commons:${row}`, x: left, y: line.y, w: width - MARGIN - left, h: line.height });
    rowBottoms.push(line.y + line.height);
  });
  const corridors = rowBottoms.map(bottom => bottom + CORRIDOR / 2);
  const height = Math.max(rowBottoms[rowBottoms.length - 1] + CORRIDOR + BOTTOM_H + MARGIN, Math.floor(viewportHeight / scale));
  const bottomTop = height - BOTTOM_H - MARGIN;

  const room = { x: MARGIN, y: floorTop, w: OFFICE_W, h: OFFICE_H };
  const desk = { x: room.x + room.w / 2 - 70, y: room.y + 92, w: 140, h: 44 };
  const door = { x: room.x + room.w - 54, y: room.y + room.h };
  const owner = {
    room, desk, chair: { x: desk.x + desk.w / 2, y: desk.y - 14 }, door,
    drop: { x: desk.x + desk.w / 2, y: desk.y + desk.h + 26 },
    tray: { x: desk.x + 10, y: desk.y + 6, w: desk.w - 20, h: desk.h - 12 },
    whiteboard: { x: room.x + room.w - 110, y: room.y + 4, w: 96, h: 64 },
    shelf: { x: room.x + 14, y: room.y + 2, w: 120, h: 56 },
  };
  // The floor in front of the owner's office, below the line at its door (and that line's first tags) and left of
  // the walk from the door down to the corridor, is a commons when a group fits there.
  const lobbyTop = door.y + WAIT_GAP + LOBBY_CLEAR;
  const lobby = { key: 'commons:owner', x: MARGIN, y: lobbyTop, w: door.x - WALKER_CLEAR - MARGIN, h: rowBottoms[0] - lobbyTop };
  if (lobby.w >= COMMONS_MIN_W && lobby.h >= COMMONS_MIN_H) commons.unshift(lobby);
  const hasWatchdog = teams.some(team => team.kind === 'watchdog');
  const entrance = { x: MARGIN + 150, y: height - 6 };
  const stationRect = { x: MARGIN + 210, y: bottomTop + 26, w: 150, h: 110 };
  const stationX = stationRect.x + stationRect.w / 2;
  const stationDesk = { x: stationX - 52, y: stationRect.y + 50, w: 104, h: 32 };
  const watchdog = hasWatchdog ? {
    station: stationRect,
    seat: { x: stationX, y: stationRect.y + 36, facing: 'down' as const, cut: stationDesk.y + 3,
      stand: { x: stationX, y: stationRect.y + stationRect.h + 10 }, desk: stationDesk,
      screen: { x: stationX + 18, y: stationDesk.y + 2, w: 20, h: 10 } },
  } : null;
  const lounge = { x: MARGIN + 420, y: bottomTop + 18, w: 330, h: BOTTOM_H - 36 };
  const bar = { x: width - MARGIN - 300, y: bottomTop + 18, w: 260, h: 70 };
  const decor: DecorItem[] = [];
  const windows = Math.max(3, Math.floor((width - MARGIN * 2) / 300));
  for (let index = 0; index <= windows; index += 1) {
    const at = MARGIN + (index * (width - MARGIN * 2)) / windows;
    if (at > MARGIN + OFFICE_W + 20 && at < width - MARGIN - 20) decor.push({ key: `wall:${index}`, kind: 'tall-plant', x: at, y: floorTop - 6, variant: index % 3 });
  }
  decor.push({ key: 'cooler', kind: 'cooler', x: width - MARGIN - 24, y: bottomTop + 120, variant: 0 });
  const printerX = lounge.x + lounge.w + 70;
  decor.push({ key: 'printer', kind: 'printer', x: printerX, y: bottomTop + 106, variant: 0 });
  // The bottom strip between the printer and the bar, where nobody walks, is a commons when a group fits there.
  const strip = { key: 'commons:bottom', x: printerX + PRINTER_HALF + 8, y: bottomTop + 8, w: bar.x - 8 - (printerX + PRINTER_HALF + 8), h: BOTTOM_H - 16 };
  if (strip.w >= COMMONS_MIN_W && strip.h >= COMMONS_MIN_H) commons.push(strip);
  decor.push({ key: 'lounge-corner', kind: 'tall-plant', x: MARGIN + 18, y: bottomTop + BOTTOM_H - 20, variant: 1 });
  decor.push({ key: 'lounge', kind: 'plant', x: lounge.x + lounge.w - 12, y: lounge.y + 26, variant: 2 });
  for (const zone of zones) {
    if (zone.x + zone.w + ZONE_GAP / 2 < width - MARGIN) {
      decor.push({ key: `beside:${zone.key}`, kind: 'plant', x: zone.x + zone.w + ZONE_GAP / 2, y: zone.y + zone.h - 8, variant: stableHash(zone.key) % 3 });
    }
  }
  for (const area of commons) decor.push(...furnishCommons(area.key, area, stableHash(area.key)));
  return { width, height, wall: WALL, owner, zones, watchdog, entrance, lounge, bar, spine: MARGIN / 2, corridors, decor, commons };
}

/** The walkway along a zone's left edge, inside its rug. */
export function aisleOf(zone: ZoneLayout): number {
  return zone.x + 8;
}

/** The corridor under a zone's row (every zone in a row has the row's height). */
export function corridorBelow(zone: ZoneLayout): number {
  return zone.y + zone.h + CORRIDOR / 2;
}

/** The lane just above the bottom strip (lounge, bar, entrance, Watchdog station). */
export function bottomLane(layout: OfficeLayout): number {
  return layout.height - BOTTOM_H - MARGIN - 6;
}

/** Where people with an open owner question wait: feet on one line in the corridor in front of the owner's
 * glass wall, the first beside the door (the doorway stays clear for walkers), then leftwards as far as the
 * room reaches. Anyone beyond these spots keeps the "?" at their desk. */
export function waitingSpots(layout: OfficeLayout): Point[] {
  const { room, door } = layout.owner;
  const spots: Point[] = [];
  for (let x = door.x - WAIT_PITCH; x >= room.x + WAIT_PITCH / 2; x -= WAIT_PITCH) spots.push({ x, y: door.y + WAIT_GAP });
  return spots;
}

/** Top-left corner of every trophy slot, plank by plank, left to right. */
export function shelfSlots(shelf: Rect): Point[] {
  const columns = Math.floor((shelf.w - 8) / TROPHY_PITCH);
  return SHELF_PLANKS.flatMap(plank => Array.from({ length: columns }, (_, column) => ({
    x: shelf.x + 4 + column * TROPHY_PITCH + (TROPHY_PITCH - TROPHY_W) / 2, y: shelf.y + plank - TROPHY_H,
  })));
}

