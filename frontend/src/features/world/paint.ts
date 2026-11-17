/** Tofu office art: Canvas2D painters for the floor under the people. Everything here draws at the positions it
 * is given, so the scene can redraw the floor every frame while rooms slide (tween.ts) and the floor and its
 * people always agree. Procedural surfaces: a seamless plank tile (filled as a pattern, so the floor has no
 * size) and the window wall with skies by local hour; team rooms and the light are diorama.ts. Furniture is
 * stamped from the theme's art atlas (assets/manifest.generated.ts PROPS: rect, size, anchor); a pod's desks
 * and chairs are painted once into a layer that moves as one piece. Clutter is seeded by desk identity, so a
 * desk keeps its mug wherever its room moves.
 * Pure drawing, no DOM or state. Entry points: paintFloorTile, drawFloor, drawWall, windowSlots, paintWindow,
 * paintPod, drawFurniture, stamp, drawInside. */
import { podDivider, type DecorItem, type OfficeLayout, type Pod, type Rect, type Seat } from './layout';

export type Ctx = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;
export type ArtTheme = 'light' | 'dark';
export interface AtlasProp {
  readonly rect: readonly number[]; readonly size: readonly number[]; readonly anchor: readonly number[];
  readonly slots?: Readonly<Record<string, readonly number[]>>;
}
export interface Atlas { image: CanvasImageSource; props: Readonly<Record<string, AtlasProp>> }

/** Procedural surfaces per theme; the props carry their own night colours in the dark atlas. */
export const PALETTE = {
  light: {
    wood: '#d4a873', woodAlt: '#cb9d66', woodLight: '#dbb27f', seam: 'rgba(122, 78, 38, 0.30)', grain: 'rgba(120, 72, 32, 0.07)', plankLight: 'rgba(255, 240, 214, 0.22)',
    wall: '#efe5d3', wallLine: 'rgba(160, 128, 88, 0.10)', rail: '#9b6a42', railLight: '#b5835a', skirting: '#6e4a2f', side: '#4a3324',
    rugBase: '#efe7da', rugEdge: '#4a3a2c', rugLine: '#ffffff', shade: '58, 34, 14', glow: '255, 236, 196', daylight: true,
  },
  dark: {
    wood: '#4b3528', woodAlt: '#463125', woodLight: '#513a2c', seam: 'rgba(16, 9, 5, 0.45)', grain: 'rgba(0, 0, 0, 0.10)', plankLight: 'rgba(255, 220, 180, 0.05)',
    wall: '#2b3045', wallLine: 'rgba(255, 255, 255, 0.03)', rail: '#3e2a1d', railLight: '#55392a', skirting: '#1c130d', side: '#15100c',
    rugBase: '#3a312c', rugEdge: '#1e1712', rugLine: '#d8c3a0', shade: '0, 0, 0', glow: '255, 196, 120', daylight: false,
  },
} as const;
export type Palette = typeof PALETTE[ArtTheme];

/** The plank tile: 16 rows of 18-unit boards, long enough that its repeat is not read as a pattern. */
export const FLOOR_TILE = { w: 960, h: 288, plank: 18 } as const;
/** A pod layer reaches this far past the pod's box: chairs and desk fronts overhang it. */
export const POD_BLEED = 24;
const CLUTTER = ['clutter-mug', 'clutter-papers', 'clutter-books', 'clutter-mini-plant'] as const;

export function seeded(seed: number) {
  let state = seed >>> 0 || 1;
  return () => {
    state = Math.imul(state ^ (state >>> 15), 2246822507) ^ Math.imul(state ^ (state >>> 13), 3266489909);
    return ((state ^= state >>> 16) >>> 0) / 4294967296;
  };
}
export function rr(ctx: Ctx, x: number, y: number, w: number, h: number, r: number) {
  const k = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath(); ctx.moveTo(x + k, y); ctx.arcTo(x + w, y, x + w, y + h, k); ctx.arcTo(x + w, y + h, x, y + h, k);
  ctx.arcTo(x, y + h, x, y, k); ctx.arcTo(x, y, x + w, y, k); ctx.closePath();
}
function hexRgb(hex: string): [number, number, number] {
  const value = parseInt(hex.replace('#', '').padEnd(6, '0').slice(0, 6), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}
export function mix(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexRgb(a); const [br, bg, bb] = hexRgb(b);
  const c = (x: number, y: number) => Math.round(x + (y - x) * t).toString(16).padStart(2, '0');
  return `#${c(ar, br)}${c(ag, bg)}${c(ab, bb)}`;
}

/** Draws prop `name` with its anchor on (x, y), in scene units. */
export function stamp(ctx: Ctx, atlas: Atlas, name: string, x: number, y: number) {
  const prop = atlas.props[name];
  if (!prop) return;
  const [sx, sy, sw, sh] = prop.rect;
  ctx.drawImage(atlas.image, sx, sy, sw, sh, x - prop.anchor[0], y - prop.anchor[1], prop.size[0], prop.size[1]);
}

/** Long boards in a staggered bond on a tile that repeats seamlessly: every board, seam and grain line is drawn
 * again one tile to either side, so a board crossing the tile edge continues on the other. */
export function paintFloorTile(ctx: Ctx, palette: Palette) {
  const { w, h, plank } = FLOOR_TILE;
  const random = seeded(41);
  const wrapped = (x: number, y: number, width: number, height: number) => {
    for (const shift of [-w, 0, w]) ctx.fillRect(x + shift, y, width, height);
  };
  ctx.fillStyle = palette.wood; ctx.fillRect(0, 0, w, h);
  for (let y = 0; y < h; y += plank) {
    let x = -random() * 200;
    const end = x + w;
    while (x < end) {
      const length = Math.min(150 + random() * 170, end - x);
      const tone = random();
      ctx.fillStyle = tone < 0.5 ? mix(palette.wood, palette.woodAlt, tone * 2) : mix(palette.wood, palette.woodLight, (tone - 0.5) * 2);
      wrapped(x, y, length, plank);
      ctx.fillStyle = palette.grain;
      for (let g = 0; g < 2 && length > 60; g += 1) wrapped(x + 10 + random() * (length - 50), y + 4 + random() * (plank - 8), 24 + random() * 30, 1);
      ctx.fillStyle = palette.seam; wrapped(x + length - 1, y + 1, 1, plank - 2);
      x += length;
    }
    ctx.fillStyle = palette.seam; ctx.fillRect(0, y + plank - 1, w, 1);
    ctx.fillStyle = palette.plankLight; ctx.fillRect(0, y, w, 1);
  }
}

/** The floor below the wall, filled with the plank pattern (already scaled to scene units). */
export function drawFloor(ctx: Ctx, layout: OfficeLayout, planks: CanvasPattern | null, palette: Palette) {
  const { width, height, wall } = layout;
  ctx.fillStyle = planks ?? palette.wood;
  ctx.fillRect(0, wall, width, height - wall);
}

/** Where the window wall's glass panes go for a floor this wide (the window prop's glass slot). */
export function windowSlots(width: number, glass: readonly number[] = [0, 0, 196, 44]): Rect[] {
  const count = Math.max(3, Math.floor((width - 80) / 300));
  const span = (width - 80) / count;
  return Array.from({ length: count }, (_, index) => ({ x: 40 + span * index + (span - glass[2]) / 2, y: 10, w: glass[2], h: glass[3] }));
}

/** The wall itself, the rails and the side walls; the windows are layers drawn on top. */
export function drawWall(ctx: Ctx, layout: OfficeLayout, palette: Palette) {
  const { width, height, wall } = layout;
  ctx.fillStyle = palette.wall; ctx.fillRect(0, 0, width, wall);
  ctx.fillStyle = palette.wallLine; for (let x = 0; x < width; x += 24) ctx.fillRect(x, 0, 1, wall - 22);
  ctx.fillStyle = palette.rail; ctx.fillRect(0, wall - 22, width, 16);
  ctx.fillStyle = palette.railLight; ctx.fillRect(0, wall - 22, width, 3);
  ctx.fillStyle = palette.skirting; ctx.fillRect(0, wall - 6, width, 6);
  ctx.fillStyle = palette.side; ctx.fillRect(0, 0, 12, height); ctx.fillRect(width - 12, 0, 12, height);
  ctx.fillRect(0, height - 10, width, 10);
}

function sky(ctx: Ctx, x: number, y: number, w: number, h: number, hour: number, seed: number) {
  const night = hour < 6 || hour >= 19;
  const dusk = (hour >= 17 && hour < 19) || (hour >= 6 && hour < 8);
  ctx.save();
  ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
  const gradient = ctx.createLinearGradient(0, y, 0, y + h);
  if (night) { gradient.addColorStop(0, '#101736'); gradient.addColorStop(1, '#2b2c55'); }
  else if (dusk) { gradient.addColorStop(0, '#6f86c9'); gradient.addColorStop(0.6, '#f0a77a'); gradient.addColorStop(1, '#f7cf8e'); }
  else { gradient.addColorStop(0, '#7ec2ea'); gradient.addColorStop(1, '#cfeaf6'); }
  ctx.fillStyle = gradient; ctx.fillRect(x, y, w, h);
  const random = seeded(seed);
  if (night) {
    for (let i = 0; i < w / 9; i += 1) { ctx.fillStyle = `rgba(255,250,230,${0.3 + random() * 0.6})`; ctx.fillRect(x + random() * w, y + random() * h * 0.5, 1.2, 1.2); }
  } else {
    ctx.fillStyle = 'rgba(255,255,255,0.75)';
    for (let i = 0; i < 2; i += 1) {
      const cx = x + random() * w, cy = y + 8 + random() * h * 0.3;
      for (let k = 0; k < 4; k += 1) { ctx.beginPath(); ctx.ellipse(cx + k * 7, cy + (k % 2) * 2, 8, 4, 0, 0, Math.PI * 2); ctx.fill(); }
    }
  }
  for (const [tone, base] of [[night ? '#1b2142' : '#9db6c9', 0.42], [night ? '#252c55' : '#86a1b6', 0.2]] as const) {
    ctx.fillStyle = tone;
    for (let bx = x; bx < x + w;) {
      const bw = 10 + random() * 22, bh = h * (base + random() * 0.3);
      ctx.fillRect(bx, y + h - bh, bw, bh);
      if (night) { ctx.fillStyle = 'rgba(255,214,140,0.55)'; for (let k = 0; k < 4; k += 1) if (random() < 0.5) ctx.fillRect(bx + 2 + random() * (bw - 4), y + h - bh + 3 + random() * (bh - 6), 2, 2); ctx.fillStyle = tone; }
      bx += bw + 2 + random() * 6;
    }
  }
  ctx.restore();
}

/** One window into a layer whose origin is the window prop's top-left: the sky by local hour behind its glass,
 * then the frame. `index` seeds the skyline, so each pane keeps its own view. */
export function paintWindow(ctx: Ctx, atlas: Atlas, hour: number, index: number) {
  const prop = atlas.props.window;
  if (!prop) return;
  const glass = prop.slots?.glass ?? [0, 0, 196, 44];
  sky(ctx, prop.anchor[0] + glass[0], prop.anchor[1] + glass[1], glass[2], glass[3], hour, 97 + index * 31);
  stamp(ctx, atlas, 'window', prop.anchor[0], prop.anchor[1]);
}

/** One pod, back to front, into a layer whose origin is (pod.x - POD_BLEED, pod.y - POD_BLEED): chairs behind
 * the top row, its desks, the partition, the bottom row's desks, then the chairs in front of them. `clutter`
 * picks a desk's item (index into the clutter props, or none) from the desk's identity. */
export function paintPod(ctx: Ctx, atlas: Atlas, pod: Pod, seats: ReadonlyArray<Seat>, clutter: (seat: number) => number) {
  ctx.save();
  ctx.translate(POD_BLEED - pod.x, POD_BLEED - pod.y);
  const top = seats.map((seat, index) => ({ seat, index })).filter(item => item.seat.facing === 'down');
  const bottom = seats.map((seat, index) => ({ seat, index })).filter(item => item.seat.facing === 'up');
  const place = (item: { seat: Seat; index: number }, prop: 'desk-down' | 'desk-up') => {
    stamp(ctx, atlas, prop, item.seat.desk.x, item.seat.desk.y);
    const slot = atlas.props[prop]?.slots?.clutter;
    const pick = clutter(item.index);
    if (slot && pick < CLUTTER.length) stamp(ctx, atlas, CLUTTER[pick], item.seat.desk.x + slot[0], item.seat.desk.y + slot[1]);
  };
  for (const item of top) stamp(ctx, atlas, 'chair-down', item.seat.x, item.seat.y);
  for (const item of top) place(item, 'desk-down');
  stamp(ctx, atlas, 'pod-divider', pod.x + 6, podDivider(pod));
  for (const item of bottom) place(item, 'desk-up');
  for (const item of bottom) stamp(ctx, atlas, 'chair-up', item.seat.x, item.seat.y);
  ctx.restore();
}
export const CLUTTER_CHOICES = CLUTTER.length + 1;

/** Furniture that is not part of a pod: the owner's office with its whiteboard and trophy shelf, each zone's
 * head desk, the Watchdog's station, the lounge, bar and entrance, and the plants and appliances. `alpha`
 * fades items that are appearing or leaving in a layout transition. */
export function drawFurniture(ctx: Ctx, atlas: Atlas, layout: OfficeLayout, routerZones: ReadonlySet<string>,
  zoneAlpha: (key: string) => number, decorAlpha: (key: string) => number, watchdogAlpha: number) {
  const { room, whiteboard, shelf } = layout.owner;
  stamp(ctx, atlas, 'owner-office', room.x, room.y);
  stamp(ctx, atlas, 'whiteboard', whiteboard.x, whiteboard.y);
  stamp(ctx, atlas, 'trophy-shelf', shelf.x, shelf.y);
  const base = ctx.globalAlpha;
  for (const zone of layout.zones) {
    ctx.globalAlpha = base * zoneAlpha(zone.key);
    stamp(ctx, atlas, 'chair-down', zone.lead.x, zone.lead.y);
    stamp(ctx, atlas, routerZones.has(zone.key) ? 'router-desk' : 'lead-desk', zone.lead.desk.x, zone.lead.desk.y);
  }
  if (layout.watchdog) {
    ctx.globalAlpha = base * watchdogAlpha;
    stamp(ctx, atlas, 'watchdog-station', layout.watchdog.station.x, layout.watchdog.station.y);
  }
  ctx.globalAlpha = base;
  stamp(ctx, atlas, 'meeting-area', layout.lounge.x, layout.lounge.y);
  stamp(ctx, atlas, 'coffee-bar', layout.bar.x, layout.bar.y);
  stamp(ctx, atlas, 'entrance', layout.entrance.x, layout.entrance.y);
  layout.decor.forEach((item: DecorItem) => {
    ctx.globalAlpha = base * decorAlpha(item.key);
    const variant = (item.variant % 3) + 1;
    if (item.kind === 'plant') stamp(ctx, atlas, `plant-${variant}`, item.x, item.y);
    else if (item.kind === 'tall-plant') stamp(ctx, atlas, `tall-plant-${variant}`, item.x, item.y);
    else if (item.kind === 'cooler' || item.kind === 'printer') stamp(ctx, atlas, item.kind, item.x, item.y);
  });
  ctx.globalAlpha = base;
}

/** Draws source box (sx, sy, sw, sh) of `image` to destination box (dx, dy, dw, dh), only the part inside `box` when
 * given: the source is cropped to match, so this clips to a rectangle without canvas clip state (a sprite that
 * saves, clips and restores the context costs a software-rasterized canvas a mask on every frame). */
export function drawInside(ctx: Ctx, image: CanvasImageSource, sx: number, sy: number, sw: number, sh: number,
  dx: number, dy: number, dw: number, dh: number, box: Rect | null) {
  if (!box) { ctx.drawImage(image, sx, sy, sw, sh, dx, dy, dw, dh); return; }
  const left = Math.max(dx, box.x), top = Math.max(dy, box.y);
  const right = Math.min(dx + dw, box.x + box.w), bottom = Math.min(dy + dh, box.y + box.h);
  if (right <= left || bottom <= top) return;
  const kx = sw / dw, ky = sh / dh;
  ctx.drawImage(image, sx + (left - dx) * kx, sy + (top - dy) * ky, (right - left) * kx, (bottom - top) * ky, left, top, right - left, bottom - top);
}
