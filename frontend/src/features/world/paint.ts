/** Tofu office art: the Canvas2D painter for the static floor layer. Floors, walls, window skies (by local hour)
 * and team rugs (team colours) are procedural; every piece of furniture is stamped from the theme's art atlas
 * (assets/manifest.generated.ts PROPS: atlas rect, size and anchor) at the geometry layout.ts computes, which
 * stays authoritative. Pure drawing over a layout; no DOM, so it runs in art-worker.ts on an OffscreenCanvas.
 * Entry point: paintOffice. */
import { podDivider, type DecorItem, type OfficeLayout, type Seat, type ZoneLayout } from './layout';

type Ctx = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;
export type ArtTheme = 'light' | 'dark';
export interface PaintZone { key: string; color: string; kind: 'router' | 'project' | 'watchdog' }
export interface PaintInput { layout: OfficeLayout; zones: ReadonlyArray<PaintZone>; hour: number; theme: ArtTheme }
export interface AtlasProp {
  readonly rect: readonly number[]; readonly size: readonly number[]; readonly anchor: readonly number[];
  readonly slots?: Readonly<Record<string, readonly number[]>>;
}
export interface Atlas { image: CanvasImageSource; props: Readonly<Record<string, AtlasProp>> }

/** Procedural surfaces per theme; the props carry their own night colours in the dark atlas. */
const PALETTE = {
  light: {
    wood: '#d4a873', woodAlt: '#cb9d66', woodLight: '#dbb27f', seam: 'rgba(122, 78, 38, 0.30)', grain: 'rgba(120, 72, 32, 0.07)', plankLight: 'rgba(255, 240, 214, 0.22)',
    wall: '#efe5d3', wallLine: 'rgba(160, 128, 88, 0.10)', rail: '#9b6a42', railLight: '#b5835a', skirting: '#6e4a2f', side: '#4a3324',
    rugBase: '#efe7da', rugEdge: '#4a3a2c', rugLine: '#ffffff', shade: '58, 34, 14', glow: '255, 236, 196', ambient: 'rgba(0, 0, 0, 0)', daylight: true,
  },
  dark: {
    wood: '#4b3528', woodAlt: '#463125', woodLight: '#513a2c', seam: 'rgba(16, 9, 5, 0.45)', grain: 'rgba(0, 0, 0, 0.10)', plankLight: 'rgba(255, 220, 180, 0.05)',
    wall: '#2b3045', wallLine: 'rgba(255, 255, 255, 0.03)', rail: '#3e2a1d', railLight: '#55392a', skirting: '#1c130d', side: '#15100c',
    rugBase: '#3a312c', rugEdge: '#1e1712', rugLine: '#d8c3a0', shade: '0, 0, 0', glow: '255, 196, 120', ambient: 'rgba(14, 18, 44, 0.20)', daylight: false,
  },
} as const;
type Palette = typeof PALETTE[ArtTheme];

function seeded(seed: number) {
  let state = seed >>> 0 || 1;
  return () => {
    state = Math.imul(state ^ (state >>> 15), 2246822507) ^ Math.imul(state ^ (state >>> 13), 3266489909);
    return ((state ^= state >>> 16) >>> 0) / 4294967296;
  };
}
function rr(ctx: Ctx, x: number, y: number, w: number, h: number, r: number) {
  const k = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath(); ctx.moveTo(x + k, y); ctx.arcTo(x + w, y, x + w, y + h, k); ctx.arcTo(x + w, y + h, x, y + h, k);
  ctx.arcTo(x, y + h, x, y, k); ctx.arcTo(x, y, x + w, y, k); ctx.closePath();
}
function fillRR(ctx: Ctx, x: number, y: number, w: number, h: number, r: number, color: string) { rr(ctx, x, y, w, h, r); ctx.fillStyle = color; ctx.fill(); }
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
function stamp(ctx: Ctx, atlas: Atlas, name: string, x: number, y: number) {
  const prop = atlas.props[name];
  if (!prop) return;
  const [sx, sy, sw, sh] = prop.rect;
  ctx.drawImage(atlas.image, sx, sy, sw, sh, x - prop.anchor[0], y - prop.anchor[1], prop.size[0], prop.size[1]);
}

/** Long boards in a staggered bond: neighbouring boards differ only slightly in tone, seams are soft, and a
 * thin top highlight gives each board a bevel, so the floor reads as one calm surface under the furniture. */
function floor(ctx: Ctx, layout: OfficeLayout, palette: Palette) {
  const { width, height, wall } = layout;
  ctx.fillStyle = palette.wood; ctx.fillRect(0, wall, width, height - wall);
  const random = seeded(41);
  const plank = 18;
  for (let y = wall; y < height; y += plank) {
    let x = -random() * 200;
    while (x < width) {
      const length = 150 + random() * 170;
      const tone = random();
      ctx.fillStyle = tone < 0.5 ? mix(palette.wood, palette.woodAlt, tone * 2) : mix(palette.wood, palette.woodLight, (tone - 0.5) * 2);
      ctx.fillRect(x, y, length, plank);
      ctx.fillStyle = palette.grain;
      for (let g = 0; g < 2; g += 1) ctx.fillRect(x + 10 + random() * (length - 50), y + 4 + random() * (plank - 8), 24 + random() * 30, 1);
      ctx.fillStyle = palette.seam; ctx.fillRect(x + length - 1, y + 1, 1, plank - 2);
      x += length;
    }
    ctx.fillStyle = palette.seam; ctx.fillRect(0, y + plank - 1, width, 1);
    ctx.fillStyle = palette.plankLight; ctx.fillRect(0, y, width, 1);
  }
}

const rgba = (rgb: string, alpha: number) => `rgba(${rgb}, ${alpha})`;

/** Ambient light over the finished floor: daylight falls in from the window wall, the walls cast a soft
 * contact shade, the edges of the room fall off (vignette); at night the room cools and every lead's
 * desk and the owner's desk keep a warm pool of lamp light. */
function lighting(ctx: Ctx, layout: OfficeLayout, palette: Palette, night: boolean) {
  const { width, height, wall } = layout;
  const day = ctx.createLinearGradient(0, wall, 0, wall + 320);
  day.addColorStop(0, rgba(palette.glow, night ? 0.04 : 0.20)); day.addColorStop(1, rgba(palette.glow, 0));
  ctx.fillStyle = day; ctx.fillRect(0, wall, width, 320);
  const shade = ctx.createLinearGradient(0, wall, 0, wall + 30);
  shade.addColorStop(0, rgba(palette.shade, 0.28)); shade.addColorStop(1, rgba(palette.shade, 0));
  ctx.fillStyle = shade; ctx.fillRect(0, wall, width, 30);
  for (const [from, to] of [[12, 40], [width - 12, width - 40]]) {
    const side = ctx.createLinearGradient(from, 0, to, 0);
    side.addColorStop(0, rgba(palette.shade, 0.22)); side.addColorStop(1, rgba(palette.shade, 0));
    ctx.fillStyle = side; ctx.fillRect(Math.min(from, to), wall, 28, height - wall);
  }
  const radius = Math.hypot(width, height) / 2;
  const vignette = ctx.createRadialGradient(width / 2, height * 0.45, radius * 0.45, width / 2, height * 0.45, radius);
  vignette.addColorStop(0, rgba(palette.shade, 0)); vignette.addColorStop(1, rgba(palette.shade, palette.daylight ? 0.20 : 0.42));
  ctx.fillStyle = vignette; ctx.fillRect(0, 0, width, height);
  if (!night) return;
  ctx.fillStyle = palette.daylight ? 'rgba(20, 24, 56, 0.14)' : palette.ambient; ctx.fillRect(0, 0, width, height);
  const lamps = [layout.owner.desk, ...layout.zones.map(zone => zone.lead.desk)];
  if (layout.watchdog) lamps.push(layout.watchdog.seat.desk);
  for (const desk of lamps) {
    const x = desk.x + desk.w / 2, y = desk.y + desk.h / 2;
    const pool = ctx.createRadialGradient(x, y, 10, x, y, 190);
    pool.addColorStop(0, rgba(palette.glow, palette.daylight ? 0.16 : 0.12)); pool.addColorStop(1, rgba(palette.glow, 0));
    ctx.fillStyle = pool; ctx.fillRect(x - 190, y - 190, 380, 380);
  }
}

function sky(ctx: Ctx, x: number, y: number, w: number, h: number, hour: number, seed: number) {
  const night = hour < 6 || hour >= 19;
  const dusk = (hour >= 17 && hour < 19) || (hour >= 6 && hour < 8);
  const gradient = ctx.createLinearGradient(0, y, 0, y + h);
  if (night) { gradient.addColorStop(0, '#101736'); gradient.addColorStop(1, '#2b2c55'); }
  else if (dusk) { gradient.addColorStop(0, '#6f86c9'); gradient.addColorStop(0.6, '#f0a77a'); gradient.addColorStop(1, '#f7cf8e'); }
  else { gradient.addColorStop(0, '#7ec2ea'); gradient.addColorStop(1, '#cfeaf6'); }
  ctx.save();
  ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
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

/** The window wall: procedural skies by local hour inside the atlas window frames (their glass is clear). */
function backWall(ctx: Ctx, layout: OfficeLayout, hour: number, palette: Palette, atlas: Atlas | null) {
  const { width, wall } = layout;
  ctx.fillStyle = palette.wall; ctx.fillRect(0, 0, width, wall);
  ctx.fillStyle = palette.wallLine; for (let x = 0; x < width; x += 24) ctx.fillRect(x, 0, 1, wall - 22);
  ctx.fillStyle = palette.rail; ctx.fillRect(0, wall - 22, width, 16);
  ctx.fillStyle = palette.railLight; ctx.fillRect(0, wall - 22, width, 3);
  ctx.fillStyle = palette.skirting; ctx.fillRect(0, wall - 6, width, 6);
  const glass = atlas?.props.window?.slots?.glass ?? [0, 0, 196, 44];
  const count = Math.max(3, Math.floor((width - 80) / 300));
  const span = (width - 80) / count;
  for (let index = 0; index < count; index += 1) {
    const w = glass[2], h = glass[3], x = 40 + span * index + (span - w) / 2, y = 10;
    sky(ctx, x, y, w, h, hour, 97 + index * 31);
    if (atlas) stamp(ctx, atlas, 'window', x, y);
    if (palette.daylight && hour >= 7 && hour < 18) {
      // A light shaft from each window onto the floor, slanting away from the morning or afternoon sun.
      const lean = hour < 12 ? 70 : -70;
      const light = ctx.createLinearGradient(0, wall, 0, wall + 150);
      light.addColorStop(0, 'rgba(255, 238, 190, 0.20)'); light.addColorStop(1, 'rgba(255, 238, 190, 0)');
      ctx.fillStyle = light;
      ctx.beginPath(); ctx.moveTo(x, wall); ctx.lineTo(x + w, wall); ctx.lineTo(x + w + lean, wall + 150); ctx.lineTo(x + lean, wall + 150); ctx.fill();
    }
  }
  ctx.fillStyle = palette.side; ctx.fillRect(0, 0, 12, layout.height); ctx.fillRect(width - 12, 0, 12, layout.height);
  ctx.fillRect(0, layout.height - 10, width, 10);
}

/** A team's room: a low-pile carpet in a muted tint of the team colour with a soft drop shadow, a woven
 * inner border and a header band in the team colour where the team's sign hangs. */
function rug(ctx: Ctx, zone: ZoneLayout, color: string, palette: Palette) {
  const { x, y, w, h } = zone;
  for (const [grow, alpha] of [[8, 0.05], [5, 0.07], [2, 0.10]] as const) {
    rr(ctx, x - grow / 2, y - grow / 4 + 3, w + grow, h + grow / 2, 12 + grow / 2); ctx.fillStyle = rgba(palette.shade, alpha); ctx.fill();
  }
  fillRR(ctx, x, y, w, h, 12, mix(color, palette.rugEdge, palette.daylight ? 0.30 : 0.62));
  const pile = mix(color, palette.rugBase, palette.daylight ? 0.80 : 0.84);
  fillRR(ctx, x + 4, y + 4, w - 8, h - 8, 9, pile);
  ctx.save();
  rr(ctx, x + 4, y + 4, w - 8, h - 8, 9); ctx.clip();
  const random = seeded(Math.round(x * 13 + y * 7));
  ctx.fillStyle = rgba(palette.shade, 0.035);
  for (let i = 0; i < (w * h) / 220; i += 1) ctx.fillRect(x + random() * w, y + random() * h, 1.5, 1.5);
  const band = ctx.createLinearGradient(0, y, 0, y + 44);
  band.addColorStop(0, mix(color, pile, 0.35)); band.addColorStop(1, mix(color, pile, 0.75));
  ctx.fillStyle = band; ctx.fillRect(x, y, w, 44);
  ctx.fillStyle = rgba(palette.shade, 0.10); ctx.fillRect(x, y + 44, w, 1);
  const sheen = ctx.createLinearGradient(0, y + 45, 0, y + h);
  sheen.addColorStop(0, rgba(palette.glow, palette.daylight ? 0.10 : 0.03)); sheen.addColorStop(1, rgba(palette.shade, 0.06));
  ctx.fillStyle = sheen; ctx.fillRect(x, y + 45, w, h - 45);
  ctx.restore();
  ctx.strokeStyle = mix(color, palette.rugLine, palette.daylight ? 0.55 : 0.25); ctx.globalAlpha = 0.55; ctx.lineWidth = 1;
  rr(ctx, x + 9.5, y + 9.5, w - 19, h - 19, 6); ctx.stroke(); ctx.globalAlpha = 1;
}

const CLUTTER = ['clutter-mug', 'clutter-papers', 'clutter-books', 'clutter-mini-plant'] as const;

/** Desk clutter on a desk prop's "clutter" slot: one item or none, stable per desk. */
function clutter(ctx: Ctx, atlas: Atlas, seat: Seat, prop: 'desk-down' | 'desk-up') {
  const slot = atlas.props[prop]?.slots?.clutter;
  if (!slot) return;
  const pick = Math.floor(seeded(Math.round(seat.desk.x * 31 + seat.desk.y * 17))() * (CLUTTER.length + 1));
  if (pick < CLUTTER.length) stamp(ctx, atlas, CLUTTER[pick], seat.desk.x + slot[0], seat.desk.y + slot[1]);
}

/** One pod at a time, back to front: chairs behind the top row, its desks, the partition, the bottom row's
 * desks, then the chairs in front of them. */
function pods(ctx: Ctx, zone: ZoneLayout, atlas: Atlas) {
  const perPod = zone.pods.length ? zone.seats.length / zone.pods.length : 0;
  zone.pods.forEach((pod, index) => {
    const seats = zone.seats.slice(index * perPod, (index + 1) * perPod);
    const top = seats.filter(seat => seat.facing === 'down');
    const bottom = seats.filter(seat => seat.facing === 'up');
    for (const seat of top) stamp(ctx, atlas, 'chair-down', seat.x, seat.y);
    for (const seat of top) { stamp(ctx, atlas, 'desk-down', seat.desk.x, seat.desk.y); clutter(ctx, atlas, seat, 'desk-down'); }
    stamp(ctx, atlas, 'pod-divider', pod.x + 6, podDivider(pod));
    for (const seat of bottom) { stamp(ctx, atlas, 'desk-up', seat.desk.x, seat.desk.y); clutter(ctx, atlas, seat, 'desk-up'); }
    for (const seat of bottom) stamp(ctx, atlas, 'chair-up', seat.x, seat.y);
  });
}

function decor(ctx: Ctx, atlas: Atlas, items: ReadonlyArray<DecorItem>) {
  for (const item of items) {
    const variant = (item.variant % 3) + 1;
    if (item.kind === 'plant') stamp(ctx, atlas, `plant-${variant}`, item.x, item.y);
    else if (item.kind === 'tall-plant') stamp(ctx, atlas, `tall-plant-${variant}`, item.x, item.y);
    else if (item.kind === 'cooler' || item.kind === 'printer') stamp(ctx, atlas, item.kind, item.x, item.y);
  }
}

/** `atlas` null paints the procedural base alone (floor, walls, skies, rooms and light), so the office shows
 * at once while the furniture atlas is still loading; the next paint with the atlas adds the furniture. */
export function paintOffice(ctx: Ctx, input: PaintInput, atlas: Atlas | null): void {
  const { layout, zones, hour, theme } = input;
  const palette = PALETTE[theme];
  floor(ctx, layout, palette);
  backWall(ctx, layout, hour, palette, atlas);
  const colors = new Map(zones.map(zone => [zone.key, zone]));
  for (const zone of layout.zones) rug(ctx, zone, colors.get(zone.key)?.color || '#8f7ae6', palette);
  if (atlas) furniture(ctx, layout, colors, atlas);
  lighting(ctx, layout, palette, !palette.daylight || hour < 7 || hour >= 19);
}

function furniture(ctx: Ctx, layout: OfficeLayout, colors: ReadonlyMap<string, PaintZone>, atlas: Atlas) {
  const { room, whiteboard, shelf } = layout.owner;
  stamp(ctx, atlas, 'owner-office', room.x, room.y);
  stamp(ctx, atlas, 'whiteboard', whiteboard.x, whiteboard.y);
  stamp(ctx, atlas, 'trophy-shelf', shelf.x, shelf.y);
  for (const zone of layout.zones) {
    const lead = zone.lead;
    stamp(ctx, atlas, 'chair-down', lead.x, lead.y);
    stamp(ctx, atlas, colors.get(zone.key)?.kind === 'router' ? 'router-desk' : 'lead-desk', lead.desk.x, lead.desk.y);
    pods(ctx, zone, atlas);
  }
  if (layout.watchdog) stamp(ctx, atlas, 'watchdog-station', layout.watchdog.station.x, layout.watchdog.station.y);
  stamp(ctx, atlas, 'meeting-area', layout.lounge.x, layout.lounge.y);
  stamp(ctx, atlas, 'coffee-bar', layout.bar.x, layout.bar.y);
  stamp(ctx, atlas, 'entrance', layout.entrance.x, layout.entrance.y);
  decor(ctx, atlas, layout.decor);
}
