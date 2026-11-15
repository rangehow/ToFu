/** Lead office tower: pure geometry, no DOM or art.
 * A side-view cutaway tower, TOWER_WIDTH units wide (one unit is about one CSS
 * pixel in a sidebar). Room 0 is the lead's penthouse office; every other room
 * is one floor below it. Every room is a cross-section: a slab on top, the back
 * wall, and the floor strip whose feet line (`feet`) is where people stand. The
 * elevator shaft (SHAFT) is the tower's spine down the right edge: hand-offs
 * travel down it and people arrive through its door. Rooms stack top to bottom,
 * so the tower grows with the team while its width never changes.
 * Entry points: officeTower, roomRect, roomAt, plus the station constants the
 * scene places people at.
 */
export const TOWER_WIDTH = 320;
export const OFFICE_HEIGHT = 184;
export const FLOOR_HEIGHT = 96;
/** The slab between rooms, at the top of each room. */
export const SLAB = 5;
/** Depth of the floor strip; people's feet stand FLOOR_FEET above a room's bottom. */
export const FLOOR_STRIP = 16;
export const FLOOR_FEET = 10;
/** The elevator spine on the right edge; its door opens onto every room. */
export const SHAFT = { x: 294, width: 26, door: { x: 297, width: 20, height: 40 } } as const;
/** A floor's name card on its bare left wall. */
export const FLOOR_CARD = { x: 8, y: 10, width: 168, height: 58 } as const;
/** A Worker floor's stations (x along the floor, room units). */
export const FLOOR_SPOTS = { chair: 224, desk: 234, deskWidth: 52, beside: 196, door: 282 } as const;
/** The lead office's stations. */
export const LEAD_SPOTS = {
  board: { x: 16, y: 66, width: 78, height: 70 },
  stand: 106,
  chair: 156, desk: 166, deskWidth: 64,
  window: { x: 120, y: 22, width: 118, height: 70 },
  tray: { x: 246, width: 34 },
  front: 262,
  door: 282,
} as const;

export interface OfficeTower { rooms: number; width: number; height: number; }
export interface RoomRect { x: number; y: number; width: number; height: number; }

export function officeTower(rooms: number): OfficeTower {
  const count = Math.max(1, Math.floor(rooms) || 1);
  return { rooms: count, width: TOWER_WIDTH, height: OFFICE_HEIGHT + (count - 1) * FLOOR_HEIGHT };
}

export function roomRect(index: number): RoomRect {
  return index === 0 ? { x: 0, y: 0, width: TOWER_WIDTH, height: OFFICE_HEIGHT }
    : { x: 0, y: OFFICE_HEIGHT + (index - 1) * FLOOR_HEIGHT, width: TOWER_WIDTH, height: FLOOR_HEIGHT };
}

/** The feet line of a room, in room units. */
export const feetOf = (height: number) => height - FLOOR_FEET;

/** Room index under a world point, or -1 outside the tower. */
export function roomAt(point: { x: number; y: number }, tower: OfficeTower): number {
  if (!(point.x >= 0 && point.y >= 0 && point.x < tower.width && point.y < tower.height)) return -1;
  if (point.y < OFFICE_HEIGHT) return 0;
  return Math.min(tower.rooms - 1, 1 + Math.floor((point.y - OFFICE_HEIGHT) / FLOOR_HEIGHT));
}
