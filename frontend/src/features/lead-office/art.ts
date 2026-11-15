/** Lead office art: one procedural vector kit for the side-view cutaway tower.
 * Art direction (docs/PROJECT_COORDINATION.md, "Lead office"): a warm diorama
 * of flat-shaded shapes with soft light, one palette, contact shadows and a
 * consistent key light from the windows; people are 2.6-head chibi figures with
 * a jointed rig, so every pose is drawn, not sampled from a sheet. Daylight
 * follows the viewer's local clock: the window sky, sun or moon, the city's lit
 * windows at night, the window light on the floor and the warmth of the lamps.
 *
 * Owns: palette and looks, the daylight state, the static layer cache (walls,
 * floors, furniture, shaft; painted once per device scale into offscreen
 * canvases) and the per-frame painters (people, screens, bubbles, papers,
 * bursts, door panels, lighting). Painters take room units, read only
 * preallocated state and allocate nothing per frame. No DOM besides the cache's
 * canvases, no timers, no reads.
 * Entry points: lookFor, createDaylight, createLayerCache, drawPerson,
 * drawScreen, drawBubble, drawFolder, drawPaper, drawBurst, drawSparkle,
 * drawEnvelope, drawDoor, drawLighting, drawLeadWindow, drawFloorWindow.
 */
import type { CharacterModel } from '../office-game/office-character';
import { FLOOR_HEIGHT, FLOOR_SPOTS, FLOOR_STRIP, LEAD_SPOTS, OFFICE_HEIGHT, SHAFT, SLAB, TOWER_WIDTH, feetOf } from './layout';

const TAU = Math.PI * 2;

export const INK = '#2b2018';
/** Reserved signals: amber asks, red fails, gold delivers, green is done, blue waits. */
export const SIGNAL = { ask: '#f4b740', fail: '#c8402f', gold: '#e8b04a', good: '#4fae6a', wait: '#5b8fd1' } as const;
const PAPER = '#fffaf0';
const MANILA = '#e8c47c', MANILA_TAB = '#c99a4a';
const WOOD = { top: '#b57d4f', face: '#93603c', shade: '#6f4529', leg: '#5a3a24' } as const;
const PLANK = ['#c08f5e', '#b8865a', '#c69766'] as const;
const SHAFT_COLORS = { body: '#30343f', rib: '#3c4150', frame: '#6b7385', panel: '#9aa3b4', seam: '#5d6576' } as const;
const PLANT = { leaf: '#4f8a4b', leafDark: '#386a3a', pot: '#c2683f', potShade: '#9c4f2f' } as const;

/** A person's look: palette and silhouette marks. */
export interface Look {
  skin: string; skinShade: string; hair: string; hairStyle: 0 | 1 | 2 | 3;
  top: string; topShade: string; legs: string; shoe: string;
  mark: 'tie' | 'headphones' | 'glasses' | 'beret' | 'visor' | 'cap' | 'scarf' | 'none'; markColor: string;
}
const SKINS = [['#f2c9a0', '#d9a77c'], ['#d9a273', '#b9814f'], ['#a8714a', '#8a5a37']] as const;
const HAIRS = ['#3a2a20', '#6b4425', '#1f1b1e', '#a2643a'] as const;
/** Role silhouettes (shared with the studio's character models), two palette variants each. */
const ROLE_LOOKS: Record<CharacterModel, Array<Pick<Look, 'top' | 'topShade' | 'legs' | 'mark' | 'markColor' | 'hairStyle'>>> = {
  coordinator: [{ top: '#2f4a6b', topShade: '#243a55', legs: '#283447', mark: 'tie', markColor: SIGNAL.gold, hairStyle: 3 },
    { top: '#3b3f63', topShade: '#2d304d', legs: '#2a2d40', mark: 'tie', markColor: '#d9a441', hairStyle: 0 }],
  executor: [{ top: '#6c7a3e', topShade: '#56622f', legs: '#3d3a33', mark: 'cap', markColor: '#4b5a2a', hairStyle: 0 },
    { top: '#7a6a3e', topShade: '#625430', legs: '#3d3a33', mark: 'cap', markColor: '#5c4f27', hairStyle: 2 }],
  engineer: [{ top: '#3f6f8f', topShade: '#305a75', legs: '#2f3a48', mark: 'headphones', markColor: '#2b2b30', hairStyle: 0 },
    { top: '#4b5f7d', topShade: '#3a4b64', legs: '#2f3a48', mark: 'headphones', markColor: '#a8402f', hairStyle: 2 }],
  researcher: [{ top: '#f1ece2', topShade: '#d6cfc1', legs: '#4a5568', mark: 'glasses', markColor: '#2b2b30', hairStyle: 1 },
    { top: '#ebe5d8', topShade: '#cfc7b6', legs: '#5b4a3a', mark: 'glasses', markColor: '#6b4425', hairStyle: 3 }],
  reviewer: [{ top: '#8a5a7a', topShade: '#704863', legs: '#3b3346', mark: 'cap', markColor: '#5a4a3a', hairStyle: 0 },
    { top: '#7d5f8f', topShade: '#654c75', legs: '#3b3346', mark: 'glasses', markColor: '#2b2b30', hairStyle: 1 }],
  writer: [{ top: '#b5523a', topShade: '#94412d', legs: '#3e3530', mark: 'beret', markColor: '#7a2f2a', hairStyle: 2 },
    { top: '#a8613f', topShade: '#8a4e31', legs: '#3e3530', mark: 'scarf', markColor: '#e3b24a', hairStyle: 1 }],
  analyst: [{ top: '#4f7a5a', topShade: '#3e6248', legs: '#3a3f37', mark: 'visor', markColor: '#3f8f5a', hairStyle: 3 },
    { top: '#5d7f6c', topShade: '#4a6657', legs: '#3a3f37', mark: 'glasses', markColor: '#2b2b30', hairStyle: 0 }],
  general: [{ top: '#c98a3e', topShade: '#a87131', legs: '#3d4250', mark: 'none', markColor: '', hairStyle: 2 },
    { top: '#5f8fa0', topShade: '#4b7483', legs: '#3d4250', mark: 'scarf', markColor: '#d06a4a', hairStyle: 0 }],
};
/** Wall tint per role: neighbouring floors read as different departments of one building. */
const WALLS: Record<CharacterModel, readonly [string, string]> = {
  coordinator: ['#eadcc3', '#cdb48f'], executor: ['#e3dcc8', '#c4b693'], engineer: ['#dde3dc', '#b9c4b6'],
  researcher: ['#ece0c9', '#cfba95'], reviewer: ['#eadad3', '#cdb3a8'], writer: ['#f0e2c6', '#d5bd92'],
  analyst: ['#dce4e6', '#b6c4c8'], general: ['#eadcc8', '#cdb79a'],
};

function hash(text: string): number {
  let value = 2166136261;
  for (let index = 0; index < text.length; index++) { value ^= text.charCodeAt(index); value = Math.imul(value, 16777619); }
  return value >>> 0;
}
/** A stable look for one person: their role's silhouette, varied by identity. */
export function lookFor(model: CharacterModel, identity: string): Look {
  const seed = hash(identity), variants = ROLE_LOOKS[model];
  const base = variants[seed % variants.length], skin = SKINS[(seed >>> 3) % SKINS.length];
  return { ...base, skin: skin[0], skinShade: skin[1], hair: HAIRS[(seed >>> 6) % HAIRS.length], shoe: '#2a2220' };
}

/* ---------------------------------------------------------------- daylight */

/** Sky keyframes over the local day (minute of day, top colour, horizon colour). */
const SKY_KEYS: ReadonlyArray<readonly [number, readonly [number, number, number], readonly [number, number, number]]> = [
  [0, [14, 22, 46], [34, 46, 84]], [300, [24, 30, 62], [70, 60, 96]], [380, [112, 128, 186], [246, 178, 130]],
  [480, [104, 168, 226], [204, 228, 242]], [720, [92, 160, 226], [196, 226, 246]], [1020, [110, 160, 214], [230, 214, 184]],
  [1110, [86, 92, 160], [242, 140, 92]], [1180, [34, 40, 86], [92, 72, 120]], [1260, [16, 24, 50], [38, 48, 88]], [1440, [14, 22, 46], [34, 46, 84]],
];
export interface Daylight {
  minute: number;
  /** 0 at night, 1 at full day. */
  day: number;
  skyTop: string; skyLow: string; skyline: string;
  /** Where the sun (day) or moon (night) stands across a window, 0..1, and its height 0..1. */
  orbX: number; orbY: number; sun: boolean;
}
const mix = (a: number, b: number, k: number) => Math.round(a + (b - a) * k);
const rgb = (c: readonly [number, number, number], d: readonly [number, number, number], k: number) => `rgb(${mix(c[0], d[0], k)},${mix(c[1], d[1], k)},${mix(c[2], d[2], k)})`;

/** The sky of the viewer's local minute; recomputed only when the minute changes. */
export function createDaylight() {
  const state: Daylight = { minute: -1, day: 1, skyTop: '', skyLow: '', skyline: '', orbX: .5, orbY: .5, sun: true };
  let offset = new Date().getTimezoneOffset();
  return {
    state,
    /** Epoch ms → local minute; true when the sky changed. */
    at(epochMs: number): boolean {
      const minute = Math.floor(((epochMs / 60000 - offset) % 1440 + 1440) % 1440);
      if (minute === state.minute) return false;
      if (state.minute >= 0 && minute < state.minute) offset = new Date().getTimezoneOffset();
      state.minute = minute;
      let index = 0;
      while (index < SKY_KEYS.length - 2 && SKY_KEYS[index + 1][0] <= minute) index++;
      const [from, top0, low0] = SKY_KEYS[index], [to, top1, low1] = SKY_KEYS[index + 1];
      const k = (minute - from) / (to - from);
      state.skyTop = rgb(top0, top1, k); state.skyLow = rgb(low0, low1, k);
      // Daylight rises 05:20-07:20 and sets 17:30-19:30.
      state.day = Math.min(1, Math.max(0, Math.min((minute - 320) / 120, (1170 - minute) / 120)));
      const shade = 40 + Math.round(state.day * 60);
      state.skyline = `rgb(${shade + 4},${shade + 10},${shade + 30})`;
      state.sun = minute >= 330 && minute < 1140;
      const arc = state.sun ? (minute - 330) / 810 : ((minute - 1140 + 1440) % 1440) / 630;
      state.orbX = Math.min(1, Math.max(0, arc));
      state.orbY = 1 - Math.sin(Math.min(1, Math.max(0, arc)) * Math.PI);
      return true;
    },
  };
}

/* ------------------------------------------------------------ static layers */

export type LayerKind = 'lead' | 'desk' | 'lead-desk' | 'chair' | `floor:${CharacterModel}`;
const LAYER_SIZE: Record<'lead' | 'desk' | 'lead-desk' | 'chair' | 'floor', readonly [number, number]> = {
  lead: [TOWER_WIDTH, OFFICE_HEIGHT], floor: [TOWER_WIDTH, FLOOR_HEIGHT], desk: [FLOOR_SPOTS.deskWidth + 4, 40],
  'lead-desk': [LEAD_SPOTS.deskWidth + 4, 44], chair: [18, 30],
};
export const layerSize = (kind: LayerKind) => LAYER_SIZE[kind.startsWith('floor:') ? 'floor' : kind as 'lead'];

/** Offscreen canvases of everything that never moves, painted once per device
 * scale (device pixels per room unit). A new scale replaces the whole cache. */
export function createLayerCache() {
  const layers = new Map<LayerKind, HTMLCanvasElement | null>();
  let scale = 0;
  return {
    get(kind: LayerKind, deviceScale: number): HTMLCanvasElement | null {
      if (deviceScale !== scale) { layers.clear(); scale = deviceScale; }
      if (layers.has(kind)) return layers.get(kind)!;
      const [width, height] = layerSize(kind);
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.ceil(width * deviceScale)); canvas.height = Math.max(1, Math.ceil(height * deviceScale));
      const ctx = canvas.getContext('2d');
      if (!ctx) { layers.set(kind, null); return null; }
      ctx.scale(deviceScale, deviceScale);
      if (kind === 'lead') paintLead(ctx);
      else if (kind === 'desk') paintDesk(ctx, FLOOR_SPOTS.deskWidth, 1);
      else if (kind === 'lead-desk') paintDesk(ctx, LEAD_SPOTS.deskWidth, 2);
      else if (kind === 'chair') paintChair(ctx);
      else paintFloor(ctx, kind.slice(6) as CharacterModel);
      layers.set(kind, canvas);
      return canvas;
    },
    size: () => layers.size,
    clear() { layers.clear(); scale = 0; },
  };
}

function rect(ctx: CanvasRenderingContext2D, color: string, x: number, y: number, w: number, h: number) { ctx.fillStyle = color; ctx.fillRect(x, y, w, h); }
function round(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}
function vertical(ctx: CanvasRenderingContext2D, y0: number, y1: number, stops: ReadonlyArray<readonly [number, string]>) {
  const gradient = ctx.createLinearGradient(0, y0, 0, y1);
  for (const [at, color] of stops) gradient.addColorStop(at, color);
  return gradient;
}

/** Wall, wainscot, floor planks, slab and the shaft column of one room. */
function paintShell(ctx: CanvasRenderingContext2D, height: number, wall: string, wainscot: string, wainscotHeight: number) {
  const floorTop = height - FLOOR_STRIP;
  rect(ctx, wall, 0, SLAB, SHAFT.x, floorTop - SLAB);
  // Wallpaper: faint vertical pinstripes.
  ctx.fillStyle = 'rgba(80,50,20,0.045)';
  for (let x = 4; x < SHAFT.x; x += 9) ctx.fillRect(x, SLAB, 1, floorTop - SLAB);
  rect(ctx, wainscot, 0, floorTop - wainscotHeight, SHAFT.x, wainscotHeight);
  rect(ctx, 'rgba(255,255,255,0.22)', 0, floorTop - wainscotHeight, SHAFT.x, 1);
  rect(ctx, 'rgba(60,35,15,0.18)', 0, floorTop - wainscotHeight + 1, SHAFT.x, 1);
  for (let x = 14; x < SHAFT.x; x += 28) rect(ctx, 'rgba(60,35,15,0.12)', x, floorTop - wainscotHeight + 3, 1, wainscotHeight - 5);
  // Skirting board.
  rect(ctx, '#7a5233', 0, floorTop - 3, SHAFT.x, 3);
  // Floor planks in staggered rows, then the cut edge of the slab below.
  for (let row = 0; row < 3; row++) {
    const y = floorTop + row * 4, offset = row % 2 ? 13 : 0;
    rect(ctx, PLANK[row], 0, y, SHAFT.x, 4);
    for (let x = offset; x < SHAFT.x; x += 26) rect(ctx, 'rgba(70,40,18,0.28)', x, y, 1, 4);
  }
  rect(ctx, 'rgba(255,240,210,0.35)', 0, floorTop, SHAFT.x, 1);
  rect(ctx, '#7a5233', 0, height - 4, SHAFT.x, 4);
  // Ambient occlusion where wall meets floor and under the slab.
  ctx.fillStyle = vertical(ctx, floorTop - 10, floorTop, [[0, 'rgba(40,22,10,0)'], [1, 'rgba(40,22,10,0.22)']]);
  ctx.fillRect(0, floorTop - 10, SHAFT.x, 10);
  ctx.fillStyle = vertical(ctx, SLAB, SLAB + 9, [[0, 'rgba(40,22,10,0.28)'], [1, 'rgba(40,22,10,0)']]);
  ctx.fillRect(0, SLAB, SHAFT.x, 9);
  // Slab: concrete with a lit edge.
  rect(ctx, '#3d332c', 0, 0, TOWER_WIDTH, SLAB);
  rect(ctx, '#5c4d41', 0, SLAB - 1.2, TOWER_WIDTH, 1.2);
  // Shaft: the building's spine with its door frame.
  const feet = feetOf(height);
  rect(ctx, SHAFT_COLORS.body, SHAFT.x, SLAB, SHAFT.width, height - SLAB);
  for (let y = SLAB + 6; y < height; y += 12) rect(ctx, SHAFT_COLORS.rib, SHAFT.x, y, SHAFT.width, 1.5);
  rect(ctx, 'rgba(0,0,0,0.28)', SHAFT.x, SLAB, 2, height - SLAB);
  const door = SHAFT.door;
  rect(ctx, SHAFT_COLORS.frame, door.x - 2, feet - door.height - 2, door.width + 4, door.height + 2);
  rect(ctx, '#1b1d24', door.x, feet - door.height, door.width, door.height);
  rect(ctx, '#2a2e38', door.x, feet - 1, door.width, height - feet + 1);
}

function plant(ctx: CanvasRenderingContext2D, x: number, feet: number, size: number) {
  ctx.fillStyle = PLANT.potShade; round(ctx, x - size * .42, feet - size * .62, size * .84, size * .62, 2); ctx.fill();
  ctx.fillStyle = PLANT.pot; round(ctx, x - size * .42, feet - size * .62, size * .7, size * .62, 2); ctx.fill();
  const leaves: ReadonlyArray<readonly [number, number, number]> = [[-.5, -1.25, -.6], [.45, -1.3, .5], [0, -1.55, 0], [-.25, -1.05, -.3], [.3, -1.0, .35]];
  leaves.forEach(([dx, dy, tilt], index) => {
    ctx.fillStyle = index % 2 ? PLANT.leafDark : PLANT.leaf;
    ctx.beginPath(); ctx.ellipse(x + dx * size * .55, feet - size * .62 + dy * size * .55, size * .2, size * .48, tilt, 0, TAU); ctx.fill();
  });
}
function books(ctx: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, seed: number) {
  const colors = ['#a8402f', '#3f6f8f', '#c98a3e', '#4f7a5a', '#7d5f8f', '#e0c27a'];
  let cursor = x;
  for (let index = 0; cursor < x + width - 2; index++) {
    const w = 3 + ((seed + index * 7) % 3), h = height - ((seed + index * 5) % 4);
    rect(ctx, colors[(seed + index) % colors.length], cursor, y + height - h, w, h);
    rect(ctx, 'rgba(255,255,255,0.25)', cursor + .6, y + height - h + 1.5, .8, h - 3);
    cursor += w + .6;
  }
}
function cabinet(ctx: CanvasRenderingContext2D, x: number, feet: number, width: number, height: number) {
  rect(ctx, 'rgba(40,22,10,0.18)', x + 1, feet - 1, width, 2);
  rect(ctx, WOOD.face, x, feet - height, width, height);
  rect(ctx, WOOD.top, x - 1, feet - height - 2, width + 2, 2.5);
  rect(ctx, WOOD.shade, x, feet - 2, width, 2);
}
function frame(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  rect(ctx, 'rgba(40,22,10,0.18)', x + 1.2, y + 1.2, w, h);
  rect(ctx, '#7a5233', x, y, w, h); rect(ctx, PAPER, x + 1.5, y + 1.5, w - 3, h - 3);
}
function clock(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.fillStyle = 'rgba(40,22,10,0.18)'; ctx.beginPath(); ctx.arc(x + .8, y + .8, 6, 0, TAU); ctx.fill();
  ctx.fillStyle = '#7a5233'; ctx.beginPath(); ctx.arc(x, y, 6, 0, TAU); ctx.fill();
  ctx.fillStyle = PAPER; ctx.beginPath(); ctx.arc(x, y, 4.8, 0, TAU); ctx.fill();
}
/** The pendant lamp: cord from the slab and a brass shade. */
function pendant(ctx: CanvasRenderingContext2D, x: number, drop: number) {
  rect(ctx, '#2b2018', x - .4, SLAB, .8, drop);
  ctx.fillStyle = '#b8873c'; ctx.beginPath(); ctx.moveTo(x - 3, SLAB + drop); ctx.lineTo(x + 3, SLAB + drop);
  ctx.lineTo(x + 6, SLAB + drop + 5); ctx.lineTo(x - 6, SLAB + drop + 5); ctx.closePath(); ctx.fill();
  rect(ctx, '#e0b257', x - 6, SLAB + drop + 4, 12, 1);
}

/** A Worker floor: department tint, a low cabinet, a plant, the role's wall piece. */
function paintFloor(ctx: CanvasRenderingContext2D, model: CharacterModel) {
  const height = FLOOR_HEIGHT, feet = feetOf(height), [wall, wainscot] = WALLS[model];
  paintShell(ctx, height, wall, wainscot, 18);
  pendant(ctx, 222, 6);
  clock(ctx, 270, 26);
  // Below the name card: a cabinet with the department's things on it.
  cabinet(ctx, 14, feet, 58, 20);
  if (model === 'engineer' || model === 'executor') {
    // A small server rack with ports.
    rect(ctx, '#2f3440', 84, feet - 30, 20, 30); rect(ctx, '#4a5162', 85.5, feet - 28.5, 17, 27);
    for (let y = feet - 26; y < feet - 3; y += 5) rect(ctx, '#1d2028', 87, y, 14, 3);
    books(ctx, 18, feet - 32, 26, 10, 1);
  } else if (model === 'researcher') {
    books(ctx, 18, feet - 33, 48, 11, 3);
    ctx.fillStyle = '#3f6f8f'; ctx.beginPath(); ctx.arc(94, feet - 22, 7, 0, TAU); ctx.fill();
    ctx.strokeStyle = '#7a5233'; ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(94, feet - 22, 8.5, -2.2, .6); ctx.stroke();
    rect(ctx, '#7a5233', 91, feet - 13, 6, 13); rect(ctx, '#5a3a24', 88, feet - 2, 12, 2);
  } else if (model === 'reviewer') {
    rect(ctx, '#8b919c', 84, feet - 32, 20, 32); rect(ctx, '#a3a9b3', 85, feet - 31, 18, 9);
    rect(ctx, '#a3a9b3', 85, feet - 21, 18, 9); rect(ctx, '#a3a9b3', 85, feet - 11, 18, 9);
    for (let y = feet - 27; y < feet; y += 10) rect(ctx, '#5d6576', 91, y, 6, 1.5);
    books(ctx, 18, feet - 31, 20, 9, 2);
  } else if (model === 'writer') {
    books(ctx, 18, feet - 33, 40, 11, 5);
    rect(ctx, PAPER, 60, feet - 24, 10, 3); rect(ctx, '#f3ead7', 61, feet - 26, 9, 2);
    plant(ctx, 94, feet, 15);
  } else if (model === 'analyst') {
    books(ctx, 18, feet - 31, 22, 9, 4);
    rect(ctx, '#2f3440', 82, feet - 34, 24, 18); rect(ctx, '#eef3f2', 83.5, feet - 32.5, 21, 15);
    const bars = [5, 9, 7, 12];
    bars.forEach((h, index) => rect(ctx, index === 3 ? SIGNAL.good : '#7aa7b8', 86 + index * 5, feet - 19 - h, 3, h));
    rect(ctx, '#5a3a24', 92, feet - 16, 2, 16);
  } else {
    books(ctx, 18, feet - 32, 30, 10, 6);
    plant(ctx, 94, feet, 18);
  }
  // The wall piece above the desk, beside the window.
  frame(ctx, 254, 40, 26, 18);
  if (model === 'engineer' || model === 'executor') {
    ctx.fillStyle = '#3f6f8f'; ctx.font = 'bold 10px monospace'; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
    ctx.fillText('{ }', 267, 49.5);
  } else if (model === 'reviewer') {
    for (let row = 0; row < 3; row++) {
      rect(ctx, '#d9d2c4', 264, 44.5 + row * 4.5, 12, 1.4);
      ctx.strokeStyle = SIGNAL.good; ctx.lineWidth = 1.1; ctx.beginPath();
      ctx.moveTo(257.5, 45 + row * 4.5); ctx.lineTo(259, 46.5 + row * 4.5); ctx.lineTo(261.5, 43.5 + row * 4.5); ctx.stroke();
    }
  } else if (model === 'analyst') {
    ctx.strokeStyle = '#3f6f8f'; ctx.lineWidth = 1.3; ctx.beginPath();
    ctx.moveTo(258, 54); ctx.lineTo(263, 49); ctx.lineTo(268, 51); ctx.lineTo(276, 44); ctx.stroke();
  } else {
    // A landscape print: hills under a pale sky.
    rect(ctx, '#cfe3ef', 255.5, 41.5, 23, 15);
    ctx.fillStyle = '#7fae74'; ctx.beginPath(); ctx.moveTo(255.5, 56.5); ctx.quadraticCurveTo(262, 46, 270, 52);
    ctx.quadraticCurveTo(275, 48, 278.5, 50); ctx.lineTo(278.5, 56.5); ctx.closePath(); ctx.fill();
    ctx.fillStyle = '#f4d38a'; ctx.beginPath(); ctx.arc(273, 45, 2, 0, TAU); ctx.fill();
  }
}

/** The lead's penthouse: wood panels, a corkboard, the panorama frame, a side table. */
function paintLead(ctx: CanvasRenderingContext2D) {
  const height = OFFICE_HEIGHT, feet = feetOf(height);
  paintShell(ctx, height, '#ecdcc0', '#b98d62', 34);
  // Wood panelling lines on the wainscot.
  for (let x = 10; x < SHAFT.x; x += 22) rect(ctx, 'rgba(60,35,15,0.16)', x, feet - 34, 1, 26);
  pendant(ctx, 72, 10); pendant(ctx, 200, 10);
  // Corkboard frame (its notes are live).
  const board = LEAD_SPOTS.board;
  rect(ctx, 'rgba(40,22,10,0.22)', board.x + 2, board.y + 2, board.width, board.height);
  rect(ctx, '#7a5233', board.x, board.y, board.width, board.height);
  rect(ctx, '#c9a06c', board.x + 3, board.y + 3, board.width - 6, board.height - 6);
  ctx.fillStyle = 'rgba(120,80,40,0.25)';
  for (let index = 0; index < 60; index++) ctx.fillRect(board.x + 4 + (index * 37) % (board.width - 8), board.y + 4 + (index * 23) % (board.height - 8), 1, 1);
  // Rug under the desk.
  ctx.fillStyle = '#8a3f35'; ctx.beginPath(); ctx.ellipse(196, feet + 1, 54, 4.5, 0, 0, TAU); ctx.fill();
  ctx.strokeStyle = '#e2b25a'; ctx.lineWidth = .8; ctx.beginPath(); ctx.ellipse(196, feet + 1, 49, 3.2, 0, 0, TAU); ctx.stroke();
  plant(ctx, 238, feet, 22);
  clock(ctx, 270, 30);
  // Side table with the outbox tray.
  const tray = LEAD_SPOTS.tray;
  rect(ctx, 'rgba(40,22,10,0.2)', tray.x + 1, feet - 1, tray.width, 2);
  rect(ctx, WOOD.leg, tray.x + 2, feet - 20, 2, 20); rect(ctx, WOOD.leg, tray.x + tray.width - 4, feet - 20, 2, 20);
  rect(ctx, WOOD.top, tray.x - 1, feet - 22, tray.width + 2, 3); rect(ctx, WOOD.face, tray.x, feet - 19, tray.width, 3);
  rect(ctx, '#3b3f47', tray.x + 5, feet - 26, tray.width - 10, 4); rect(ctx, '#575c66', tray.x + 6, feet - 25, tray.width - 12, 1);
  // A tall bookshelf in the left corner under the board.
  cabinet(ctx, 14, feet, 82, 24);
  books(ctx, 18, feet - 37, 72, 11, 2);
}

/** A desk seen from the side, with a monitor (its screen is live) and keyboard. */
function paintDesk(ctx: CanvasRenderingContext2D, width: number, monitors: number) {
  const top = 40 - 18 - (monitors > 1 ? -4 : 0);
  const base = monitors > 1 ? 44 : 40;
  rect(ctx, 'rgba(40,22,10,0.22)', 2, base - 2, width, 2);
  rect(ctx, WOOD.leg, 3, top + 3, 2.5, base - top - 3); rect(ctx, WOOD.leg, width - 3, top + 3, 2.5, base - top - 3);
  rect(ctx, WOOD.face, width - 18, top + 3, 18, 12); rect(ctx, WOOD.shade, width - 18, top + 14, 18, 1.5);
  rect(ctx, '#d8b07a', width - 11, top + 8, 4, 1.2);
  rect(ctx, WOOD.top, 0, top, width + 2, 3.2); rect(ctx, 'rgba(255,235,200,0.45)', 0, top, width + 2, .8);
  // Keyboard and mug.
  rect(ctx, '#3b3f47', 3, top - 1.6, 13, 1.6); rect(ctx, '#e8e2d6', width - 8, top - 5, 4.5, 5);
  for (let index = 0; index < monitors; index++) {
    const x = 18 + index * 22;
    rect(ctx, '#3b3f47', x + 7, top - 4, 3, 4); rect(ctx, '#3b3f47', x + 3.5, top - 1, 10, 1);
    rect(ctx, '#23262c', x, top - 19, 18, 15);
  }
}
function paintChair(ctx: CanvasRenderingContext2D) {
  rect(ctx, 'rgba(40,22,10,0.22)', 2, 28.5, 14, 1.5);
  rect(ctx, '#3b3f47', 8, 18, 2, 9); rect(ctx, '#3b3f47', 3, 27, 12, 1.5);
  ctx.fillStyle = '#2b2b30'; ctx.beginPath(); ctx.arc(3.5, 28.5, 1.3, 0, TAU); ctx.arc(14.5, 28.5, 1.3, 0, TAU); ctx.fill();
  ctx.fillStyle = '#41606e'; round(ctx, 2, 15, 15, 4, 2); ctx.fill();
  ctx.fillStyle = '#375360'; round(ctx, 0, 1, 4.5, 16, 2); ctx.fill();
}

/* -------------------------------------------------------------- live layers */

/** A window on a floor: sky, sun or moon, its frame and sill (room units). */
export function drawFloorWindow(ctx: CanvasRenderingContext2D, sky: CanvasGradient, daylight: Daylight, x: number, y: number, w: number, h: number) {
  ctx.fillStyle = sky; ctx.fillRect(x, y, w, h);
  orb(ctx, daylight, x, y, w, h, 3);
  rect(ctx, '#f3ead7', x - 2, y - 2, w + 4, 2); rect(ctx, '#f3ead7', x - 2, y + h, w + 4, 2.5);
  rect(ctx, '#f3ead7', x - 2, y, 2, h); rect(ctx, '#f3ead7', x + w, y, 2, h); rect(ctx, '#f3ead7', x + w / 2 - .8, y, 1.6, h);
  rect(ctx, 'rgba(255,255,255,0.18)', x + 2, y + 2, 3, h - 4);
}
function orb(ctx: CanvasRenderingContext2D, daylight: Daylight, x: number, y: number, w: number, h: number, r: number) {
  const ox = x + r + daylight.orbX * (w - 2 * r), oy = y + r + daylight.orbY * (h - 2 * r);
  ctx.fillStyle = daylight.sun ? '#fff1b8' : '#eef0f6';
  ctx.beginPath(); ctx.arc(ox, oy, r, 0, TAU); ctx.fill();
  if (!daylight.sun) { ctx.fillStyle = daylight.skyTop; ctx.beginPath(); ctx.arc(ox + r * .45, oy - r * .3, r * .85, 0, TAU); ctx.fill(); }
}
/** Precomputed skyline: [x, width, height] per building and lit-window cells. */
const SKYLINE: ReadonlyArray<readonly [number, number, number]> = [
  [0, 12, 22], [11, 9, 32], [19, 14, 18], [32, 8, 40], [39, 16, 26], [54, 10, 34], [63, 13, 20], [75, 9, 44], [83, 15, 28], [97, 11, 36], [107, 12, 24],
];
const LIT: ReadonlyArray<readonly [number, number]> = [
  [3, 18], [14, 26], [14, 20], [23, 14], [35, 34], [35, 28], [35, 22], [43, 20], [47, 16], [57, 28], [57, 22], [67, 16], [78, 38], [78, 30], [78, 22],
  [87, 22], [91, 16], [100, 30], [100, 22], [110, 18],
];
/** The lead's panorama: sky, drifting clouds by day, the city with its lit windows by night. */
export function drawLeadWindow(ctx: CanvasRenderingContext2D, sky: CanvasGradient, daylight: Daylight) {
  const { x, y, width: w, height: h } = LEAD_SPOTS.window;
  ctx.fillStyle = sky; ctx.fillRect(x, y, w, h);
  orb(ctx, daylight, x, y, w, h * .7, 5);
  if (daylight.day > 0) {
    ctx.globalAlpha = .75 * daylight.day; ctx.fillStyle = '#ffffff';
    for (let index = 0; index < 3; index++) {
      const cx = x + ((daylight.minute * .12 + index * 47) % (w + 30)) - 15, cy = y + 10 + index * 9;
      ctx.beginPath(); ctx.ellipse(cx, cy, 9, 2.6, 0, 0, TAU); ctx.ellipse(cx + 5, cy - 1.8, 5, 2.4, 0, 0, TAU); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  ctx.fillStyle = daylight.skyline;
  for (const [bx, bw, bh] of SKYLINE) ctx.fillRect(x + bx, y + h - bh, bw, bh);
  if (daylight.day < 1) {
    ctx.globalAlpha = 1 - daylight.day; ctx.fillStyle = '#ffd98a';
    for (const [lx, ly] of LIT) ctx.fillRect(x + lx, y + h - ly, 2, 2);
    ctx.globalAlpha = 1;
  }
  rect(ctx, '#f3ead7', x - 3, y - 3, w + 6, 3); rect(ctx, '#e6d7b9', x - 4, y + h, w + 8, 4);
  rect(ctx, '#f3ead7', x - 3, y, 3, h); rect(ctx, '#f3ead7', x + w, y, 3, h);
  rect(ctx, '#f3ead7', x + w / 3 - 1, y, 2, h); rect(ctx, '#f3ead7', x + 2 * w / 3 - 1, y, 2, h);
  rect(ctx, 'rgba(255,255,255,0.14)', x + 4, y + 3, 4, h - 6);
}

/** Room light over a painted room: night dimming, the lamp pools and window light on the floor. */
export function drawLighting(ctx: CanvasRenderingContext2D, lights: { pool: CanvasGradient; beam: CanvasGradient }, daylight: Daylight,
  height: number, lamps: readonly number[], windowX: number, windowW: number, lit: boolean) {
  const night = 1 - daylight.day;
  // A room nobody occupies keeps its lights off: dim by day, dark by night.
  if (!lit) { ctx.globalAlpha = .3 + .3 * night; ctx.fillStyle = '#1a1410'; ctx.fillRect(0, 0, SHAFT.x, height); ctx.globalAlpha = 1; }
  else if (night > 0) { ctx.globalAlpha = .2 * night; ctx.fillStyle = '#101630'; ctx.fillRect(0, 0, SHAFT.x, height); ctx.globalAlpha = 1; }
  if (lit) {
    ctx.save(); ctx.globalCompositeOperation = 'lighter'; ctx.globalAlpha = .16 + .2 * night; ctx.fillStyle = lights.pool;
    for (const x of lamps) { ctx.translate(x, 0); ctx.fillRect(-90, 0, 180, height); ctx.translate(-x, 0); }
    ctx.restore();
  }
  if (daylight.day > 0) {
    // Sunlight falls from the window across the floor.
    ctx.save(); ctx.globalAlpha = .2 * daylight.day; ctx.fillStyle = lights.beam;
    const floor = height - FLOOR_STRIP, shift = (daylight.orbX - .5) * -60;
    ctx.beginPath(); ctx.moveTo(windowX, SLAB + 8); ctx.lineTo(windowX + windowW, SLAB + 8);
    ctx.lineTo(windowX + windowW + shift + 24, floor + 6); ctx.lineTo(windowX + shift - 10, floor + 6); ctx.closePath(); ctx.fill();
    ctx.restore();
  }
}
/** Gradients for drawLighting in room units, made once per context. */
export function createLights(ctx: CanvasRenderingContext2D) {
  const pool = ctx.createRadialGradient(0, 18, 2, 0, 30, 90);
  pool.addColorStop(0, 'rgba(255,214,150,1)'); pool.addColorStop(1, 'rgba(255,214,150,0)');
  const beam = ctx.createLinearGradient(0, SLAB, 0, OFFICE_HEIGHT);
  beam.addColorStop(0, 'rgba(255,244,214,0.9)'); beam.addColorStop(1, 'rgba(255,244,214,0)');
  return { pool, beam };
}

export type ScreenMode = 'off' | 'code' | 'paused' | 'done' | 'error';
/** A monitor's screen in room units; `tick` advances the code it shows. */
export function drawScreen(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, mode: ScreenMode, tick: number) {
  if (mode === 'off') {
    rect(ctx, '#2a2f38', x, y, w, h);
    ctx.fillStyle = 'rgba(255,255,255,0.08)'; ctx.beginPath(); ctx.moveTo(x, y + h * .7); ctx.lineTo(x + w * .5, y); ctx.lineTo(x + w * .7, y); ctx.lineTo(x, y + h); ctx.closePath(); ctx.fill();
    return;
  }
  const back = mode === 'code' ? '#1b2b2a' : mode === 'paused' ? '#2b2a22' : mode === 'done' ? '#235d3c' : '#5a1f1a';
  rect(ctx, back, x, y, w, h);
  if (mode === 'code') {
    for (let line = 0; line < 4; line++) {
      const seed = (tick >> 1) + line, width = 3 + ((seed * 7) % 9);
      rect(ctx, line % 3 === 1 ? '#e8c47c' : '#7fd6a0', x + 1.5 + (seed % 2) * 2, y + 1.5 + line * 3.1, Math.min(width, w - 4), 1.2);
    }
    if (tick % 2) rect(ctx, '#e8f5ea', x + w - 3, y + h - 3, 1.6, 1.6);
  } else if (mode === 'paused') {
    rect(ctx, SIGNAL.ask, x + w / 2 - 3, y + h / 2 - 3, 2, 6); rect(ctx, SIGNAL.ask, x + w / 2 + 1, y + h / 2 - 3, 2, 6);
  } else if (mode === 'done') {
    ctx.strokeStyle = '#e9f7ec'; ctx.lineWidth = 1.6; ctx.lineCap = 'round'; ctx.beginPath();
    ctx.moveTo(x + w / 2 - 4, y + h / 2); ctx.lineTo(x + w / 2 - 1, y + h / 2 + 3); ctx.lineTo(x + w / 2 + 4, y + h / 2 - 3); ctx.stroke();
  } else {
    rect(ctx, '#ffd6cf', x + w / 2 - .8, y + 2.5, 1.6, h - 7); rect(ctx, '#ffd6cf', x + w / 2 - .8, y + h - 3.5, 1.6, 1.6);
  }
  // The screen's glow on the wall and the person in front of it.
  ctx.save(); ctx.globalCompositeOperation = 'lighter'; ctx.globalAlpha = .12; ctx.fillStyle = back;
  ctx.fillRect(x - 6, y - 4, w + 12, h + 8); ctx.restore();
}

/* ------------------------------------------------------------------- people */

export type PoseKind = 'stand' | 'walk' | 'type' | 'think' | 'slump' | 'cheer' | 'present' | 'wait' | 'ask' | 'ponder';
/** One person's drawable pose; the scene mutates it in place. */
export interface Pose {
  x: number; feet: number;
  /** 1 faces right, -1 left, 0 the viewer. */
  facing: -1 | 0 | 1;
  /** 0 standing … 1 seated. */
  seat: number;
  kind: PoseKind;
  /** Walk cycle position (0..1) for 'walk'; a beat count for looping poses. */
  phase: number; tick: number;
  carry: boolean;
}

function limb(ctx: CanvasRenderingContext2D, color: string, width: number, x0: number, y0: number, cx: number, cy: number, x1: number, y1: number) {
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); ctx.moveTo(x0, y0); ctx.quadraticCurveTo(cx, cy, x1, y1); ctx.stroke();
}
function dot(ctx: CanvasRenderingContext2D, color: string, x: number, y: number, r: number) { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill(); }

/** A person, feet on (pose.x, pose.feet). Side poses draw facing right under a mirror. */
export function drawPerson(ctx: CanvasRenderingContext2D, look: Look, pose: Pose) {
  ctx.save(); ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  // Contact shadow.
  ctx.fillStyle = 'rgba(30,18,8,0.26)'; ctx.beginPath(); ctx.ellipse(pose.x, pose.feet + .5, pose.seat > .5 ? 7 : 6, 1.6, 0, 0, TAU); ctx.fill();
  if (pose.facing === 0) front(ctx, look, pose); else { ctx.translate(pose.x, 0); ctx.scale(pose.facing, 1); side(ctx, look, pose); }
  ctx.restore();
}

function side(ctx: CanvasRenderingContext2D, look: Look, pose: Pose) {
  const feet = pose.feet, s = pose.seat, kind = pose.kind;
  const walking = kind === 'walk', swing = walking ? Math.sin(pose.phase * TAU) * .55 : 0;
  const hipY = feet - 13 + 2 * s, hipX = 0;
  // Legs: thigh then shin; seated thighs go forward under the desk.
  const thigh = s * Math.PI / 2;
  const tap = kind === 'wait' && pose.tick % 4 === 1 ? 1.4 : 0;
  leg(ctx, look, darker(look.legs), hipX, hipY, feet, s, thigh, -swing, 0);
  const lean = kind === 'slump' ? .95 : kind === 'type' ? .14 : kind === 'think' ? -.12 : walking ? .08 : 0;
  const shoulderX = hipX + Math.sin(lean) * 11, shoulderY = hipY - Math.cos(lean) * 11;
  const headX = shoulderX + Math.sin(lean) * 6.5 + .8, headY = shoulderY - Math.cos(lean) * 6.5;
  // Back arm first.
  arm(ctx, look, pose, shoulderX, shoulderY, headX, headY, swing, true);
  // Torso: a capsule with a shaded back.
  limb(ctx, look.topShade, 9.5, hipX - .6, hipY - 1.5, hipX - .6, (hipY + shoulderY) / 2, shoulderX - .6, shoulderY + 1);
  limb(ctx, look.top, 7.5, hipX + .6, hipY - 1.5, hipX + .6, (hipY + shoulderY) / 2, shoulderX + .6, shoulderY + 1);
  if (look.mark === 'tie') limb(ctx, look.markColor, 1.4, shoulderX + 3.2, shoulderY + 1.5, shoulderX + 3.6, shoulderY + 4, shoulderX + 3.4, shoulderY + 6.5);
  if (look.mark === 'scarf') limb(ctx, look.markColor, 3, shoulderX - 2, shoulderY + .5, shoulderX + 1, shoulderY + 1.5, shoulderX + 3.5, shoulderY + .5);
  leg(ctx, look, look.legs, hipX, hipY, feet, s, thigh, swing, tap);
  head(ctx, look, headX, headY, kind === 'slump');
  arm(ctx, look, pose, shoulderX, shoulderY, headX, headY, swing, false);
}

/** One leg: thigh then shin; seated thighs go forward under the desk. */
function leg(ctx: CanvasRenderingContext2D, look: Look, color: string, hipX: number, hipY: number, feet: number, seat: number,
  thigh: number, angle: number, lift: number) {
  const kneeX = hipX + Math.sin(angle + thigh) * 7, kneeY = hipY + Math.cos(angle + thigh) * 7;
  const footX = kneeX + Math.sin(angle * .6) * 6 + (seat > .5 ? 1 : 0), footY = Math.min(feet, kneeY + Math.cos(angle * .6) * 6) - lift;
  ctx.strokeStyle = color; ctx.lineWidth = 4; ctx.beginPath(); ctx.moveTo(hipX, hipY); ctx.lineTo(kneeX, kneeY); ctx.lineTo(footX, footY); ctx.stroke();
  ctx.fillStyle = look.shoe; ctx.beginPath(); ctx.ellipse(footX + 1.4, footY - .6, 2.8, 1.5, 0, 0, TAU); ctx.fill();
}
const DARKER = new Map<string, string>();
function darker(color: string) {
  let value = DARKER.get(color);
  if (!value) {
    const n = Number.parseInt(color.slice(1), 16);
    value = `rgb(${Math.round((n >> 16 & 255) * .78)},${Math.round((n >> 8 & 255) * .78)},${Math.round((n & 255) * .78)})`;
    DARKER.set(color, value);
  }
  return value;
}

function arm(ctx: CanvasRenderingContext2D, look: Look, pose: Pose, sx: number, sy: number, hx: number, hy: number, swing: number, back: boolean) {
  const kind = pose.kind, color = back ? look.topShade : look.top;
  let x: number, y: number, cx: number, cy: number;
  const from = sy + 1.5;
  if (kind === 'type') {
    const up = (pose.tick + (back ? 1 : 0)) % 2 ? 1.2 : 0;
    x = sx + 11 + (back ? -1.5 : 0); y = pose.feet - 17 - up; cx = sx + 2; cy = from + 8;
  } else if (kind === 'slump') {
    x = sx + 9 + (back ? -2 : 0); y = pose.feet - 17.5; cx = sx + 3; cy = from + 6;
  } else if (kind === 'think' || kind === 'ponder') {
    if (back) { x = sx + (kind === 'ponder' ? -4 : 6); y = from + (kind === 'ponder' ? 8 : 9); cx = sx + (kind === 'ponder' ? -3 : 2); cy = from + 6; }
    else { x = hx + 3.2; y = hy + 5.2; cx = sx + 6; cy = from + 8; }
  } else if (kind === 'wait' || (pose.carry && !back)) {
    x = sx + 5; y = from + 6.5; cx = sx + 3; cy = from + 8;
  } else {
    const a = back ? swing : -swing;
    x = sx + Math.sin(a) * 9.5 + .8; y = from + Math.cos(a) * 9.5; cx = sx + Math.sin(a) * 4 - .5; cy = from + 5;
  }
  limb(ctx, color, 3.2, sx, from, cx, cy, x, y);
  dot(ctx, back ? look.skinShade : look.skin, x, y, 1.7);
  if (!back && (kind === 'wait' || pose.carry)) drawFolder(ctx, x + 1, y + 1);
}

function head(ctx: CanvasRenderingContext2D, look: Look, x: number, y: number, down: boolean) {
  const r = 6;
  dot(ctx, look.skinShade, x - .6, y + .4, r);
  dot(ctx, look.skin, x, y, r - .2);
  // Hair: crown and back of the head (facing right).
  ctx.fillStyle = look.hair; ctx.beginPath(); ctx.arc(x - .2, y - .2, r + .5, Math.PI * .7, Math.PI * 1.9); ctx.closePath(); ctx.fill();
  if (look.hairStyle === 1) dot(ctx, look.hair, x - 4.5, y - 4.5, 2.6);
  if (look.hairStyle === 2) { ctx.beginPath(); ctx.ellipse(x - 6.6, y + 2.8, 1.8, 4, .3, 0, TAU); ctx.fill(); }
  if (look.hairStyle === 3) { ctx.beginPath(); ctx.ellipse(x + 3.4, y - 4.4, 3.2, 1.6, .5, 0, TAU); ctx.fill(); }
  // Face: eye, blush, ear.
  if (!down) { dot(ctx, INK, x + 3.1, y - .2, .85); dot(ctx, 'rgba(230,120,100,0.35)', x + 2.8, y + 2.2, 1.2); }
  dot(ctx, look.skinShade, x - 1, y + .8, 1.4);
  mark(ctx, look, x, y, false);
}

function mark(ctx: CanvasRenderingContext2D, look: Look, x: number, y: number, frontal: boolean) {
  const color = look.markColor;
  if (look.mark === 'headphones') {
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.arc(x - .5, y - .4, 6.8, Math.PI * 1.08, Math.PI * 1.92); ctx.stroke();
    if (frontal) { dot(ctx, color, x - 6.4, y + .6, 2); dot(ctx, color, x + 6.4, y + .6, 2); } else dot(ctx, color, x - 1, y + .6, 2.4);
  } else if (look.mark === 'glasses') {
    ctx.strokeStyle = color; ctx.lineWidth = .8;
    if (frontal) { ctx.beginPath(); ctx.arc(x - 2.4, y, 1.7, 0, TAU); ctx.moveTo(x + 4.1, y); ctx.arc(x + 2.4, y, 1.7, 0, TAU); ctx.stroke(); }
    else { ctx.beginPath(); ctx.arc(x + 3.2, y - .2, 1.8, 0, TAU); ctx.moveTo(x + 1.4, y - .4); ctx.lineTo(x - 1.4, y - .8); ctx.stroke(); }
  } else if (look.mark === 'beret') {
    ctx.fillStyle = color; ctx.beginPath(); ctx.ellipse(x - .5, y - 5.6, 6.4, 2.4, frontal ? 0 : -.18, 0, TAU); ctx.fill();
    dot(ctx, color, x - .5, y - 8, .9);
  } else if (look.mark === 'visor' || look.mark === 'cap') {
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y - 1.2, 6.3, Math.PI * 1.02, Math.PI * 1.98); ctx.closePath(); ctx.fill();
    if (frontal) { ctx.beginPath(); ctx.ellipse(x, y - 1.4, 6, 1.3, 0, 0, TAU); ctx.fill(); }
    else { ctx.beginPath(); ctx.ellipse(x + 5.2, y - 1.6, 3.6, 1, 0, 0, TAU); ctx.fill(); }
  }
}

function front(ctx: CanvasRenderingContext2D, look: Look, pose: Pose) {
  const kind = pose.kind, x = pose.x, beat = pose.tick % 4;
  const lift = kind === 'cheer' ? (beat === 1 || beat === 2 ? 3 : 0) : 0;
  const feet = pose.feet - lift, hipY = feet - 13, shoulderY = hipY - 11, headY = shoulderY - 6.5;
  ctx.strokeStyle = look.legs; ctx.lineWidth = 4;
  ctx.beginPath(); ctx.moveTo(x - 2.3, hipY); ctx.lineTo(x - 2.6, feet - .5); ctx.moveTo(x + 2.3, hipY); ctx.lineTo(x + 2.6, feet - .5); ctx.stroke();
  ctx.fillStyle = look.shoe; ctx.beginPath(); ctx.ellipse(x - 3, feet - .3, 2.2, 1.4, 0, 0, TAU); ctx.ellipse(x + 3, feet - .3, 2.2, 1.4, 0, 0, TAU); ctx.fill();
  limb(ctx, look.topShade, 11.5, x, hipY - 1.5, x, (hipY + shoulderY) / 2, x, shoulderY + 1.5);
  limb(ctx, look.top, 9, x - .8, hipY - 1.5, x - .8, (hipY + shoulderY) / 2, x - .8, shoulderY + 1.5);
  if (look.mark === 'tie') limb(ctx, look.markColor, 1.6, x, shoulderY + 1.5, x + .3, shoulderY + 4.5, x, shoulderY + 8);
  if (look.mark === 'scarf') limb(ctx, look.markColor, 3, x - 3.5, shoulderY + 1, x, shoulderY + 2.5, x + 3.5, shoulderY + 1);
  const wave = beat % 2 ? 1.5 : -1.5;
  for (let side = -1; side <= 1; side += 2) {
    let hx: number, hy: number;
    if (kind === 'cheer') { hx = x + side * (8 + (side > 0 ? wave : -wave) * .4); hy = shoulderY - 8; }
    else if (kind === 'present') { hx = x + side * 3.5; hy = shoulderY + 6; }
    else if (kind === 'ask' && side > 0) { hx = x + 8.5 + (beat === 2 ? 1 : 0); hy = shoulderY - 7.5; }
    else { hx = x + side * 6.5; hy = shoulderY + 10; }
    limb(ctx, side < 0 ? look.topShade : look.top, 3.2, x + side * 4.2, shoulderY + 1.5, x + side * 6, (shoulderY + hy) / 2 + 1, hx, hy);
    dot(ctx, look.skin, hx, hy, 1.7);
  }
  if (kind === 'present') drawPaper(ctx, x, shoulderY + 4, true);
  const hx = x;
  dot(ctx, look.skinShade, hx, headY + .5, 6.1);
  dot(ctx, look.skin, hx, headY, 5.9);
  ctx.fillStyle = look.hair; ctx.beginPath(); ctx.arc(hx, headY - .3, 6.5, Math.PI * .95, Math.PI * 2.05); ctx.closePath(); ctx.fill();
  if (look.hairStyle === 1) dot(ctx, look.hair, hx, headY - 6.6, 2.6);
  if (look.hairStyle === 2) { ctx.beginPath(); ctx.ellipse(hx + 6.2, headY + 3, 1.7, 3.8, -.2, 0, TAU); ctx.fill(); }
  if (look.hairStyle === 3) { ctx.beginPath(); ctx.ellipse(hx - 2.5, headY - 3.6, 3.6, 1.8, -.4, 0, TAU); ctx.fill(); }
  dot(ctx, INK, hx - 2.2, headY + .6, .85); dot(ctx, INK, hx + 2.2, headY + .6, .85);
  dot(ctx, 'rgba(230,120,100,0.35)', hx - 3.6, headY + 2.6, 1.1); dot(ctx, 'rgba(230,120,100,0.35)', hx + 3.6, headY + 2.6, 1.1);
  if (kind === 'cheer' || kind === 'present') {
    ctx.strokeStyle = INK; ctx.lineWidth = .8; ctx.beginPath(); ctx.arc(hx, headY + 2.2, 1.6, .15 * Math.PI, .85 * Math.PI); ctx.stroke();
  } else if (kind === 'ask') {
    ctx.strokeStyle = INK; ctx.lineWidth = .8; ctx.beginPath(); ctx.moveTo(hx - 1, headY + 3.2); ctx.lineTo(hx + 1, headY + 3.2); ctx.stroke();
  }
  mark(ctx, look, hx, headY, true);
}

/* -------------------------------------------------------------------- props */

/** A manila folder (a brief) held or lying, top-left near (x, y). */
export function drawFolder(ctx: CanvasRenderingContext2D, x: number, y: number) {
  rect(ctx, MANILA_TAB, x - 4, y - 6.5, 4, 1.5);
  rect(ctx, MANILA, x - 4, y - 5.5, 8, 9.5);
  rect(ctx, 'rgba(120,80,30,0.35)', x - 4, y + 3, 8, 1);
  rect(ctx, '#fffaf0', x - 2.6, y - 4, 5, .8);
}
/** A result sheet; `check` marks it accepted-ready (green tick). */
export function drawPaper(ctx: CanvasRenderingContext2D, x: number, y: number, check: boolean) {
  rect(ctx, 'rgba(30,18,8,0.2)', x - 4, y - 4.5, 9, 11);
  rect(ctx, PAPER, x - 4.5, y - 5.5, 9, 11);
  for (let line = 0; line < 3; line++) rect(ctx, '#d6cbb5', x - 3, y - 3.5 + line * 2.3, 5.5, .8);
  if (check) {
    ctx.strokeStyle = SIGNAL.good; ctx.lineWidth = 1.3; ctx.lineCap = 'round'; ctx.beginPath();
    ctx.moveTo(x - 1.5, y + 3); ctx.lineTo(x, y + 4.4); ctx.lineTo(x + 3, y + 1.4); ctx.stroke();
  }
}
/** The report on the outbox tray: a gold folder with a star seal. */
export function drawReport(ctx: CanvasRenderingContext2D, x: number, y: number) {
  rect(ctx, 'rgba(30,18,8,0.25)', x - 7, y - 4, 15, 5);
  rect(ctx, '#c99a3a', x - 8, y - 6, 16, 5); rect(ctx, SIGNAL.gold, x - 8, y - 7, 16, 4);
  dot(ctx, SIGNAL.good, x + 4, y - 5, 1.8);
}

export type BubbleKind = 'typing' | 'ask' | 'think' | 'wait' | 'alert';
/** A speech or thought bubble whose tail points down at (x, y). */
export function drawBubble(ctx: CanvasRenderingContext2D, kind: BubbleKind, x: number, y: number, tick: number) {
  const w = 15, h = 11, left = x - w / 2, top = y - h - 3;
  const fill = kind === 'ask' ? SIGNAL.ask : kind === 'alert' ? SIGNAL.fail : PAPER;
  ctx.save();
  ctx.fillStyle = 'rgba(30,18,8,0.22)'; round(ctx, left + .8, top + .8, w, h, 4); ctx.fill();
  ctx.fillStyle = fill; ctx.strokeStyle = INK; ctx.lineWidth = .7;
  if (kind === 'think') {
    round(ctx, left, top, w, h, 5.5); ctx.fill(); ctx.stroke();
    dot(ctx, PAPER, x - 2.5, y - 1.5, 1.4); dot(ctx, PAPER, x - 4, y + .8, .9);
  } else {
    round(ctx, left, top, w, h, 4); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - 2.4, top + h - .3); ctx.lineTo(x, y); ctx.lineTo(x + 2.4, top + h - .3); ctx.closePath(); ctx.fill();
  }
  const cy = top + h / 2;
  if (kind === 'typing' || kind === 'think') {
    for (let index = 0; index < 3; index++) dot(ctx, (tick % 3) === index ? INK : 'rgba(43,32,24,0.35)', x - 4 + index * 4, cy, 1.15);
  } else if (kind === 'ask' || kind === 'alert') {
    ctx.fillStyle = kind === 'ask' ? INK : PAPER; ctx.font = 'bold 8.5px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(kind === 'ask' ? '?' : '!', x, cy + .5);
  } else {
    // Hourglass, its sand running down beat by beat.
    const sand = (tick % 8) / 8;
    ctx.fillStyle = '#d6cbb5'; ctx.beginPath(); ctx.moveTo(x - 3, top + 2); ctx.lineTo(x + 3, top + 2); ctx.lineTo(x, cy); ctx.lineTo(x + 3, top + h - 2);
    ctx.lineTo(x - 3, top + h - 2); ctx.lineTo(x, cy); ctx.closePath(); ctx.fill();
    ctx.fillStyle = SIGNAL.wait;
    ctx.beginPath(); ctx.moveTo(x - 3 * (1 - sand), cy - 3.5 * (1 - sand)); ctx.lineTo(x + 3 * (1 - sand), cy - 3.5 * (1 - sand)); ctx.lineTo(x, cy); ctx.closePath(); ctx.fill();
    ctx.beginPath(); ctx.moveTo(x - 3 * sand, top + h - 2); ctx.lineTo(x + 3 * sand, top + h - 2); ctx.lineTo(x, top + h - 2 - 3.5 * sand); ctx.closePath(); ctx.fill();
    rect(ctx, INK, x - 3.5, top + 1.4, 7, .9); rect(ctx, INK, x - 3.5, top + h - 2.3, 7, .9);
  }
  ctx.restore();
}

const CONFETTI = [SIGNAL.gold, SIGNAL.good, '#e9806a', '#6fa8dc', '#f4d38a'] as const;
/** Confetti of a celebration, `age` ms after it began; closed-form, no particle state. */
export function drawBurst(ctx: CanvasRenderingContext2D, x: number, y: number, age: number, duration: number) {
  const t = age / 1000, fade = 1 - age / duration;
  if (fade <= 0) return;
  ctx.save(); ctx.globalAlpha = Math.min(1, fade * 1.6);
  for (let index = 0; index < 14; index++) {
    const angle = index * 2.39996, speed = 34 + (index * 37) % 26;
    const px = x + Math.cos(angle) * speed * .55 * t, py = y - Math.abs(Math.sin(angle)) * speed * 1.2 * t + 70 * t * t;
    ctx.fillStyle = CONFETTI[index % CONFETTI.length];
    ctx.translate(px, py); ctx.rotate(angle + t * 8); ctx.fillRect(-1.2, -.6, 2.4, 1.2); ctx.rotate(-(angle + t * 8)); ctx.translate(-px, -py);
  }
  ctx.restore();
}
/** Four-point sparkles turning around (x, y): a delivery landing. */
export function drawSparkle(ctx: CanvasRenderingContext2D, x: number, y: number, age: number, duration: number) {
  const fade = 1 - age / duration;
  if (fade <= 0) return;
  ctx.save(); ctx.globalAlpha = fade; ctx.fillStyle = '#fff1b8';
  for (let index = 0; index < 4; index++) {
    const angle = index * Math.PI / 2 + age / 400, radius = 6 + age / 90;
    const sx = x + Math.cos(angle) * radius, sy = y + Math.sin(angle) * radius * .6, size = 2.2 * fade + .6;
    ctx.beginPath(); ctx.moveTo(sx, sy - size); ctx.lineTo(sx + size * .3, sy - size * .3); ctx.lineTo(sx + size, sy);
    ctx.lineTo(sx + size * .3, sy + size * .3); ctx.lineTo(sx, sy + size); ctx.lineTo(sx - size * .3, sy + size * .3);
    ctx.lineTo(sx - size, sy); ctx.lineTo(sx - size * .3, sy - size * .3); ctx.closePath(); ctx.fill();
  }
  ctx.restore();
}
/** A sealed brief travelling the shaft (centre x, y), with its glow. */
export function drawEnvelope(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.save(); ctx.globalCompositeOperation = 'lighter'; ctx.fillStyle = 'rgba(255,214,140,0.35)';
  ctx.beginPath(); ctx.arc(x, y, 9, 0, TAU); ctx.fill(); ctx.restore();
  rect(ctx, PAPER, x - 6, y - 4, 12, 8);
  ctx.strokeStyle = '#c9b48f'; ctx.lineWidth = .8; ctx.beginPath(); ctx.moveTo(x - 6, y - 4); ctx.lineTo(x, y + .5); ctx.lineTo(x + 6, y - 4); ctx.stroke();
  dot(ctx, '#b5523a', x, y + .5, 1.6);
}
/** The shaft door of a room, `open` 0..1, with its indicator lamp. */
export function drawDoor(ctx: CanvasRenderingContext2D, feet: number, open: number) {
  const door = SHAFT.door, half = door.width / 2, gap = half * Math.min(1, Math.max(0, open));
  if (gap > 0) { ctx.fillStyle = 'rgba(255,224,160,0.35)'; ctx.fillRect(door.x + half - gap, feet - door.height, gap * 2, door.height); }
  ctx.fillStyle = SHAFT_COLORS.panel;
  ctx.fillRect(door.x, feet - door.height, half - gap, door.height); ctx.fillRect(door.x + half + gap, feet - door.height, half - gap, door.height);
  ctx.fillStyle = SHAFT_COLORS.seam;
  ctx.fillRect(door.x + half - gap - .6, feet - door.height, .6, door.height); ctx.fillRect(door.x + half + gap, feet - door.height, .6, door.height);
  ctx.fillStyle = 'rgba(255,255,255,0.2)'; ctx.fillRect(door.x + 1, feet - door.height + 1, 1, door.height - 2);
  dot(ctx, open > 0 ? SIGNAL.good : '#7b2f26', door.x + half, feet - door.height - 4, 1.3);
}
