/** The office's commons: spare floor furnished as breakout areas (a lounge, a reading corner, a meeting table, a
 * café) instead of stretching rooms into near-empty halls (owner, 11-17: a lone team's room ran the width of the
 * floor with five people in it). A row's spare width, the floor in front of the owner's office and a room's own
 * spare floor (diorama.ts roomFurniture) are all furnished the same way: whole furniture groups (vignettes) in
 * bands, spread evenly across the area. Every piece is placed by its top-left corner and size and named by its
 * place in the area (band, group slot, the group there, piece), so a floor-plan blend moves each piece of a
 * commons to its new place and fades the ones that come or go (tween.ts; the commons' pieces are decor).
 * Pure geometry. Entry points: COMMONS_MIN_W, furnishArea, furnishCommons. */
import type { DecorItem, DecorKind, Rect } from './layout';

interface Spec { kind: DecorKind; x: number; y: number; w: number; h: number }
interface Vignette { name: string; w: number; h: number; pieces: ReadonlyArray<Spec> }
/** A piece placed in an area; `slot` names its place there. `variant` tells its group (groupOf), so a group's
 * pieces share an accent, and differs per piece, so neighbouring plants differ. */
export interface Placed extends Spec { slot: string; variant: number }
/** Pieces per group in a variant: every vignette has fewer. */
const GROUP_STRIDE = 16;
export const groupOf = (variant: number) => Math.floor(variant / GROUP_STRIDE);

/** The art atlas' plants at their own size (assets/manifest.generated.ts PROPS plant-1, tall-plant-1). */
const PLANT = { w: 28, h: 35 } as const;
const TALL_PLANT = { w: 36, h: 61 } as const;

/** A sofa facing a low table between two armchairs on a rug, a floor lamp behind, a tall plant beside. */
const LOUNGE: Vignette = { name: 'lounge', w: 200, h: 124, pieces: [
  { kind: 'rug', x: 22, y: 16, w: 168, h: 100 },
  { kind: 'sofa', x: 66, y: 20, w: 80, h: 26 },
  { kind: 'table', x: 84, y: 58, w: 44, h: 22 },
  { kind: 'armchair', x: 38, y: 54, w: 28, h: 24 },
  { kind: 'armchair', x: 148, y: 54, w: 28, h: 24 },
  { kind: 'lamp', x: 172, y: 2, w: 14, h: 14 },
  { kind: 'tall-plant', x: 0, y: 6, ...TALL_PLANT },
] };
/** Bookcases standing on the back edge of a rug, two reading chairs either side of a side table, a reading lamp
 * and a plant. */
const LIBRARY: Vignette = { name: 'library', w: 214, h: 116, pieces: [
  { kind: 'rug', x: 14, y: 18, w: 186, h: 92 },
  { kind: 'shelf', x: 20, y: 0, w: 52, h: 30 },
  { kind: 'cabinet', x: 76, y: 0, w: 30, h: 30 },
  { kind: 'shelf', x: 110, y: 0, w: 52, h: 30 },
  { kind: 'armchair', x: 58, y: 54, w: 28, h: 24 },
  { kind: 'armchair', x: 128, y: 54, w: 28, h: 24 },
  { kind: 'table', x: 93, y: 60, w: 28, h: 16 },
  { kind: 'lamp', x: 172, y: 8, w: 14, h: 14 },
  { kind: 'plant', x: 0, y: 78, ...PLANT },
] };
/** The meeting table from the art atlas (its chairs, flip chart and plant on its own carpet). */
const MEETING: Vignette = { name: 'meeting', w: 334, h: 136, pieces: [{ kind: 'meeting', x: 2, y: 2, w: 330, h: 132 }] };
/** Two café tables with their chairs on a rug, a plant between them and a tall plant in the corner. */
const CAFE: Vignette = { name: 'cafe', w: 196, h: 118, pieces: [
  { kind: 'rug', x: 4, y: 8, w: 188, h: 104 },
  { kind: 'round-table', x: 14, y: 12, w: 64, h: 64 },
  { kind: 'round-table', x: 118, y: 42, w: 64, h: 64 },
  { kind: 'plant', x: 92, y: 10, ...PLANT },
  { kind: 'tall-plant', x: 0, y: 54, ...TALL_PLANT },
] };
/** A reading nook: a bookcase on the back edge of a small rug, two armchairs either side of a side table, a lamp
 * and a plant. */
const NOOK: Vignette = { name: 'nook', w: 164, h: 100, pieces: [
  { kind: 'rug', x: 8, y: 18, w: 140, h: 78 },
  { kind: 'shelf', x: 18, y: 0, w: 52, h: 30 },
  { kind: 'plant', x: 82, y: 0, ...PLANT },
  { kind: 'armchair', x: 32, y: 50, w: 28, h: 24 },
  { kind: 'armchair', x: 96, y: 50, w: 28, h: 24 },
  { kind: 'table', x: 66, y: 56, w: 24, h: 16 },
  { kind: 'lamp', x: 146, y: 30, w: 14, h: 14 },
] };
/** A green corner: plants of three heights behind a slatted bench. */
const GREEN: Vignette = { name: 'green', w: 152, h: 100, pieces: [
  { kind: 'tall-plant', x: 0, y: 0, ...TALL_PLANT },
  { kind: 'plant', x: 44, y: 18, ...PLANT },
  { kind: 'plant', x: 80, y: 22, ...PLANT },
  { kind: 'tall-plant', x: 116, y: 2, ...TALL_PLANT },
  { kind: 'bench', x: 24, y: 70, w: 104, h: 24 },
] };

const ORDER: ReadonlyArray<Vignette> = [LOUNGE, MEETING, NOOK, LIBRARY, GREEN, CAFE];
/** Clear floor round an area's groups and between them. */
const PAD = 16;
/** The narrowest area that holds a group: smaller spare floor widens the rooms instead. */
export const COMMONS_MIN_W = Math.min(...ORDER.map(vignette => vignette.w)) + PAD * 2;
/** The lowest area that holds a group. */
export const COMMONS_MIN_H = Math.min(...ORDER.map(vignette => vignette.h)) + PAD * 2;

/** The pieces that furnish `area`; `seed` picks which group comes first. As many bands of groups as the area's
 * height holds, each filled in turn from the cycle of groups (skipping one that does not fit) and spread evenly.
 * Each band starts half the cycle further round than the one above, so a wide area's bands never stack a group
 * over its twin. */
export function furnishArea(area: Rect, seed: number): Placed[] {
  const placed: Placed[] = [];
  const tallest = Math.max(...ORDER.map(vignette => vignette.h));
  const bands = Math.max(1, Math.floor((area.h - PAD) / (tallest + PAD)));
  const bandH = (area.h - PAD) / bands;
  for (let band = 0; band < bands; band += 1) {
    let next = seed + band * Math.ceil(ORDER.length / 2);
    const row: Vignette[] = [];
    let used = 0;
    for (let misses = 0; misses < ORDER.length;) {
      const vignette = ORDER[next % ORDER.length];
      if (vignette.h + PAD <= bandH && used + vignette.w + PAD * (row.length + 2) <= area.w) {
        row.push(vignette); used += vignette.w; misses = 0;
      } else misses += 1;
      next += 1;
    }
    const space = (area.w - used) / (row.length + 1);
    let x = area.x + space;
    row.forEach((vignette, index) => {
      const top = area.y + PAD / 2 + band * bandH + (bandH - vignette.h) / 2;
      vignette.pieces.forEach((piece, at) => placed.push({
        ...piece, x: x + piece.x, y: top + piece.y, slot: `${band}:${index}:${vignette.name}:${at}`,
        variant: (seed + band * 7 + index * 3) * GROUP_STRIDE + at,
      }));
      x += vignette.w + space;
    });
  }
  return placed;
}

/** The commons in `area` as decor keyed under `key`. */
export function furnishCommons(key: string, area: Rect, seed: number): DecorItem[] {
  return furnishArea(area, seed).map(({ slot, ...piece }) => ({ ...piece, key: `${key}:${slot}` }));
}
