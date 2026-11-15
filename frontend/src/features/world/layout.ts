/** Tofu office floor plan: pure geometry in scene units (CSS pixels at scale 1).
 * The owner's glass office sits top-left under the window wall; team zones fill rows to its right and below
 * (one zone per team: a banner, the lead's head desk, then pods of two facing desk rows); the bottom strip
 * holds the entrance with the Watchdog's station and the lounge. Walkways: each zone's left aisle, one
 * corridor under every row and the spine along the left wall, so every desk reaches the owner's door.
 * Seats keep their slot while their person stays (allocateSlots), so the office never reshuffles.
 * Entry points: layoutOffice, allocateSlots, walkToOwner, walkHome, walkOut, nearestInDirection. */

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
export interface DecorItem { kind: 'plant' | 'tall-plant' | 'shelf' | 'cooler' | 'printer'; x: number; y: number; variant: number }
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

/** The largest scale at which the whole office still fits the window height, between sceneScale and the scale
 * that lays it out exactly MIN_WIDTH units wide; a short office is drawn bigger instead of leaving empty floor.
 * The search stops when one more step would move the right edge by less than a pixel. */
export function fitOffice(viewportWidth: number, viewportHeight: number, teams: ReadonlyArray<TeamShape>): { scale: number; layout: OfficeLayout } {
  const low = sceneScale(viewportWidth);
  const high = viewportWidth / MIN_WIDTH;
  const fits = (scale: number) => layoutOffice(viewportWidth, teams, 0, scale).height * scale <= viewportHeight;
  let best = low;
  if (viewportHeight && high > low && fits(low)) {
    if (fits(high)) best = high;
    else {
      let tooBig = high;
      while ((tooBig - best) * viewportWidth > 1) {
        const middle = (best + tooBig) / 2;
        if (fits(middle)) best = middle; else tooBig = middle;
      }
    }
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
function zoneShape(shape: TeamShape, width: number) {
  const pods = Math.ceil(shape.seats / SEATS_PER_POD);
  if (!pods) return { pods, columns: 1, w: IDLE_ZONE_W, h: HEAD_H + ZONE_PAD };
  const fit = Math.max(1, Math.floor((width - ZONE_PAD * 2 + POD_GAP) / (POD_W + POD_GAP)));
  const columns = Math.min(fit, pods <= 2 ? pods : Math.ceil(Math.sqrt(pods)));
  const rows = Math.ceil(pods / columns);
  return {
    pods, columns,
    w: ZONE_PAD * 2 + columns * POD_W + (columns - 1) * POD_GAP,
    h: HEAD_H + rows * POD_H + (rows - 1) * POD_GAP / 2 + ZONE_PAD,
  };
}

function buildZone(key: string, x: number, y: number, row: number, shape: ReturnType<typeof zoneShape>): ZoneLayout {
  const pods: Pod[] = [];
  for (let index = 0; index < shape.pods; index += 1) {
    const column = index % shape.columns;
    const line = Math.floor(index / shape.columns);
    pods.push({ x: x + ZONE_PAD + column * (POD_W + POD_GAP), y: y + HEAD_H + line * (POD_H + POD_GAP / 2),
      w: POD_W, h: POD_H, columns: POD_COLUMNS });
  }
  const leadX = x + shape.w / 2;
  const leadDesk = { x: leadX - 46, y: y + 92, w: 92, h: 32 };
  const lead: Seat = {
    x: leadX, y: y + 78, facing: 'down', cut: leadDesk.y + 3, stand: { x: leadX, y: y + HEAD_H - 4 },
    desk: leadDesk, screen: { x: leadX + 14, y: leadDesk.y + 2, w: 20, h: 10 },
  };
  return {
    key, x, y, w: shape.w, h: shape.h, row, banner: { x: x + ZONE_PAD, y: y + 12 }, lead, pods,
    seats: pods.flatMap(podSeats), routine: { x: x + shape.w - ZONE_PAD, y: y + 16 },
  };
}

/** `viewportHeight` (CSS px) makes the floor reach the bottom of the window; the lounge, bar, entrance and
 * Watchdog station sit along that bottom edge. */
export function layoutOffice(viewportWidth: number, teams: ReadonlyArray<TeamShape>, viewportHeight = 0,
  scale = sceneScale(viewportWidth)): OfficeLayout {
  const width = Math.max(MIN_WIDTH, Math.round(viewportWidth / scale));
  const inner = width - MARGIN * 2;
  const floorTop = WALL + 22;
  const zones: ZoneLayout[] = [];
  const rowBottoms: number[] = [];
  let row = 0;
  let x = MARGIN + OFFICE_W + ZONE_GAP;
  let y = floorTop;
  let rowHeight = OFFICE_H;
  const placed = teams.filter(team => team.kind !== 'watchdog');
  for (const team of placed) {
    const shape = zoneShape(team, inner);
    if (x > MARGIN && x + shape.w > width - MARGIN) {
      rowBottoms.push(y + rowHeight);
      row += 1; y += rowHeight + CORRIDOR; x = MARGIN; rowHeight = 0;
    }
    zones.push(buildZone(team.key, x, y, row, shape));
    x += shape.w + ZONE_GAP;
    rowHeight = Math.max(rowHeight, shape.h);
  }
  rowBottoms.push(y + rowHeight);
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
    if (at > MARGIN + OFFICE_W + 20 && at < width - MARGIN - 20) decor.push({ kind: 'tall-plant', x: at, y: floorTop - 6, variant: index % 3 });
  }
  decor.push({ kind: 'cooler', x: width - MARGIN - 24, y: bottomTop + 120, variant: 0 });
  decor.push({ kind: 'printer', x: lounge.x + lounge.w + 70, y: bottomTop + 106, variant: 0 });
  decor.push({ kind: 'tall-plant', x: MARGIN + 18, y: bottomTop + BOTTOM_H - 20, variant: 1 });
  decor.push({ kind: 'plant', x: lounge.x + lounge.w - 12, y: lounge.y + 26, variant: 2 });
  for (const zone of zones) {
    if (zone.x + zone.w + ZONE_GAP / 2 < width - MARGIN) decor.push({ kind: 'plant', x: zone.x + zone.w + ZONE_GAP / 2, y: zone.y + zone.h - 8, variant: zone.row % 3 });
  }
  return { width, height, wall: WALL, owner, zones, watchdog, entrance, lounge, bar, spine: MARGIN / 2, corridors, decor };
}

function zoneOf(layout: OfficeLayout, seat: Seat): ZoneLayout | null {
  return layout.zones.find(zone => zone.lead === seat || zone.seats.includes(seat)) || null;
}

/** From a desk to the spot in front of the owner's desk, along aisles and corridors. */
export function walkToOwner(layout: OfficeLayout, seat: Seat): Point[] {
  const path: Point[] = [{ x: seat.x, y: seat.y }, seat.stand];
  const lane = layout.corridors[0];
  const { door, drop } = layout.owner;
  const zone = zoneOf(layout, seat);
  if (zone) {
    const aisle = zone.x + 8;
    const corridor = layout.corridors[zone.row];
    path.push({ x: aisle, y: seat.stand.y }, { x: aisle, y: corridor });
    if (zone.row > 0) path.push({ x: layout.spine, y: corridor }, { x: layout.spine, y: lane });
  } else {
    const corridor = layout.corridors[layout.corridors.length - 1] + 70;
    path.push({ x: seat.stand.x, y: corridor }, { x: layout.spine, y: corridor }, { x: layout.spine, y: lane });
  }
  path.push({ x: door.x, y: lane }, { x: door.x, y: drop.y }, { x: drop.x, y: drop.y });
  return compact(path);
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

export function walkHome(layout: OfficeLayout, seat: Seat): Point[] {
  return walkToOwner(layout, seat).reverse();
}

/** From the owner's desk out through the entrance: a Worker whose work is done leaves the floor. */
export function walkOut(layout: OfficeLayout): Point[] {
  const { door, drop } = layout.owner;
  const lane = layout.corridors[0];
  const bottom = layout.corridors[layout.corridors.length - 1] + 70;
  return compact([{ x: drop.x, y: drop.y }, { x: door.x, y: drop.y }, { x: door.x, y: lane }, { x: layout.spine, y: lane },
    { x: layout.spine, y: bottom }, { x: layout.entrance.x, y: bottom }, { x: layout.entrance.x, y: layout.entrance.y + 30 }]);
}

function compact(points: Point[]): Point[] {
  const out: Point[] = [];
  for (const point of points) {
    const last = out[out.length - 1];
    if (!last || Math.abs(last.x - point.x) > 0.5 || Math.abs(last.y - point.y) > 0.5) out.push(point);
  }
  return out;
}

export function pathLength(points: ReadonlyArray<Point>): number {
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    total += Math.abs(points[index].x - points[index - 1].x) + Math.abs(points[index].y - points[index - 1].y);
  }
  return total;
}

export type NavDirection = 'left' | 'right' | 'up' | 'down';
/** Keyboard movement between people: the nearest target inside the pressed direction's 90° cone, or, when the
 * cone is empty, the nearest one anywhere on that side. */
export function nearestInDirection<T extends Point>(from: Point, targets: ReadonlyArray<T>, direction: NavDirection): T | null {
  let best: T | null = null;
  let bestScore = Infinity;
  let bestInCone = false;
  for (const target of targets) {
    const dx = target.x - from.x;
    const dy = target.y - from.y;
    const along = direction === 'left' ? -dx : direction === 'right' ? dx : direction === 'up' ? -dy : dy;
    const across = direction === 'left' || direction === 'right' ? Math.abs(dy) : Math.abs(dx);
    if (along <= 1) continue;
    const inCone = along >= across;
    const score = Math.hypot(along, across);
    if ((inCone && !bestInCone) || (inCone === bestInCone && score < bestScore)) { best = target; bestScore = score; bestInCone = inCone; }
  }
  return best;
}
