/** Team rooms as lit dioramas and the office's light by local time (art direction: docs/FRONTEND_ARCHITECTURE.md,
 * "Office art direction"). A room is a floor of its own material (carpet tiles, parquet, terrazzo or broadloom,
 * tinted by the team colour) inside walls: a back wall whose face carries the team's sign, framed pictures and a
 * clock, glass partitions on the sides and a glass front with the doorway where the aisle leaves. Spare floor
 * fills with furniture placed only where nobody walks (along the back wall beside the head desk, the strip right of
 * the pods, the band under them), so a tall justified room reads as a furnished room, not an empty rug.
 * Light is one grade per local hour (night, dawn, morning, day, golden hour, dusk). The office lights stay on at
 * every hour, as a real office's do, so the grade only cools and dims the floor a little; the hour shows in the
 * window skies, the wall clocks and the floor lamps, which glow softly once it is dark.
 * Pure drawing over layout geometry, no DOM or state. Entry points: gradeAt, roomLights, drawRoom, drawRoomDecor,
 * roomFurniture, drawGrade, wallClock. */
import { COMMONS_MIN_H, COMMONS_MIN_W, furnishArea, groupOf } from './commons';
import { stableHash } from './hash';
import type { DecorKind, OfficeLayout, Rect, ZoneLayout } from './layout';
import { mix, rr, seeded, stamp, type Atlas, type Ctx, type Palette } from './paint';

/** The room's back wall: the band the team sign hangs in (layout's banner row). */
export const BACK_WALL = 40;
const INK = '#3b2a1f';
type Material = 'carpet' | 'parquet' | 'terrazzo' | 'broadloom';
const MATERIALS: readonly Material[] = ['carpet', 'parquet', 'terrazzo', 'broadloom'];

/* ------------------------------------------------------------------ light by local time */

/** The grade for an hour: `dark` how far the evening has gone (0 day .. 1 deep night), `tint` the colour the sky
 * lends the room and `sun` the strength of daylight through the windows. */
export interface Grade { phase: 'night' | 'dawn' | 'morning' | 'day' | 'golden' | 'dusk'; dark: number; tint: string; tintAlpha: number; sun: number }

export function gradeAt(hour: number): Grade {
  if (hour >= 21 || hour < 5) return { phase: 'night', dark: 1, tint: '#1c2350', tintAlpha: 0, sun: 0 };
  if (hour < 7) return { phase: 'dawn', dark: hour < 6 ? 0.75 : 0.45, tint: '#ffb4a0', tintAlpha: 0.04, sun: 0.2 };
  if (hour < 9) return { phase: 'morning', dark: 0, tint: '#ffd9a0', tintAlpha: 0.03, sun: 0.8 };
  if (hour < 17) return { phase: 'day', dark: 0, tint: '#ffe2b0', tintAlpha: 0, sun: 1 };
  if (hour < 19) return { phase: 'golden', dark: hour < 18 ? 0.12 : 0.38, tint: '#ffa04a', tintAlpha: hour < 18 ? 0.05 : 0.07, sun: 0.8 };
  return { phase: 'dusk', dark: hour < 20 ? 0.62 : 0.85, tint: '#8a5fb0', tintAlpha: 0.05, sun: 0 };
}

/** A lamp on the floor plan: its centre and the reach of its glow once it is dark. */
export interface Light { x: number; y: number; r: number }

/** Spare floor in a room that nobody walks through, for furniture: beside the head desk under the back wall, right
 * of the pods, and under them. */
function roomSpaces(zone: ZoneLayout) {
  const left = zone.x + 30, right = zone.x + zone.w - 14, bottom = zone.y + zone.h - 12;
  const desk = zone.lead.desk;
  const pods = zone.pods.length ? {
    x: Math.min(...zone.pods.map(pod => pod.x)) - 24, y: Math.min(...zone.pods.map(pod => pod.y)),
    r: Math.max(...zone.pods.map(pod => pod.x + pod.w)) + 24, b: Math.max(...zone.pods.map(pod => pod.y + pod.h)) + 6,
  } : null;
  const wallTop = zone.y + BACK_WALL + 6;
  const besideTop = Math.min(desk.y - 22, wallTop + 34);
  return {
    wallLeft: { x: left, y: wallTop, w: desk.x - 40 - left, h: besideTop - wallTop },
    wallRight: { x: desk.x + desk.w + 30, y: wallTop, w: right - (desk.x + desk.w + 30), h: besideTop - wallTop },
    side: pods ? { x: pods.r, y: pods.y, w: right - pods.r, h: bottom - pods.y } : { x: desk.x + desk.w + 34, y: desk.y - 6, w: right - (desk.x + desk.w + 34), h: bottom - desk.y + 6 },
    under: pods ? { x: left, y: pods.b + 10, w: right - left, h: bottom - pods.b - 10 }
      : { x: left, y: desk.y + desk.h + 30, w: right - left, h: bottom - (desk.y + desk.h + 30) },
  };
}

type Piece = { kind: DecorKind; x: number; y: number; w: number; h: number; variant: number };

/** The furniture a room has room for, placed by the room's own geometry and seeded by its key. */
export function roomFurniture(zone: ZoneLayout): Piece[] {
  const seed = stableHash(zone.key);
  const space = roomSpaces(zone);
  const pieces: Piece[] = [];
  const along = (rect: Rect, fromRight: boolean) => {
    if (rect.h < 24) return;
    let cursor = fromRight ? rect.x + rect.w : rect.x;
    // Low pieces only: anything taller would reach up over the sign and pictures on the back wall.
    const kinds = ['shelf', 'cabinet', 'shelf'] as const;
    for (let index = 0; index < kinds.length; index += 1) {
      const kind = kinds[(index + seed) % kinds.length];
      const w = kind === 'shelf' ? 52 : 30;
      if ((fromRight ? cursor - w : cursor + w) < rect.x - 0.5 || (fromRight ? cursor - w : cursor + w) > rect.x + rect.w + 0.5) break;
      const x = fromRight ? cursor - w : cursor;
      pieces.push({ kind, x, y: rect.y, w, h: Math.min(rect.h, 30), variant: seed + index });
      cursor = fromRight ? x - 10 : x + w + 10;
    }
  };
  along(space.wallLeft, false);
  along(space.wallRight, true);
  // Spare floor beside and under the pods holds the same furniture groups as the commons, picked by the room.
  const groups = (area: Rect, start: number) => furnishArea(area, start).map(({ kind, x, y, w, h, variant }) => ({ kind, x, y, w, h, variant }));
  const side = space.side;
  if (side.w >= COMMONS_MIN_W && side.h >= COMMONS_MIN_H) pieces.push(...groups(side, seed + 1));
  else if (side.w >= 40 && side.h >= 70) {
    pieces.push({ kind: 'tall-plant', x: side.x + side.w / 2 - 15, y: side.y + 20, w: 30, h: 60, variant: seed + 1 });
    if (side.h >= 200) pieces.push({ kind: 'plant', x: side.x + side.w / 2 - 12, y: side.y + side.h - 40, w: 24, h: 32, variant: seed + 2 });
  }
  const under = space.under;
  if (under.w >= COMMONS_MIN_W && under.h >= COMMONS_MIN_H) pieces.push(...groups(under, seed));
  else if (under.w >= 160 && under.h >= 56) {
    const cx = under.x + under.w / 2, top = under.y + Math.min(24, under.h - 44);
    pieces.push({ kind: 'credenza', x: cx - 70, y: top, w: 140, h: 26, variant: seed });
    pieces.push({ kind: 'plant', x: cx - 106, y: top - 6, w: 24, h: 32, variant: seed + 1 });
    pieces.push({ kind: 'plant', x: cx + 82, y: top - 6, w: 24, h: 32, variant: seed + 2 });
  }
  return pieces;
}

/** The floor lamps of the room lounges and the commons, for their glow once it is dark. */
export function roomLights(layout: OfficeLayout): Light[] {
  const lamps: Array<{ x: number; y: number; w: number; h: number }> = [];
  for (const zone of layout.zones) for (const piece of roomFurniture(zone)) if (piece.kind === 'lamp') lamps.push(piece);
  for (const item of layout.decor) if (item.kind === 'lamp' && item.w !== undefined && item.h !== undefined) lamps.push({ x: item.x, y: item.y, w: item.w, h: item.h });
  return lamps.map(lamp => ({ x: lamp.x + lamp.w / 2, y: lamp.y + lamp.h / 2, r: 44 }));
}

/** The grade over the whole frame, into its own layer. The office lights stay on at every hour, so the evening
 * only cools and dims the floor a little (owner, 11-17: the night of cut-out light pools read as light pollution),
 * the floor lamps glow softly once it is dark and the sky lends the room a faint tint at dawn, golden hour and dusk.
 * Daylight is a soft wash down from the window wall; the wall and the sides keep their shade. Nothing here is
 * additive, so no light blooms over people or their labels. */
export function drawGrade(ctx: Ctx, layout: OfficeLayout, palette: Palette, grade: Grade, lights: ReadonlyArray<Light>) {
  const { width, height, wall } = layout;
  const rgba = (rgb: string, alpha: number) => `rgba(${rgb}, ${alpha})`;
  const dim = grade.dark * (palette.daylight ? 0.13 : 0.1);
  if (dim > 0) { ctx.fillStyle = `rgba(22, 28, 64, ${dim})`; ctx.fillRect(0, 0, width, height); }
  if (grade.dark > 0) {
    for (const light of lights) {
      const glow = ctx.createRadialGradient(light.x, light.y, 0, light.x, light.y, light.r);
      glow.addColorStop(0, `rgba(255, 214, 150, ${0.3 * grade.dark})`); glow.addColorStop(1, 'rgba(255, 214, 150, 0)');
      ctx.fillStyle = glow; ctx.fillRect(light.x - light.r, light.y - light.r, light.r * 2, light.r * 2);
    }
  }
  if (grade.tintAlpha > 0) { ctx.fillStyle = grade.tint; ctx.globalAlpha = grade.tintAlpha; ctx.fillRect(0, 0, width, height); ctx.globalAlpha = 1; }
  if (grade.sun > 0) {
    const day = ctx.createLinearGradient(0, wall, 0, wall + 260);
    day.addColorStop(0, rgba(palette.glow, 0.1 * grade.sun)); day.addColorStop(1, rgba(palette.glow, 0));
    ctx.fillStyle = day; ctx.fillRect(0, wall, width, 260);
  }
  const shade = ctx.createLinearGradient(0, wall, 0, wall + 24);
  shade.addColorStop(0, rgba(palette.shade, 0.2)); shade.addColorStop(1, rgba(palette.shade, 0));
  ctx.fillStyle = shade; ctx.fillRect(0, wall, width, 24);
  for (const [from, to] of [[12, 36], [width - 12, width - 36]]) {
    const side = ctx.createLinearGradient(from, 0, to, 0);
    side.addColorStop(0, rgba(palette.shade, 0.14)); side.addColorStop(1, rgba(palette.shade, 0));
    ctx.fillStyle = side; ctx.fillRect(Math.min(from, to), wall, 24, height - wall);
  }
}

/* ------------------------------------------------------------------ room shell */

/** The floor tile of a material, at `level` pixels per unit (a pattern fills the room). Floors are warm neutrals
 * with only a hint of the team colour, so the rooms read as one office and the people on them carry the contrast;
 * the team colour itself is spent on the wall's accent band and the sign. */
export function paintRoomTile(material: Material, color: string, palette: Palette, level: number): HTMLCanvasElement {
  const size = material === 'terrazzo' ? 96 : 64;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = Math.round(size * level);
  const ctx = canvas.getContext('2d');
  if (!ctx) return canvas;
  ctx.scale(level, level);
  const day = palette.daylight;
  const random = seeded(stableHash(material + color));
  const wrapped = (draw: (dx: number, dy: number) => void) => { for (const dx of [-size, 0, size]) for (const dy of [-size, 0, size]) draw(dx, dy); };
  if (material === 'carpet') {
    // Quiet carpet tiles: the turned pile of alternate tiles shows only as a faint change of tone.
    const base = mix(mix(color, palette.rugBase, day ? 0.88 : 0.86), day ? '#d8cebd' : '#2f2a27', 0.35), alt = mix(base, day ? '#ffffff' : '#000000', 0.045);
    for (let i = 0; i < 2; i += 1) for (let j = 0; j < 2; j += 1) { ctx.fillStyle = (i + j) % 2 ? alt : base; ctx.fillRect(i * 32, j * 32, 32, 32); }
    ctx.fillStyle = `rgba(0,0,0,${day ? 0.045 : 0.1})`; ctx.fillRect(0, 0, size, 0.8); ctx.fillRect(0, 32, size, 0.8); ctx.fillRect(0, 0, 0.8, size); ctx.fillRect(32, 0, 0.8, size);
  } else if (material === 'parquet') {
    const wood = mix(palette.woodLight, color, day ? 0.06 : 0.05);
    // Narrow boards running across the room in a staggered bond, close in tone.
    for (let k = 0; k < 8; k += 1) {
      const offset = (k * 23) % 32;
      for (let x = offset - 32; x < size; x += 32) {
        ctx.fillStyle = mix(wood, k % 2 ? palette.woodAlt : palette.wood, 0.25 + random() * 0.12);
        ctx.fillRect(x, k * 8, 32, 8);
        ctx.fillStyle = palette.seam; ctx.fillRect(x + 31.5, k * 8 + 1, 0.5, 6);
      }
      ctx.fillStyle = palette.seam; ctx.fillRect(0, k * 8 + 7.5, size, 0.5);
    }
  } else if (material === 'terrazzo') {
    const ground = mix(color, day ? '#efe9df' : '#3a3734', day ? 0.92 : 0.9);
    ctx.fillStyle = ground; ctx.fillRect(0, 0, size, size);
    const chips = [mix(ground, mix(color, INK, 0.3), 0.28), mix(ground, '#ffffff', 0.4), mix(ground, day ? '#bfb3a3' : '#6a625a', 0.35), mix(ground, mix(color, '#d3925a', 0.5), 0.3)];
    for (let i = 0; i < 22; i += 1) {
      const x = random() * size, y = random() * size, r = 0.5 + random() * 1.2, fill = chips[i % chips.length], turn = random() * Math.PI;
      ctx.fillStyle = fill;
      wrapped((dx, dy) => { ctx.beginPath(); ctx.ellipse(x + dx, y + dy, r, r * 0.6, turn, 0, Math.PI * 2); ctx.fill(); });
    }
  } else {
    const base = mix(mix(color, palette.rugBase, day ? 0.88 : 0.86), day ? '#ddd3c3' : '#322c29', 0.3);
    ctx.fillStyle = base; ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = `rgba(255,255,255,${day ? 0.045 : 0.02})`;
    for (let y = 0; y < size; y += 16) ctx.fillRect(0, y, size, 6);
    ctx.fillStyle = `rgba(0,0,0,${day ? 0.04 : 0.08})`;
    for (let i = 0; i < 60; i += 1) { const x = random() * size, y = random() * size; wrapped((dx, dy) => ctx.fillRect(x + dx, y + dy, 1.2, 1.2)); }
  }
  return canvas;
}

export const roomMaterial = (zone: { key: string }): Material => MATERIALS[stableHash(zone.key) % MATERIALS.length];

/** A room's shell: contact shadow, its floor (`floor` is the material pattern, already in scene units and anchored
 * to the room), the back wall with its cap and baseboard, the glass sides and the glass front with a doorway at the
 * aisle. */
export function drawRoom(ctx: Ctx, zone: Rect, color: string, palette: Palette, floor: CanvasPattern | null) {
  const { x, y, w, h } = zone;
  const day = palette.daylight;
  const shadow = `rgba(${palette.shade}, ${day ? 0.16 : 0.32})`;
  ctx.fillStyle = shadow;
  rr(ctx, x - 2, y + 4, w + 6, h + 6, 6); ctx.fill();
  ctx.fillStyle = floor ?? mix(color, palette.rugBase, 0.7);
  ctx.fillRect(x, y, w, h);
  // Floor darkens toward the back wall and the sides: the room's own ambient occlusion.
  const occlusion = ctx.createLinearGradient(0, y + BACK_WALL, 0, y + BACK_WALL + 26);
  occlusion.addColorStop(0, `rgba(${palette.shade}, ${day ? 0.26 : 0.4})`); occlusion.addColorStop(1, `rgba(${palette.shade}, 0)`);
  ctx.fillStyle = occlusion; ctx.fillRect(x, y + BACK_WALL, w, 26);
  for (const [from, to] of [[x, x + 16], [x + w, x + w - 16]]) {
    const side = ctx.createLinearGradient(from, 0, to, 0);
    side.addColorStop(0, `rgba(${palette.shade}, ${day ? 0.14 : 0.24})`); side.addColorStop(1, `rgba(${palette.shade}, 0)`);
    ctx.fillStyle = side; ctx.fillRect(Math.min(from, to), y + BACK_WALL, 16, h - BACK_WALL);
  }
  // Back wall: a warm neutral face with a hint of the team colour, a darker cap (the wall's thickness seen from
  // above) with the team's accent band under it, a dado rail and a baseboard.
  const face = mix(color, day ? '#f6efe2' : '#2c3146', day ? 0.86 : 0.84);
  const wallFill = ctx.createLinearGradient(0, y, 0, y + BACK_WALL);
  wallFill.addColorStop(0, mix(face, '#ffffff', day ? 0.18 : 0.05)); wallFill.addColorStop(1, mix(face, '#000000', day ? 0.06 : 0.18));
  ctx.fillStyle = wallFill; ctx.fillRect(x, y, w, BACK_WALL);
  ctx.fillStyle = mix('#7a5a40', INK, day ? 0.35 : 0.7); ctx.fillRect(x - 3, y - 4, w + 6, 6);
  ctx.fillStyle = '#ffffff'; ctx.globalAlpha *= 0.22; ctx.fillRect(x - 3, y - 4, w + 6, 1); ctx.globalAlpha /= 0.22;
  ctx.fillStyle = mix(color, day ? '#ffffff' : '#000000', day ? 0.12 : 0.25); ctx.fillRect(x, y + 2, w, 3);
  ctx.fillStyle = mix(face, INK, 0.22); ctx.fillRect(x, y + BACK_WALL - 13, w, 1.5);
  ctx.fillStyle = mix('#7a5a40', INK, day ? 0.3 : 0.72); ctx.fillRect(x, y + BACK_WALL - 4, w, 4);
  // Glass partitions: a dark frame, pale glass and a highlight; mullions every so often.
  const glass = day ? 'rgba(214, 236, 246, 0.55)' : 'rgba(120, 160, 200, 0.28)';
  const frame = mix('#8a6a50', INK, day ? 0.45 : 0.78);
  const pane = (px: number, py: number, pw: number, ph: number) => {
    ctx.fillStyle = frame; ctx.fillRect(px, py, pw, ph);
    ctx.fillStyle = glass; ctx.fillRect(px + 1, py + 1, pw - 2, ph - 2);
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    if (pw > ph) ctx.fillRect(px + 1, py + 1, pw - 2, 0.8); else ctx.fillRect(px + 1, py + 1, 0.8, ph - 2);
  };
  pane(x - 3, y + 2, 5, h - 2);
  pane(x + w - 2, y + 2, 5, h - 2);
  const door = 30;
  pane(x + door, y + h - 3, w - door + 1, 5);
  ctx.fillStyle = frame;
  for (let mx = x + door + 64; mx < x + w - 20; mx += 64) ctx.fillRect(mx, y + h - 4, 2, 7);
  for (let my = y + BACK_WALL + 60; my < y + h - 20; my += 60) { ctx.fillRect(x - 4, my, 7, 2); ctx.fillRect(x + w - 3, my, 7, 2); }
  // Door posts either side of the doorway.
  ctx.fillStyle = mix('#8a6a50', INK, 0.3); ctx.fillRect(x - 4, y + h - 5, 6, 8); ctx.fillRect(x + door - 3, y + h - 5, 6, 8);
}

/** Wall art and furniture of a room (floor layer): framed pictures on the back wall and the pieces of
 * roomFurniture. The sign and the clock are drawn by the scene over the light. */
export function drawRoomDecor(ctx: Ctx, atlas: Atlas | null, zone: ZoneLayout, color: string, palette: Palette) {
  const day = palette.daylight;
  const seed = stableHash(zone.key);
  const random = seeded(seed);
  // Pictures along the back wall, right of where the sign hangs and left of the clock.
  for (let index = 0, px = zone.x + zone.w - 78; index < 3 && px > zone.x + 300; index += 1, px -= 74) {
    const pw = 34 + Math.floor(random() * 12), ph = 22;
    const top = zone.y + 7;
    ctx.fillStyle = `rgba(${palette.shade}, 0.22)`; ctx.fillRect(px + 1.5, top + 2, pw, ph);
    ctx.fillStyle = mix(INK, color, 0.2); ctx.fillRect(px, top, pw, ph);
    ctx.fillStyle = day ? '#fbf6ec' : '#d9d2c4'; ctx.fillRect(px + 2, top + 2, pw - 4, ph - 4);
    const art = [mix(color, '#ffffff', 0.15), mix(color, '#d3925a', 0.55), mix(color, '#1e6f8f', 0.6), mix(color, INK, 0.4)];
    const style = (seed + index) % 3;
    if (style === 0) { for (let k = 0; k < 3; k += 1) { ctx.fillStyle = art[k]; ctx.beginPath(); ctx.arc(px + 9 + k * ((pw - 18) / 2), top + 11, 3.2 + (k % 2), 0, Math.PI * 2); ctx.fill(); } }
    else if (style === 1) { ctx.fillStyle = art[2]; ctx.beginPath(); ctx.moveTo(px + 4, top + ph - 4); ctx.lineTo(px + pw * 0.4, top + 7); ctx.lineTo(px + pw * 0.62, top + 13); ctx.lineTo(px + pw * 0.78, top + 9); ctx.lineTo(px + pw - 4, top + ph - 4); ctx.fill(); ctx.fillStyle = art[1]; ctx.beginPath(); ctx.arc(px + pw - 9, top + 8, 2.4, 0, Math.PI * 2); ctx.fill(); }
    else { ctx.fillStyle = art[0]; ctx.fillRect(px + 5, top + 5, (pw - 10) / 2, ph - 10); ctx.fillStyle = art[3]; ctx.fillRect(px + 5 + (pw - 10) / 2, top + 5, (pw - 10) / 2, (ph - 10) / 2); ctx.fillStyle = art[1]; ctx.fillRect(px + 5 + (pw - 10) / 2, top + 5 + (ph - 10) / 2, (pw - 10) / 2, (ph - 10) / 2); }
  }
  for (const piece of roomFurniture(zone)) drawPiece(ctx, atlas, piece, color, palette);
}

function contact(ctx: Ctx, palette: Palette, x: number, y: number, w: number, h: number) {
  ctx.fillStyle = `rgba(${palette.shade}, ${palette.daylight ? 0.2 : 0.38})`;
  ctx.beginPath(); ctx.ellipse(x + w / 2, y + h, w / 2 + 2, Math.max(2.5, h * 0.16), 0, 0, Math.PI * 2); ctx.fill();
}
function outlined(ctx: Ctx, fill: string, x: number, y: number, w: number, h: number, r: number) {
  rr(ctx, x, y, w, h, r); ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = INK; ctx.lineWidth = 1; ctx.stroke();
}

/** Muted accents for the commons' fabrics and rugs: it belongs to no team, so it wears none of their colours. */
const COMMONS_ACCENTS = ['#8fa58a', '#c4a07a', '#8c9fb4', '#b89490'] as const;

/** The commons' furniture (commons.ts: the sized decor pieces), each group in one accent, straight on the office
 * floor: every group brings its own rug. `alphaOf` fades a piece that comes or goes in a floor-plan blend. */
export function drawCommons(ctx: Ctx, atlas: Atlas | null, layout: OfficeLayout, palette: Palette, alphaOf: (key: string) => number) {
  const base = ctx.globalAlpha;
  for (const item of layout.decor) {
    if (item.w === undefined || item.h === undefined) continue;
    ctx.globalAlpha = base * alphaOf(item.key);
    drawPiece(ctx, atlas, { kind: item.kind, x: item.x, y: item.y, w: item.w, h: item.h, variant: item.variant },
      COMMONS_ACCENTS[groupOf(item.variant) % COMMONS_ACCENTS.length], palette);
  }
  ctx.globalAlpha = base;
}

export function drawPiece(ctx: Ctx, atlas: Atlas | null, piece: Piece, color: string, palette: Palette) {
  const { x, y, w, h } = piece;
  const day = palette.daylight;
  const wood = day ? '#a8744a' : '#5a3d2b', woodTop = day ? '#c99466' : '#6f4c36';
  const fabric = mix(color, day ? '#e9dccb' : '#3c3a44', 0.5), fabricDark = mix(fabric, INK, 0.25);
  const random = seeded(piece.variant * 7919 + 13);
  switch (piece.kind) {
    case 'shelf': {
      contact(ctx, palette, x, y + h - 2, w, 4);
      outlined(ctx, wood, x, y, w, h, 2);
      ctx.fillStyle = woodTop; ctx.fillRect(x + 1, y + 1, w - 2, 5);
      const shelves = 2, rowH = (h - 9) / shelves;
      for (let row = 0; row < shelves; row += 1) {
        const top = y + 7 + row * rowH;
        ctx.fillStyle = mix(wood, INK, 0.45); ctx.fillRect(x + 3, top, w - 6, rowH - 2);
        for (let bx = x + 4; bx < x + w - 6;) {
          const bw = 2.5 + random() * 3, bh = rowH - 3 - random() * 4;
          ctx.fillStyle = [mix(color, '#ffffff', 0.2), '#d3925a', '#5f8fae', '#9a6fb0', '#6f9e6a', '#efe6d4'][Math.floor(random() * 6)];
          ctx.fillRect(bx, top + rowH - 2 - bh, bw, bh);
          bx += bw + 0.6 + (random() < 0.15 ? 3 : 0);
        }
      }
      break;
    }
    case 'cabinet': {
      contact(ctx, palette, x, y + h - 2, w, 4);
      outlined(ctx, day ? '#c9ced3' : '#58606b', x, y, w, h, 2);
      ctx.fillStyle = day ? '#e4e8ec' : '#6e7783'; ctx.fillRect(x + 1, y + 1, w - 2, 5);
      for (let k = 0; k < 2; k += 1) { ctx.fillStyle = day ? '#b3b9c0' : '#4a515b'; ctx.fillRect(x + 3, y + 8 + k * 10, w - 6, 8); ctx.fillStyle = INK; ctx.fillRect(x + w / 2 - 3, y + 11 + k * 10, 6, 1.5); }
      break;
    }
    case 'tall-plant':
      if (atlas) stamp(ctx, atlas, 'tall-plant-' + ((piece.variant % 3) + 1), x + w / 2, y + h);
      break;
    case 'plant':
      if (atlas) stamp(ctx, atlas, 'plant-' + ((piece.variant % 3) + 1), x + w / 2, y + h);
      break;
    case 'rug': {
      // A flat-woven area rug: a darker border band, a quiet field and a fine inner line.
      const field = mix(color, day ? '#f4ead8' : '#2e2924', day ? 0.72 : 0.74);
      ctx.fillStyle = `rgba(${palette.shade}, ${day ? 0.1 : 0.22})`; rr(ctx, x + 1, y + 2, w, h, 8); ctx.fill();
      ctx.fillStyle = mix(field, INK, day ? 0.22 : 0.3); rr(ctx, x, y, w, h, 8); ctx.fill();
      ctx.fillStyle = field; rr(ctx, x + 4, y + 4, w - 8, h - 8, 5); ctx.fill();
      ctx.strokeStyle = mix(field, day ? '#ffffff' : '#000000', 0.35); ctx.lineWidth = 1; rr(ctx, x + 8.5, y + 8.5, w - 17, h - 17, 3); ctx.stroke();
      break;
    }
    case 'round-table': {
      const cx = x + w / 2, cy = y + h / 2, r = Math.min(w, h) * 0.22;
      for (let k = 0; k < 4; k += 1) {
        const angle = Math.PI / 4 + (k * Math.PI) / 2;
        const px = cx + Math.cos(angle) * r * 1.75, py = cy + Math.sin(angle) * r * 1.6;
        contact(ctx, palette, px - 6, py + 3, 12, 3);
        outlined(ctx, fabricDark, px - 7, py - 7, 14, 14, 4.5);
        outlined(ctx, mix(fabric, '#ffffff', 0.14), px - 5, py - 5, 10, 9, 3);
      }
      ctx.fillStyle = `rgba(${palette.shade}, ${day ? 0.2 : 0.36})`; ctx.beginPath(); ctx.ellipse(cx + 1, cy + 4, r + 1, r * 0.8, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = wood; ctx.beginPath(); ctx.arc(cx, cy + 2, r, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = INK; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = woodTop; ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = '#ffffff'; ctx.beginPath(); ctx.arc(cx - r * 0.35, cy - r * 0.2, 2.6, 0, Math.PI * 2); ctx.fill(); ctx.lineWidth = 0.8; ctx.stroke();
      ctx.fillStyle = mix(color, '#ffffff', 0.3); ctx.beginPath(); ctx.arc(cx + r * 0.35, cy + r * 0.25, 2.2, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      break;
    }
    case 'meeting':
      if (atlas) stamp(ctx, atlas, 'meeting-area', x, y);
      break;
    case 'bench': {
      contact(ctx, palette, x, y + h - 2, w, 4);
      const leg = mix(wood, INK, 0.3);
      outlined(ctx, leg, x + 5, y + h - 9, 4, 9, 1); outlined(ctx, leg, x + w - 9, y + h - 9, 4, 9, 1);
      outlined(ctx, wood, x, y + 3, w, h - 10, 3);
      outlined(ctx, woodTop, x, y, w, h - 13, 3);
      ctx.strokeStyle = mix(woodTop, INK, 0.3); ctx.lineWidth = 0.8;
      for (let k = 1; k < 3; k += 1) { const sy = y + ((h - 13) * k) / 3; ctx.beginPath(); ctx.moveTo(x + 3, sy); ctx.lineTo(x + w - 3, sy); ctx.stroke(); }
      break;
    }
    case 'sofa': {
      contact(ctx, palette, x, y + h - 2, w, 5);
      outlined(ctx, fabricDark, x, y, w, h, 5);
      outlined(ctx, fabric, x + 6, y + 3, w - 12, 9, 3);
      for (let k = 0; k < 2; k += 1) outlined(ctx, mix(fabric, '#ffffff', 0.12), x + 7 + k * ((w - 14) / 2), y + 12, (w - 14) / 2, h - 15, 3);
      break;
    }
    case 'armchair': {
      contact(ctx, palette, x, y + h - 2, w, 4);
      outlined(ctx, fabricDark, x, y, w, h, 5);
      outlined(ctx, mix(fabric, '#ffffff', 0.12), x + 4, y + 6, w - 8, h - 9, 3);
      break;
    }
    case 'table': {
      contact(ctx, palette, x, y + h - 1, w, 4);
      outlined(ctx, wood, x, y + 3, w, h - 3, 6);
      outlined(ctx, woodTop, x, y, w, h - 5, 6);
      ctx.fillStyle = '#fbf6ec'; ctx.fillRect(x + 7, y + 4, 9, 7); ctx.fillStyle = mix(color, INK, 0.2); ctx.fillRect(x + 8, y + 5.5, 7, 1); ctx.fillRect(x + 8, y + 7.5, 5, 1);
      ctx.fillStyle = '#ffffff'; ctx.beginPath(); ctx.arc(x + w - 9, y + 7, 2.6, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = INK; ctx.lineWidth = 0.8; ctx.stroke();
      break;
    }
    case 'lamp': {
      ctx.fillStyle = `rgba(${palette.shade}, 0.25)`; ctx.beginPath(); ctx.ellipse(x + w / 2, y + h + 12, 6, 2.2, 0, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = INK; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.moveTo(x + w / 2, y + h + 12); ctx.lineTo(x + w / 2, y + h / 2); ctx.stroke();
      ctx.fillStyle = day ? '#f7e3b5' : '#ffd88a'; ctx.beginPath(); ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2.4, 0, 0, Math.PI * 2); ctx.fill(); ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = 'rgba(255,255,255,0.6)'; ctx.beginPath(); ctx.ellipse(x + w / 2 - 2, y + h / 2 - 1.5, w / 5, h / 7, 0, 0, Math.PI * 2); ctx.fill();
      break;
    }
    case 'credenza': {
      contact(ctx, palette, x, y + h - 2, w, 5);
      outlined(ctx, wood, x, y, w, h, 2);
      ctx.fillStyle = woodTop; ctx.fillRect(x + 1, y + 1, w - 2, 6);
      for (let k = 0; k < 4; k += 1) { ctx.strokeStyle = mix(wood, INK, 0.4); ctx.lineWidth = 1; ctx.strokeRect(x + 4 + k * ((w - 8) / 4), y + 9, (w - 8) / 4 - 2, h - 12); ctx.fillStyle = INK; ctx.fillRect(x + 4 + k * ((w - 8) / 4) + (w - 8) / 8 - 3, y + 15, 4, 1.2); }
      ctx.fillStyle = mix(color, '#ffffff', 0.2); ctx.fillRect(x + 16, y - 6, 10, 8); ctx.fillStyle = '#d3925a'; ctx.fillRect(x + 28, y - 9, 4, 11); ctx.fillStyle = '#5f8fae'; ctx.fillRect(x + 32.5, y - 8, 4, 10);
      break;
    }
  }
}

/** The room's wall clock showing the local time (drawn every frame on the back wall at its right end). */
export function wallClock(ctx: Ctx, zone: Rect, date: Date, day: boolean) {
  const cx = zone.x + zone.w - 26, cy = zone.y + 18, r = 9;
  ctx.fillStyle = 'rgba(0,0,0,0.18)'; ctx.beginPath(); ctx.arc(cx + 1, cy + 1.5, r, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = INK; ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = day ? '#fffaf0' : '#e9e2d2'; ctx.beginPath(); ctx.arc(cx, cy, r - 1.6, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = INK;
  for (let k = 0; k < 12; k += 3) { const a = (k / 12) * Math.PI * 2; ctx.fillRect(cx + Math.sin(a) * (r - 3.2) - 0.6, cy - Math.cos(a) * (r - 3.2) - 0.6, 1.2, 1.2); }
  const minutes = date.getMinutes() + date.getSeconds() / 60, hours = (date.getHours() % 12) + minutes / 60;
  const hand = (angle: number, length: number, width: number, colour: string) => {
    ctx.strokeStyle = colour; ctx.lineWidth = width; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.sin(angle) * length, cy - Math.cos(angle) * length); ctx.stroke();
  };
  hand((hours / 12) * Math.PI * 2, 4, 1.4, INK);
  hand((minutes / 60) * Math.PI * 2, 6, 1, INK);
  hand((date.getSeconds() / 60) * Math.PI * 2, 6.4, 0.5, '#c77d3a');
  ctx.lineCap = 'butt';
}
