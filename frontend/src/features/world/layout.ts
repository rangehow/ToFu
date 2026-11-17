/** Tofu office floor plan: pure geometry in scene units (CSS pixels at scale 1).
 * The owner's glass office sits top-left under the window wall; team zones fill rows to its right and below
 * (one zone per team: a banner, the lead's head desk, then pods of two facing desk rows); the bottom strip
 * holds the entrance with the Watchdog's station and the lounge. Walkways (routes.ts walks them): each zone's
 * left aisle, one corridor under every row, the spine along the left wall and the bottom lane to the entrance,
 * so every desk reaches the owner's door and the way out. Seats keep their slot while their person stays
 * (allocateSlots), so the office never reshuffles.
 * Entry points: fitOffice, layoutOffice, allocateSlots, waitingSpots, shelfSlots, corridorBelow, bottomLane. */
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
/** `key` names the item's place (the n-th gap of the window wall, the plant beside a team's room, the cooler),
 * so a floor-plan blend moves each item to its own new place; `variant` is stable per key. */
export interface DecorItem { key: string; kind: 'plant' | 'tall-plant' | 'shelf' | 'cooler' | 'printer'; x: number; y: number; variant: number }
export interface OfficeLayout {
  width: number; height: number; wall: number;
  owner: { room: Rect; desk: Rect; chair: Point; door: Point; drop: Point; tray: Rect; whiteboard: Rect; shelf: Rect };
  zones: ZoneLayout[]; watchdog: { station: Rect; seat: Seat } | null;
  entrance: Point; lounge: Rect; bar: Rect; spine: number; corridors: number[]; decor: DecorItem[];
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
/** Banner row plus the head desk; the lead sits low enough that a mark above its head clears the banner. */
const HEAD_H = 136;
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
function zoneShape(shape: TeamShape, width: number, wanted?: number) {
  const pods = Math.ceil(shape.seats / SEATS_PER_POD);
  if (!pods) return { pods, columns: 1, w: IDLE_ZONE_W, h: HEAD_H + ZONE_PAD };
  const fit = Math.max(1, Math.floor((width - ZONE_PAD * 2 + POD_GAP) / (POD_W + POD_GAP)));
  const columns = Math.min(fit, wanted ?? (pods <= 2 ? pods : Math.ceil(Math.sqrt(pods))));
  const rows = Math.ceil(pods / columns);
  return {
    pods, columns,
    w: ZONE_PAD * 2 + columns * POD_W + (columns - 1) * POD_GAP,
    h: HEAD_H + rows * POD_H + (rows - 1) * POD_GAP / 2 + ZONE_PAD,
  };
}

interface ZoneRow { start: number; y: number; height: number; items: Array<{ key: string; shape: ZoneShape }> }

/** Rows fill greedily left to right, the first one beside the owner's office. */
function arrangeRows(teams: ReadonlyArray<TeamShape>, width: number, floorTop: number, columns: ReadonlyMap<string, number>): ZoneRow[] {
  let x = MARGIN + OFFICE_W + ZONE_GAP;
  let current: ZoneRow = { start: x, y: floorTop, height: OFFICE_H, items: [] };
  const rows = [current];
  for (const team of teams) {
    const shape = zoneShape(team, width - MARGIN * 2, columns.get(team.key));
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
function compactRows(teams: ReadonlyArray<TeamShape>, width: number, floorTop: number): ZoneRow[] {
  const columns = new Map<string, number>();
  let rows = arrangeRows(teams, width, floorTop, columns);
  for (;;) {
    let best: { key: string; columns: number; rows: ZoneRow[] } | null = null;
    for (const row of rows) for (const item of row.items) {
      if (item.shape.columns <= 1) continue;
      const trial = new Map(columns).set(item.key, item.shape.columns - 1);
      const candidate = arrangeRows(teams, width, floorTop, trial);
      if (rowsBottom(candidate) < rowsBottom(best?.rows ?? rows)) best = { key: item.key, columns: item.shape.columns - 1, rows: candidate };
    }
    if (!best) return rows;
    columns.set(best.key, best.columns);
    rows = best.rows;
  }
}

/** `w` and `h` may exceed the shape's own size (a justified row); its pods stay centred under the head desk. */
function buildZone(key: string, x: number, y: number, row: number, shape: ZoneShape, w: number, h: number): ZoneLayout {
  const pods: Pod[] = [];
  const left = x + (w - shape.w) / 2;
  // The head desk and its pods stay under the team sign; a taller room's spare floor below them is its lounge.
  const top = y;
  for (let index = 0; index < shape.pods; index += 1) {
    const column = index % shape.columns;
    const line = Math.floor(index / shape.columns);
    pods.push({ x: left + ZONE_PAD + column * (POD_W + POD_GAP), y: top + HEAD_H + line * (POD_H + POD_GAP / 2),
      w: POD_W, h: POD_H, columns: POD_COLUMNS });
  }
  const leadX = x + w / 2;
  const leadDesk = { x: leadX - 46, y: top + 92, w: 92, h: 32 };
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
  // Each row is justified: its zones share the leftover width and take the row's height, so the teams read
  // as one even block of rooms with no ragged holes in the floor.
  const rows = compactRows(teams.filter(each => each.kind !== 'watchdog'), width, floorTop);
  // A window taller than the plan gives the rows its spare height (rooms, not bare corridor floor, fill it).
  const spare = Math.max(0, Math.floor(viewportHeight / scale) - (rowsBottom(rows) + CORRIDOR + BOTTOM_H + MARGIN));
  rows.forEach((line, index) => { line.y += (spare / rows.length) * index; line.height += spare / rows.length; });
  const zones: ZoneLayout[] = [];
  const rowBottoms: number[] = [];
  rows.forEach((line, row) => {
    const used = line.items.reduce((sum, item) => sum + item.shape.w + ZONE_GAP, -ZONE_GAP);
    const extra = line.items.length ? Math.max(0, width - MARGIN - line.start - used) / line.items.length : 0;
    let left = line.start;
    for (const item of line.items) {
      const w = item.shape.w + extra;
      zones.push(buildZone(item.key, left, line.y, row, item.shape, w, line.height));
      left += w + ZONE_GAP;
    }
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
  decor.push({ key: 'printer', kind: 'printer', x: lounge.x + lounge.w + 70, y: bottomTop + 106, variant: 0 });
  decor.push({ key: 'lounge-corner', kind: 'tall-plant', x: MARGIN + 18, y: bottomTop + BOTTOM_H - 20, variant: 1 });
  decor.push({ key: 'lounge', kind: 'plant', x: lounge.x + lounge.w - 12, y: lounge.y + 26, variant: 2 });
  for (const zone of zones) {
    if (zone.x + zone.w + ZONE_GAP / 2 < width - MARGIN) {
      decor.push({ key: `beside:${zone.key}`, kind: 'plant', x: zone.x + zone.w + ZONE_GAP / 2, y: zone.y + zone.h - 8, variant: stableHash(zone.key) % 3 });
    }
  }
  return { width, height, wall: WALL, owner, zones, watchdog, entrance, lounge, bar, spine: MARGIN / 2, corridors, decor };
}

/** The walkway along a zone's left edge, inside its rug. */
export function aisleOf(zone: ZoneLayout): number {
  return zone.x + 8;
}

/** The corridor under a zone's row (every zone in a justified row has the row's height). */
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

